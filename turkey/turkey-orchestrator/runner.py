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


def parse_today_tr(stderr_text: str) -> str | None:
    m = re.search(r"Today \(TR\):\s*(\d{4}-\d{2}-\d{2})", stderr_text)
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
    return None


def newest_file(directory: Path, pattern_glob: str) -> Path | None:
    files = sorted(directory.glob(pattern_glob), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def run_skill(task_cfg: dict, logger: logging.Logger, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    cmd = list(task_cfg["command"])
    # 不依赖 PATH 中存在 python：统一用当前解释器启动子进程
    if cmd and cmd[0] in ("python", "python3"):
        cmd[0] = sys.executable
    if extra_args:
        cmd = cmd + extra_args
    cwd = task_cfg.get("run_cwd") or task_cfg.get("skill_dir")
    env = os.environ.copy()
    env.update(load_env_file(Path(task_cfg.get("env_file", ""))))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    logger.info(f"执行 skill: {' '.join(cmd)} (cwd={cwd}) [python={sys.executable}, ver={sys.version.split()[0]}]")
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=float(task_cfg.get("skill_timeout_sec", 600)))
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


def _find_bht_verify_marker(task_cfg: dict, today_tr: str | None) -> Path | None:
    """Return pending marker path if skill left a BHT verification request."""
    if not today_tr:
        return None
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    cache_dir = skill_dir / task_cfg.get("cache_dir", ".cache/turkey-close-report")
    marker = cache_dir / f"{today_tr}_bht_verify_pending.json"
    return marker if marker.is_file() else None


def _fallback_output_paths(task_cfg: dict, today_tr: str | None) -> tuple[Path | None, Path | None]:
    """定位 close_report 临时版(fallback)输出文件。

    skill 在无 BHT 文章时只写 *_fallback_*.md（正式版 pattern 匹配不到），
    必须显式按 fallback 文件名定位，否则 fallback 推送分支永不触发（09-29 踩坑）。
    优先读配置 fallback_full_pattern/fallback_brief_pattern；缺省时从正式版
    pattern 推导（在 _zh 语言后缀前插入 _fallback）。
    """
    if not today_tr:
        return None, None
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    out_dir = skill_dir / task_cfg.get("output_dir", "output")

    def _derive(kind: str) -> str:
        fb = task_cfg.get(f"fallback_{kind}_pattern", "")
        if fb:
            return fb
        base = task_cfg.get(f"{kind}_pattern", "")
        if not base:
            return ""
        if "_zh." in base:
            return base.replace("_zh.", "_fallback_zh.", 1)
        stem, dot, ext = base.rpartition(".")
        return f"{stem}_fallback.{ext}" if dot else f"{base}_fallback"

    fb_full_p, fb_brief_p = _derive("full"), _derive("brief")
    fb_full = out_dir / fb_full_p.format(date=today_tr) if fb_full_p else None
    fb_brief = out_dir / fb_brief_p.format(date=today_tr) if fb_brief_p else None
    full = fb_full if fb_full and fb_full.is_file() else None
    brief = fb_brief if fb_brief and fb_brief.is_file() else None
    return brief, full


def _source_fingerprint_path(task_cfg: dict, today_tr: str | None) -> Path | None:
    """Path to the persisted source fingerprint for a given report date."""
    if not today_tr:
        return None
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    cache_dir = skill_dir / task_cfg.get("cache_dir", ".cache/turkey-close-report")
    return cache_dir / f"{today_tr}_source_fingerprint.json"


def _load_source_fingerprint(task_cfg: dict, today_tr: str | None) -> dict | None:
    path = _source_fingerprint_path(task_cfg, today_tr)
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_source_fingerprint(task_cfg: dict, today_tr: str | None,
                             fingerprint: dict) -> None:
    path = _source_fingerprint_path(task_cfg, today_tr)
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _extract_close_report_fingerprint(task_cfg: dict, today_tr: str | None) -> dict:
    """Build a fingerprint of the material sources used by close_report.

    The goal is to decide whether a later retry run is worth regenerating.
    Real-time quote numbers change continuously and must NOT trigger a new
    report, so we only track article URLs/IDs and bulletin UUIDs.
    """
    fp: dict[str, str | None] = {
        "bht_article_id": None,
        "bht_url": None,
        "paraborsa_url": None,
        "info_daily_uuid": None,
        "info_technical_uuid": None,
    }
    if not today_tr:
        return fp
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    cache_dir = skill_dir / task_cfg.get("cache_dir", ".cache/turkey-close-report")

    bht_cache = cache_dir / f"bloomberght_close_{today_tr}.json"
    if bht_cache.is_file():
        try:
            data = json.loads(bht_cache.read_text(encoding="utf-8"))
            cr = data.get("closing_review") or {}
            fp["bht_article_id"] = cr.get("article_id")
            fp["bht_url"] = cr.get("url")
            fp["bht_ok"] = data.get("ok", False)
        except Exception:
            pass

    par_cache = cache_dir / f"paraborsa_{today_tr}.json"
    if par_cache.is_file():
        try:
            data = json.loads(par_cache.read_text(encoding="utf-8"))
            fp["paraborsa_url"] = (data.get("selected") or {}).get("url")
        except Exception:
            pass

    info_cache = cache_dir / f"info_yatirim_{today_tr}.json"
    if info_cache.is_file():
        try:
            data = json.loads(info_cache.read_text(encoding="utf-8"))
            fp["info_daily_uuid"] = (data.get("daily") or {}).get("uuid")
            fp["info_technical_uuid"] = (data.get("technical") or {}).get("uuid")
        except Exception:
            pass

    return fp


def _is_fingerprint_worth_regen(old_fp: dict | None, new_fp: dict) -> bool:
    """Return True only when a material source changed in a meaningful way.

    For close_report the only meaningful change is BHT going from missing to
    present. Changes in Paraborsa/Info bulletins or BHT article replacement are
    also considered, but live quote fluctuations are ignored.
    """
    if not old_fp:
        return True

    # The most important case: BHT article became available after being missing.
    old_bht_ok = old_fp.get("bht_ok", False)
    new_bht_ok = new_fp.get("bht_ok", False)
    if not old_bht_ok and new_bht_ok:
        return True

    # BHT article replaced by another article.
    if old_fp.get("bht_article_id") != new_fp.get("bht_article_id"):
        return True

    # Paraborsa/Info source changed.
    if old_fp.get("paraborsa_url") != new_fp.get("paraborsa_url"):
        return True
    if old_fp.get("info_daily_uuid") != new_fp.get("info_daily_uuid"):
        return True
    if old_fp.get("info_technical_uuid") != new_fp.get("info_technical_uuid"):
        return True

    return False


def execute_task(cfg: dict, task_name: str, logger: logging.Logger) -> dict:
    task_cfg = cfg["tasks"][task_name]
    log_dir = Path(cfg.get("logging", {}).get("log_dir", "logs"))
    started = time.time()
    max_retries = int(task_cfg.get("max_retries", 2))
    retry_delay = int(task_cfg.get("retry_delay_sec", 300))
    bht_verify_delay = int(task_cfg.get("bht_verify_delay_sec", 1800))

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

    # close_report 专用：素材指纹去重。兜底补跑（00:10 轮）时若关键素材
    # （BHT 文章/Paraborsa/Info）与上次成功生成完全一致，说明两次运行之间
    # 只有实时行情波动，重新生成只会产出措辞不同的重复稿 → 直接结束。
    # 注意：这不违反"执行即推"——23:45 首发永远生成+推送；这里只是让
    # 兜底轮在【无任何新信息】时不制造第二篇。素材变化（BHT 文章从无到有、
    # 券商文章更换、简报更换）时仍然会重新生成并推送。
    regen_worthwhile = True
    close_fingerprint = task_cfg.get("source_fingerprint_dedup", False)
    pre_fp = _load_source_fingerprint(task_cfg, pre_today_tr) if close_fingerprint else None
    if close_fingerprint and pre_today_tr and pre_fp:
        existing_full_dedup = _today_dated_file(task_cfg, pre_today_tr, "full_pattern")
        if existing_full_dedup:
            new_fp = _extract_close_report_fingerprint(task_cfg, pre_today_tr)
            if not _is_fingerprint_worth_regen(pre_fp, new_fp):
                logger.info(
                    f"素材指纹无变化(BHT id={new_fp.get('bht_article_id')}, "
                    f"paraborsa={new_fp.get('paraborsa_url')})，"
                    f"今日({pre_today_tr})报告已推送过且无新信息，跳过重新生成"
                )
                entry = {"task": task_name, "time": now_iso(),
                         "status": "ok", "dedup": "source_fingerprint_unchanged",
                         "today_tr": pre_today_tr,
                         "full_file": str(existing_full_dedup),
                         "elapsed_sec": round(time.time() - started, 1)}
                write_run_log(log_dir, entry)
                return entry
            logger.info("素材指纹有变化，重新生成修正版")
    elif close_fingerprint and pre_today_tr and not pre_fp:
        logger.info(f"无历史素材指纹({pre_today_tr})，正常生成并记录")

    # skip-if-exists: 仅当【今日日期】的完整报告已存在时才跳过生成
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
        # 只要当天存在 BHT 待校验标记就进入校验模式（不再要求 attempt>1）：
        # - BHT 已发布 -> skill 强刷缓存生成正式版并清标记 -> 正常推送
        # - BHT 仍缺失 -> skill 原样退出(保留标记, 不烧LLM) -> 走下方 fallback
        #   分支的素材指纹去重，不会重复推送临时版
        # 历史踩坑(10-08): 旧逻辑 attempt>1 才带 --verify-bht，但 fallback 分支
        # 推送后直接 return，循环永远到不了 attempt 2，正式版无法自动生成。
        marker = _find_bht_verify_marker(task_cfg, today_tr)
        extra_args = ["--verify-bht"] if marker else None
        try:
            rc, out, err = run_skill(task_cfg, logger, extra_args=extra_args)
        except Exception as exc:  # noqa: BLE001
            # 子进程崩溃（command/cwd 配错、输出解码失败、超时）按失败尝试处理，
            # 不允许异常绕过 run.jsonl 记录与告警（2026-10-10 审查 P2-6）。
            rc, out, err = 1, "", f"run_skill exception: {exc}"
            logger.error(f"run_skill exception (第{attempt}/{max_retries}): {exc}")
        last_stdout, last_stderr = out, err
        parsed_tr = parse_today_tr(err)
        if parsed_tr:
            today_tr = parsed_tr
        # 先尝试定位输出文件——若 skill 实际已成功生成报告，绝不应判假假日。
        # 历史踩坑：close_report 08-18 / 08-19 stderr 含 "borsa_related: no closing review for YYYY-MM-DD"
        # 子串命中 skip_keyword "no close"，导致已生成的报告被错判 skipped_holiday（详见 MEMORY §强制约定 #7）。
        brief_file, full_file = locate_outputs(task_cfg, today_tr, logger)

        # BHT fallback：skill 无 BHT 文章时只产出 *_fallback_*.md，正式版 pattern
        # 匹配不到（full_file=None）。旧代码此处要求 full_file 存在才推送临时版
        # → 死代码 → 误判 gen_failed（09-29 踩坑）。修复：显式定位 fallback 文件，
        # 素材指纹去重后推送临时版并返回；BHT 二次校验交棒 00:10 补跑自动化
        # （进程内 sleep bht_verify_delay 会超出单次执行 600s 上限，不可取）。
        marker = _find_bht_verify_marker(task_cfg, today_tr)
        if rc == 0 and marker and not full_file:
            fb_brief, fb_full = _fallback_output_paths(task_cfg, today_tr)
            if fb_full:
                if close_fingerprint and today_tr:
                    pre_fp_fb = _load_source_fingerprint(task_cfg, today_tr)
                    new_fp_fb = _extract_close_report_fingerprint(task_cfg, today_tr)
                    if pre_fp_fb and not _is_fingerprint_worth_regen(pre_fp_fb, new_fp_fb):
                        logger.info("临时版素材指纹无变化（BHT 仍未发布、券商/Info 未变），跳过重复生成与推送")
                        entry = {"task": task_name, "time": now_iso(),
                                 "status": "ok", "dedup": "source_fingerprint_unchanged",
                                 "today_tr": today_tr,
                                 "elapsed_sec": round(time.time() - started, 1)}
                        write_run_log(log_dir, entry)
                        return entry
                logger.info("BHT 收盘评论未发布，推送临时版(fallback)收评；"
                            "正式版校验交棒 00:10 补跑轮次")
                entry = _do_push(cfg, task_cfg, task_name, today_tr,
                                 fb_brief, fb_full, logger, started, log_dir, "ok_fallback")
                # 推送成功后才记录素材指纹，避免推送失败导致补跑轮误跳过
                if close_fingerprint and today_tr and entry.get("status") == "ok_fallback":
                    try:
                        fp = _extract_close_report_fingerprint(task_cfg, today_tr)
                        _save_source_fingerprint(task_cfg, today_tr, fp)
                        logger.info(f"已记录素材指纹: {today_tr} -> BHT id={fp.get('bht_article_id')}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"记录素材指纹失败(不影响推送): {exc}")
                return entry
            logger.warning("检测到 BHT 待校验标记但临时版文件缺失，按生成失败处理")

        # brief_optional=True 时只要有 full 即成功；否则（morning/close）也需 full
        if rc == 0 and full_file:
            gen_ok = True
            logger.info(f"报告生成成功 (第{attempt}次) -> full={full_file.name} brief={'有' if brief_file else '无'}")
            break
        low = (err + " " + out).lower()
        # 假日/周末/无数据：skill 返回 0 + 未产出文件 + 命中 skip_keywords
        # 关键：必须先确认 full_file 为 None，否则会把已生成的报告误判为假日。
        if rc == 0 and not full_file and any(kw in low for kw in skip_keywords):
            logger.info(f"今日为假日/周末/无数据，跳过 (today_tr={today_tr})")
            entry = {"task": task_name, "time": now_iso(), "status": "skipped_holiday",
                     "today_tr": today_tr, "elapsed_sec": round(time.time() - started, 1)}
            write_run_log(log_dir, entry)
            return entry

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

    entry = _do_push(cfg, task_cfg, task_name, today_tr,
                     brief_file, full_file, logger, started, log_dir, "ok")
    # close_report 专用：推送成功后才记录素材指纹。指纹若在推送前保存，
    # 推送瞬断会被下一轮"指纹无变化"去重永久吞掉（2026-10-10 审查 P1-1）。
    if close_fingerprint and today_tr and entry.get("status") in ("ok", "ok_fallback"):
        try:
            fp = _extract_close_report_fingerprint(task_cfg, today_tr)
            _save_source_fingerprint(task_cfg, today_tr, fp)
            logger.info(f"已记录素材指纹: {today_tr} -> BHT id={fp.get('bht_article_id')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"记录素材指纹失败(不影响推送): {exc}")
    return entry


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
    if brief_file is None and not brief_optional and full_file is not None:
        # brief 配置了但 skill 没产出（如 flash 的 brief 校验失败只打日志仍返回 0）：
        # 只发文件、无文本摘要是静默降级，必须留痕（2026-10-10 审查 P2-4）。
        logger.warning(f"{task_name}: brief 文件缺失且 brief_optional=False — 本次仅推送文件")
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
    # 避免 "跳过" 被误读为 "未推送"（实际已完成简报+文件推送）；
    # ok_fallback 保留原标记，便于 00:10 补跑轮识别"临时版已推、仍需 BHT 二次校验"，
    # 不触发指纹去重短路，确保 BHT 上线后正式版仍能覆盖推送。
    if gen_status == "skipped_exists":
        status = "ok" if push_ok else "push_failed"
    elif gen_status == "ok_fallback":
        status = "ok_fallback" if push_ok else "push_failed"
    else:
        status = "ok" if push_ok else "push_failed"
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
    return 0 if result.get("status") in ("ok", "ok_fallback", "skipped_holiday", "skipped_exists") else 1


if __name__ == "__main__":
    sys.exit(main())
