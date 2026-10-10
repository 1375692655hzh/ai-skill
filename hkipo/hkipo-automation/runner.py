#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""土耳其报告自动化编排器。

流程：运行 skill 生成报告 -> 读取简报/完整报告 -> 推送飞书群 -> 记录运行日志。
简约版(brief)发文本，原稿版(full)发文件。含失败重试与告警推送。仅依赖 Python 标准库。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from push_wecom import (  # noqa: E402
    load_config,
    setup_logging,
    send_with_retry,
    send_file_to_group,
    fit_text,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def load_env_file(env_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not env_path or not env_path.is_file():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def parse_today_tr(text: str) -> str | None:
    """从 skill 输出中解析目标日期，兼容土耳其与 hkipo 两种格式。
    土耳其: "Today (TR): YYYY-MM-DD"；hkipo: "== hkipo update target=YYYY-MM-DD"。
    """
    if not text:
        return None
    m = re.search(r"Today \(TR\):\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.search(r"target[=:]\s*(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def resolve_today_tr(task_cfg: dict, logger: logging.Logger) -> str | None:
    """独立调用 skill 的 resolve_target_date.py 获取 today_tr，避免运行完整 skill。
    支持两种输出格式：纯文本日期（info/aa/paraborsa/x）或 JSON（morning/close）。
    """
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    resolve_script = skill_dir / "scripts" / "resolve_target_date.py"
    if not resolve_script.is_file():
        return None
    env = os.environ.copy()
    env.update(load_env_file(Path(task_cfg.get("env_file", ""))))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(resolve_script)],
            cwd=str(skill_dir), env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        lines = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
        # 从后往前扫描，免疫前置 warning/日志行：先找 JSON（morning/close），再找纯日期
        for i, line in enumerate(reversed(lines)):
            if not line.startswith("{"):
                continue
            # 从该行拼到 stdout 末尾并截到最后一个 }，兼容 JSON 后接额外文本
            start = len(lines) - 1 - i
            raw = "\n".join(lines[start:])
            end = raw.rfind("}")
            if end > 0:
                raw = raw[: end + 1]
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            today_tr = data.get("today_date") or data.get("target_date")
            if today_tr:
                logger.info(f"resolve_target_date(JSON) -> today_tr={today_tr}")
                return str(today_tr)
        # 纯文本格式（info/aa/paraborsa/x 输出 YYYY-MM-DD）
        for line in reversed(lines):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", line)
            if m:
                logger.info(f"resolve_target_date -> today_tr={m.group(1)}")
                return m.group(1)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"resolve_target_date 失败: {exc}")
    # 兜底：脚本无输出时（如 hkipo 的 resolve_target_date.py），回退到今日（UTC+8）
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    logger.info(f"resolve_target_date -> fallback today (UTC+8)={today}")
    return today


def newest_file(directory: Path, pattern_glob: str) -> Path | None:
    files = sorted(directory.glob(pattern_glob), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def run_skill(task_cfg: dict, logger: logging.Logger) -> tuple[int, str, str]:
    cmd = list(task_cfg["command"])
    # 不依赖 PATH 中存在 python：统一用当前解释器启动子进程
    if cmd and cmd[0] in ("python", "python3"):
        cmd[0] = sys.executable
    cwd = task_cfg.get("run_cwd") or task_cfg.get("skill_dir")
    env = os.environ.copy()
    env.update(load_env_file(Path(task_cfg.get("env_file", ""))))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    logger.info(f"执行 skill: {' '.join(cmd)} (cwd={cwd}) [python={sys.executable}, ver={sys.version.split()[0]}]")
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    if proc.stdout:
        logger.info(f"skill stdout: {proc.stdout.strip()[:500]}")
    if proc.stderr:
        logger.info(f"skill stderr: {proc.stderr.strip()[:500]}")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def locate_outputs(task_cfg: dict, today_tr: str | None,
                   logger: logging.Logger) -> tuple[Path | None, Path | None]:
    """返回 (brief_file, full_file)，分别定位简报与完整报告。
    brief_pattern 可选（部分 skill 无简报版）。

    只接受文件名嵌入 today_tr 的当天文件；默认【绝不回退】到旧日期文件，
    避免把昨天旧稿当成今天推送（历史已发生 12 次错版）。
    仅当任务显式配置 "allow_fallback_to_latest": true 时才允许回退到最新文件。
    """
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    out_dir = skill_dir / task_cfg.get("output_dir", "output")
    brief = full = None
    brief_pattern = task_cfg.get("brief_pattern", "")
    full_pattern = task_cfg.get("full_pattern", "")
    if today_tr:
        if brief_pattern:
            b = out_dir / brief_pattern.format(date=today_tr)
            brief = b if b.is_file() else None
        if full_pattern:
            f = out_dir / full_pattern.format(date=today_tr)
            full = f if f.is_file() else None
    # 显式允许时才回退到最新文件（fallback glob 可通过 brief_glob / full_glob 配置）
    if task_cfg.get("allow_fallback_to_latest"):
        if not brief and brief_pattern:
            brief_glob = task_cfg.get("brief_glob") or _glob_from_pattern(brief_pattern)
            nb = newest_file(out_dir, brief_glob)
            if nb and nb.is_file():
                brief = nb
                logger.warning(f"allow_fallback_to_latest: 简报回退到最新文件 {nb.name}")
        if not full and full_pattern:
            full_glob = task_cfg.get("full_glob") or _glob_from_pattern(full_pattern)
            nf = newest_file(out_dir, full_glob)
            if nf and nf.is_file():
                full = nf
                logger.warning(f"allow_fallback_to_latest: 完整报告回退到最新文件 {nf.name}")
    elif today_tr and not full:
        logger.warning(f"未找到今日({today_tr})完整报告，不回退旧文件，将判定生成失败")
    if brief:
        logger.info(f"简报文件: {brief}")
    if full:
        logger.info(f"完整报告文件: {full}")
    return brief, full


def _glob_from_pattern(pattern: str) -> str:
    """从 {date}_xxx.txt 模式推导 glob（把 {date} 替换为 *）。"""
    return pattern.replace("{date}", "*")


def check_ext_consistency(task_cfg: dict, task_name: str, logger: logging.Logger) -> str | None:
    """校验 output 目录最近一次产出文件的扩展名与 full_pattern 期望一致。
    防止 skill 输出 .txt 而 runner 期望 .md（或反之）时静默失配、错发旧稿。
    不一致时返回错误描述；一致或无法判断时返回 None。
    """
    full_pattern = task_cfg.get("full_pattern", "")
    if not full_pattern:
        return None
    expected_ext = Path(full_pattern).suffix  # e.g. ".md"
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    out_dir = skill_dir / task_cfg.get("output_dir", "output")
    if not out_dir.is_dir():
        return None
    # 去掉扩展名做 glob：skill 误产出其他扩展名的文件也能被发现
    stem_glob = _glob_from_pattern(full_pattern)
    dot = stem_glob.rfind(".")
    if dot > 0:
        stem_glob = stem_glob[:dot] + ".*"
    latest = newest_file(out_dir, stem_glob)
    if not latest:
        return None
    if latest.suffix.lower() != expected_ext.lower():
        return (f"扩展名错配: runner 期望 {expected_ext}，但 skill 最新产出为 "
                f"{latest.suffix} (文件: {latest.name})。"
                f"请统一 skill 输出与 automation_config.json 的 full_pattern。")
    return None


def _today_dated_file(task_cfg: dict, today_tr: str | None, pattern_key: str) -> Path | None:
    """仅检查【今日日期】对应的文件是否存在（不回退到最新文件，避免把旧稿当成今天的）。"""
    pattern = task_cfg.get(pattern_key, "")
    if not pattern or not today_tr:
        return None
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    out_dir = skill_dir / task_cfg.get("output_dir", "output")
    p = out_dir / pattern.format(date=today_tr)
    return p if p.is_file() else None


def push_content(cfg: dict, task_cfg: dict, group_names: list[str],
                 content: str, logger: logging.Logger) -> bool:
    groups = cfg.get("groups") or cfg.get("wecom", {}).get("groups", {})
    retries = int(task_cfg.get("push_retries", 3))
    delay = int(task_cfg.get("push_retry_delay_sec", 10))
    ok = True
    for name in group_names:
        g = groups.get(name)
        if not g:
            logger.error(f"未找到群配置: {name}")
            ok = False
            continue
        msg_type = g.get("msg_type", "text")
        mode = g.get("mode", "appchat")
        # 企微 text 上限 2048 字节需截断；飞书 text 上限 30KB，完整版可直接发
        if msg_type == "text" and mode in ("webhook", "appchat"):
            body = fit_text(content)
        else:
            body = content
        try:
            send_with_retry(g, body, logger, retries=retries, delay=delay, msg_type=msg_type)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"推送到 {name} 最终失败: {exc}")
            ok = False
    return ok


def push_file(cfg: dict, task_cfg: dict, group_names: list[str],
              file_path: Path, logger: logging.Logger) -> bool:
    """将完整早报以文件形式发送到群（支持 feishu_app）。"""
    groups = cfg.get("groups") or cfg.get("wecom", {}).get("groups", {})
    retries = int(task_cfg.get("push_retries", 3))
    delay = int(task_cfg.get("push_retry_delay_sec", 10))
    ok = True
    for name in group_names:
        g = groups.get(name)
        if not g:
            logger.error(f"未找到群配置(文件推送): {name}")
            ok = False
            continue
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                send_file_to_group(g, str(file_path))
                logger.info(f"文件推送成功 (第{attempt}次) -> {file_path.name} group={name}")
                last_err = None
                break
            except NotImplementedError as exc:
                logger.warning(f"群 {name} 不支持文件推送，跳过: {exc}")
                last_err = exc
                break  # 不重试不支持的渠道
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning(f"文件推送失败 (第{attempt}/{retries}次) group={name}: {exc}")
                if attempt < retries:
                    time.sleep(delay)
        if last_err and not isinstance(last_err, NotImplementedError):
            logger.error(f"文件推送到 {name} 最终失败: {last_err}")
            ok = False
    return ok


def push_alarm(cfg: dict, logger: logging.Logger, title: str, detail: str) -> None:
    alarm_name = cfg.get("alarm_group") or cfg.get("wecom", {}).get("alarm_group")
    if not alarm_name:
        return
    groups = cfg.get("groups") or cfg.get("wecom", {}).get("groups", {})
    g = groups.get(alarm_name)
    if not g:
        return
    msg = f"{title}\n时间: {now_iso()}\n详情: {detail}"
    try:
        send_with_retry(g, msg, logger, retries=3, delay=10)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"告警推送失败: {exc}")


def write_run_log(log_dir: Path, entry: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "run.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def execute_task(cfg: dict, task_name: str, logger: logging.Logger) -> dict:
    task_cfg = cfg["tasks"][task_name]
    log_dir = Path(cfg.get("logging", {}).get("log_dir", "logs"))
    started = time.time()
    max_retries = int(task_cfg.get("max_retries", 2))
    retry_delay = int(task_cfg.get("retry_delay_sec", 300))

    # skip_keywords: 用于检测假日/周末/无数据的 stderr 关键词（可配置）
    skip_keywords = task_cfg.get("skip_keywords", ["holiday", "weekend", "no briefing", "skipped"])
    # brief_optional: 无简报版时是否仅发文件（默认 False，morning/close 有简报）
    brief_optional = task_cfg.get("brief_optional", False)
    # skip_if_exists: 今日报告已存在则跳过生成，直接推送（默认 True）
    skip_if_exists = task_cfg.get("skip_if_exists", True)

    # 扩展名一致性校验：skill 产出与 runner 期望不一致时 fail-fast + 告警，
    # 避免静默失配后退而求其次推送旧稿
    ext_err = check_ext_consistency(task_cfg, task_name, logger)
    if ext_err:
        logger.error(ext_err)
        push_alarm(cfg, logger, f"⚠️ {task_name} 配置错配", ext_err)
        entry = {"task": task_name, "time": now_iso(), "status": "gen_failed",
                 "today_tr": None, "error": ext_err,
                 "elapsed_sec": round(time.time() - started, 1)}
        write_run_log(log_dir, entry)
        return entry

    # 预解析 today_tr，用于 skip-if-exists 检查
    pre_today_tr = resolve_today_tr(task_cfg, logger)

    # skip-if-exists: 仅当【今日日期】的完整报告已存在时才跳过生成
    # （不回退到"最新文件"，否则会把昨天的旧稿误判为今天已生成）
    # skip_if_exists: 仅当【今日日期】的完整报告已存在时才跳过生成
    # （不回退到"最新文件"，否则会把昨天的旧稿误判为今天已生成）
    # 注意：skipped_exists 仅跳过生成，不跳过推送——执行即推，无论之前是否推过
    if skip_if_exists and pre_today_tr:
        existing_full = _today_dated_file(task_cfg, pre_today_tr, "full_pattern")
        if existing_full:
            logger.info(f"今日报告已存在，跳过生成直接推送: {existing_full.name}")
            existing_brief = _today_dated_file(task_cfg, pre_today_tr, "brief_pattern")
            return _do_push(cfg, task_cfg, task_name, pre_today_tr,
                            existing_brief, existing_full, logger, started, log_dir, "skipped_exists")
        logger.info(f"今日({pre_today_tr})报告不存在，执行生成")

    gen_ok = False
    last_stderr = ""
    last_stdout = ""
    brief_file: Path | None = None
    full_file: Path | None = None
    today_tr: str | None = pre_today_tr

    for attempt in range(1, max_retries + 1):
        rc, out, err = run_skill(task_cfg, logger)
        last_stdout, last_stderr = out, err
        parsed_tr = parse_today_tr(out + "\n" + err)
        if parsed_tr:
            today_tr = parsed_tr
        low = (err + " " + out).lower()
        # 假日/周末/无数据：skill 返回 0 且命中 skip_keywords
        if rc == 0 and any(kw in low for kw in skip_keywords):
            logger.info(f"今日为假日/周末/无数据，跳过 (today_tr={today_tr})")
            entry = {"task": task_name, "time": now_iso(), "status": "skipped_holiday",
                     "today_tr": today_tr, "elapsed_sec": round(time.time() - started, 1)}
            write_run_log(log_dir, entry)
            return entry
        brief_file, full_file = locate_outputs(task_cfg, today_tr, logger)
        # 任务无需输出文件时（如 reminder/cleanup），只要 skill rc=0 即成功
        no_output_required = not task_cfg.get("brief_pattern") and not task_cfg.get("full_pattern")
        if rc == 0 and (full_file or no_output_required):
            gen_ok = True
            if no_output_required:
                logger.info(f"任务执行成功 (第{attempt}次) -> 无需输出文件")
            else:
                logger.info(f"报告生成成功 (第{attempt}次) -> full={full_file.name} brief={'有' if brief_file else '无'}")
            break
        logger.warning(f"生成失败 (第{attempt}/{max_retries}) rc={rc}")
        if attempt < max_retries:
            logger.info(f"{retry_delay}秒后重试...")
            time.sleep(retry_delay)

    if not gen_ok:
        detail = (last_stderr or last_stdout or "未知错误")[:500]
        logger.error("报告生成最终失败，推送告警")
        push_alarm(cfg, logger, f"⚠️ {task_name} 生成失败", detail)
        entry = {"task": task_name, "time": now_iso(), "status": "gen_failed",
                 "today_tr": today_tr, "error": detail,
                 "elapsed_sec": round(time.time() - started, 1)}
        write_run_log(log_dir, entry)
        return entry

    return _do_push(cfg, task_cfg, task_name, today_tr,
                    brief_file, full_file, logger, started, log_dir, "ok")


def _do_push(cfg: dict, task_cfg: dict, task_name: str, today_tr: str | None,
             brief_file: Path | None, full_file: Path | None,
             logger: logging.Logger, started: float, log_dir: Path,
             gen_status: str) -> dict:
    """推送逻辑：简约版(brief)发文本，原稿版(full)发文件。无 brief 时发通知文本。"""
    push_groups = task_cfg.get("push_groups", [])
    brief_optional = task_cfg.get("brief_optional", False)
    # 推送标题前缀
    title_prefix = task_cfg.get("title_prefix", task_name)

    text_ok = True
    if brief_file:
        # 有简报版：直接发文本
        text_content = brief_file.read_text(encoding="utf-8").strip()
        text_ok = push_content(cfg, task_cfg, push_groups, text_content, logger)
        if not text_ok:
            push_alarm(cfg, logger, f"⚠️ {task_name} 文本推送失败", f"文件: {brief_file.name}")
    elif brief_optional and full_file:
        # 无简报版：发通知文本（标题 + 报告已生成提示）
        notify = f"【{title_prefix}】{today_tr or ''} 报告已生成，完整内容见文件。"
        text_ok = push_content(cfg, task_cfg, push_groups, notify, logger)
        if not text_ok:
            push_alarm(cfg, logger, f"⚠️ {task_name} 通知推送失败", "通知文本发送失败")

    file_ok = True
    if task_cfg.get("send_full_as_file", True) and full_file:
        file_ok = push_file(cfg, task_cfg, push_groups, full_file, logger)
        if not file_ok:
            push_alarm(cfg, logger, f"⚠️ {task_name} 文件推送失败", f"文件: {full_file.name}")

    push_ok = text_ok and file_ok
    # status 语义：skipped_exists 时若推送成功，记 ok 而非 skipped_exists，
    # 避免 "跳过" 被误读为 "未推送"（实际已完成简报+文件推送）
    if gen_status == "skipped_exists":
        status = "ok" if push_ok else "push_failed"
    else:
        status = "ok" if (push_ok and gen_status == "ok") else (
            "ok" if push_ok else "push_failed")
    entry = {"task": task_name, "time": now_iso(),
             "status": status,
             "today_tr": today_tr,
             "brief_file": str(brief_file) if brief_file else None,
             "full_file": str(full_file) if full_file else None,
             "text_push": text_ok, "file_push": file_ok,
             "elapsed_sec": round(time.time() - started, 1)}
    write_run_log(log_dir, entry)
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="土耳其报告自动化编排（支持7个skill）")
    ap.add_argument("--config", default=str(HERE / "automation_config.json"))
    ap.add_argument("--task", default="morning_briefing")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_cfg = cfg.get("logging", {})
    logger = setup_logging(Path(log_cfg.get("log_dir", "logs")), log_cfg.get("level", "INFO"))

    if args.task not in cfg.get("tasks", {}):
        logger.error(f"未找到任务: {args.task}")
        return 2
    if not cfg["tasks"][args.task].get("enabled", True):
        logger.info(f"任务 {args.task} 已禁用，跳过")
        return 0

    result = execute_task(cfg, args.task, logger)
    logger.info(f"任务结束: {result}")
    return 0 if result.get("status") in ("ok", "skipped_holiday", "skipped_exists") else 1


if __name__ == "__main__":
    sys.exit(main())
