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
import shutil
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
    # utf-8-sig：带 BOM 的 .env 也能正确解析（与无 BOM 文件行为一致）
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
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


def _match_dated_file(out_dir: Path, pattern: str, date: str) -> Path | None:
    """按 pattern({date} 已替换) 定位文件。pattern 含 glob 通配符(*?[)时按
    glob 取最新匹配（如 土耳其快讯_{date}_中文简报*.md 的时间后缀），否则精确路径。
    始终要求文件名嵌入指定日期，绝不跨日期回退。"""
    target = pattern.format(date=date)
    if any(ch in target for ch in "*?["):
        return newest_file(out_dir, target)
    p = out_dir / target
    return p if p.is_file() else None


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
            brief = _match_dated_file(out_dir, brief_pattern, today_tr)
        if full_pattern:
            full = _match_dated_file(out_dir, full_pattern, today_tr)
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
    return _match_dated_file(out_dir, pattern, today_tr)


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
    # 2026-10-19 事故修正：前一晚未发文时缓存 article_id=None，此后 BHT 补发
    # 文章，纯读缓存的指纹预检永远判"素材无变化"，00:10 补跑轮被去重吞掉、
    # 正式版永不接棒。缓存无 id 时改用探针实时重探列表页（_scan_list_page
    # 内置双抓合并防 CDN 漏卡）；缓存已有 id 则免探保持快速路径。
    if fp.get("bht_article_id") is None:
        probe = skill_dir / "scripts" / "probe_bht_close.py"
        if probe.is_file():
            try:
                proc = subprocess.run(
                    [sys.executable, str(probe), "--date", str(today_tr)],
                    cwd=str(skill_dir), capture_output=True, text=True,
                    encoding="utf-8", timeout=120,
                )
                line = next((l for l in reversed((proc.stdout or "").splitlines())
                             if l.strip().startswith("{")), "")
                if line:
                    live = json.loads(line)
                    if live.get("article_id"):
                        fp["bht_article_id"] = live["article_id"]
                        fp["bht_url"] = live.get("url")
                        fp["bht_ok"] = True
            except Exception:
                pass  # 探针失败退回缓存口径，去重行为不变

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


# ---------------------------------------------------------------------------
# 腾讯文档上传（通用分支，仅对配置 "tdoc_upload": true 的任务生效）
# 完整报告 -> 腾讯智能文档（mcporter 调 tencent-docs）-> 群链接文本推送。
# 上传机制与 tr_daily.py 一致（Windows 下 mcporter.CMD 改走 node + cli.js）。
# ---------------------------------------------------------------------------

_TDOC_CHUNK_LIMIT = 12000  # 单块字符上限，超出分块后续写（与 tr_daily chunk_limit 一致）


def _mcporter_invocation(exe: str, service: str, tool: str, payload: str) -> list[str]:
    """构造 mcporter 调用命令。Windows 下 shutil.which 命中 .cmd/.bat 批处理
    包装时，经 cmd.exe 传多行/含特殊字符的 JSON --args 会被截断或破坏（10-13
    踩坑：创建"成功"但 file_id/url 为空）。此时改用 node + mcporter 包内
    cli.js 直调，与经 npm shim 等价但不经批处理。找不到 node/cli.js 时回退
    原行为（调用方记日志）。"""
    argv_tail = ["call", service, tool, "--args", payload]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        cli_js = Path(os.path.dirname(os.path.abspath(exe))) / "node_modules" / "mcporter" / "dist" / "cli.js"
        node = shutil.which("node")
        if node and cli_js.is_file():
            return [node, str(cli_js)] + argv_tail
    return [exe] + argv_tail


def _tdoc_mcporter_call(task_cfg: dict, tool: str, args: dict,
                        logger: logging.Logger) -> dict:
    """直连 mcporter（不经 shell），返回解析出的 JSON；失败返回 {"error": ...}。"""
    exe = task_cfg.get("mcporter_path") or shutil.which("mcporter")
    if not exe:
        return {"error": "找不到 mcporter：请安装 mcporter 或在任务配置设置 mcporter_path"}
    service = task_cfg.get("tencent_service", "tencent-docs")
    payload = json.dumps(args, ensure_ascii=False)
    cmd = _mcporter_invocation(exe, service, tool, payload)
    logger.info(f"mcporter: {tool} ({len(payload)}B)")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"mcporter 调用异常: {exc}"}
    out = (r.stdout or r.stderr or "").strip()
    try:
        return json.loads(re.search(r"\{.*\}", out, re.S).group(0))
    except Exception:  # noqa: BLE001
        return {"error": (out or f"rc={r.returncode} 无输出")[:300]}


def _tdoc_chunk_lines(text: str, limit: int) -> list[str]:
    """按行累积分块（与 tr_daily.chunk_lines 同策略），单块约不超过 limit 字符。"""
    chunks, cur, size = [], [], 0
    for ln in text.splitlines():
        cur.append(ln)
        size += len(ln) + 1
        if size >= limit:
            chunks.append("\n".join(cur))
            cur, size = [], 0
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [""]


def _tdoc_policy_of(resp: dict) -> int:
    """get_privilege 响应中的 policy 转 int；缺失/非法返回 -1。"""
    try:
        return int(resp.get("policy"))
    except (TypeError, ValueError):
        return -1


def _tdoc_ensure_upload(task_cfg: dict, task_name: str, today_tr: str | None,
                        full_file: Path, logger: logging.Logger) -> tuple[str | None, str | None, str, str]:
    """上传完整报告为腾讯智能文档，返回 (file_id, url, err, warn)。

    同日同文件不得重复建文档：结果记录在 output/<date>_*.json（file_id/url），
    已存在则直接复用其 url（不调用 mcporter）。任一步失败返回 err 描述，
    不写状态文件、不推链接（由调用方 push_alarm，绝不静默）。
    warn 为"所有人可读"回读未确认的告警文本：不阻断链接推送，由调用方
    push_alarm 并记入状态文件 privilege_verified=false。"""
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    out_dir = skill_dir / task_cfg.get("output_dir", "output")
    pattern = task_cfg.get("tdoc_state_pattern", "{date}_tdoc.json")
    state_path = out_dir / pattern.format(date=today_tr) if today_tr else None
    if state_path and state_path.is_file():
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
            fid, url = str(st.get("file_id", "")), str(st.get("url", ""))
            if fid and url:
                logger.info(f"{task_name}: 复用已上传腾讯文档 {state_path.name} -> {url}")
                return fid, url, "", ""
            err = f"状态文件 {state_path} 缺少 file_id/url（file_id={fid or '空'}, url={url or '空'}），不重复建文档，请人工检查"
            logger.error(err)
            return None, None, err, ""
        except Exception as exc:  # noqa: BLE001
            err = f"读取状态文件 {state_path} 失败: {exc}；不重复建文档，请人工检查"
            logger.error(err)
            return None, None, err, ""
    if not today_tr:
        return None, None, "today_tr 未知，无法按日期命名/去重腾讯文档", ""

    title_prefix = task_cfg.get("tdoc_title_prefix") or task_cfg.get("title_prefix", task_name)
    doc_title = f"{title_prefix} · {today_tr}"
    limit = int(task_cfg.get("tdoc_chunk_limit", _TDOC_CHUNK_LIMIT))
    try:
        content = full_file.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        return None, None, f"读取完整报告失败: {exc}", ""
    chunks = _tdoc_chunk_lines(content, limit)

    d = _tdoc_mcporter_call(task_cfg, "create_smartcanvas_by_mdx",
                            {"title": doc_title, "mdx": chunks[0]}, logger)
    fid, url = str(d.get("file_id", "")), str(d.get("url", ""))
    if not fid:
        return None, None, f"腾讯文档创建失败（无 file_id）: {str(d)[:300]}", ""
    logger.info(f"{task_name}: 腾讯文档已创建 {doc_title} -> {url or d}")
    for i, ch in enumerate(chunks[1:], 2):
        d2 = _tdoc_mcporter_call(task_cfg, "smartcanvas.edit",
                                 {"file_id": fid, "action": "INSERT_AFTER", "content": ch}, logger)
        if d2.get("error"):
            return None, None, f"追加第 {i}/{len(chunks)} 块失败: {str(d2)[:200]}（文档 {fid} 内容不完整，未写状态文件）", ""
    # 所有人可读（policy=2），否则群里打不开链接。
    # 2026-10-13 对照腾讯文档开放平台官方 API 核验参数：MCP 工具 manage.set_privilege
    # 的 schema 为 policy:number（2-所有人可读，3-所有人可编辑），等价 REST 为
    # PATCH /openapi/drive/v2/files/{fileID}/permission (policy=publicRead，官方文档
    # "设置文档权限")。传字符串 "2" 会被服务端 400001 拒绝（已实测），必须传数值。
    dp = _tdoc_mcporter_call(task_cfg, "manage.set_privilege",
                             {"file_id": fid, "policy": 2}, logger)
    if dp.get("error"):
        return None, None, f"设置文档所有人可读失败: {str(dp)[:200]}（链接可能仅自己可见，未推送）", ""
    logger.info(f"{task_name}: manage.set_privilege 已受理（policy=2，trace_id={dp.get('trace_id', '')}）")
    # 设置后回读校验。注意（2026-10-13 实测）：manage.get_privilege 对本账号下所有
    # 文档——包括已匿名可访问的公开智能文档——恒返回 policy=0（私密文档同样返回 0，
    # 完全不 discriminating），官方文档亦声明该接口仅支持 0/1/2/3 四种场景、其余
    # 场景可能误报。故 policy!=2 时重试一次设置+回读（排除最终一致延迟）；仍 !=2
    # 记失败告警（调用方 push_alarm + 状态文件 privilege_verified=false），但不
    # 阻断链接推送——回读接口已被证明不可信时，硬失败会让每日任务必然失败。
    dg = _tdoc_mcporter_call(task_cfg, "manage.get_privilege", {"file_id": fid}, logger)
    if _tdoc_policy_of(dg) != 2:
        logger.warning(f"{task_name}: get_privilege 回读 policy={dg.get('policy')}（预期 2），"
                       "5 秒后重试设置并回读")
        time.sleep(5)
        _tdoc_mcporter_call(task_cfg, "manage.set_privilege",
                            {"file_id": fid, "policy": 2}, logger)
        dg = _tdoc_mcporter_call(task_cfg, "manage.get_privilege", {"file_id": fid}, logger)
    warn = ""
    if _tdoc_policy_of(dg) != 2:
        warn = (f"所有人可读回读校验未通过：get_privilege 返回 policy={dg.get('policy')}"
                f"（预期 2，已重试一次）。该接口对智能文档已知误报（公开文档也返回 0），"
                "链接仍推送，请人工打开确认。")
        logger.warning(f"{task_name}: {warn}")
    if state_path:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"file_id": fid, "url": url,
                                              "privilege_verified": not warn},
                                             ensure_ascii=False),
                                  encoding="utf-8")
            logger.info(f"{task_name}: 腾讯文档状态已记录 {state_path}")
        except Exception as exc:  # noqa: BLE001
            # 状态未落盘：下次运行会重复建文档，必须告警让人工补录
            return fid, url, f"上传成功但状态文件写入失败: {exc}（请人工补录 {state_path}，避免重复建文档）", warn
    return fid, url, "", warn


def _do_push_tdoc_link(cfg: dict, task_cfg: dict, task_name: str, today_tr: str | None,
                       logger: logging.Logger, started: float, log_dir: Path,
                       gen_status: str, title_prefix: str) -> dict:
    """腾讯文档链接推送分支：读技能 tr_data/build_state.json 中【当日】url，
    推送【标题】<日期>\\n腾讯文档：<url> 文本，不发送 .md 文件。
    build_state 无当日 url 时不推送任何内容，按推送失败记并告警（绝不推空链/旧链）。"""
    push_groups = task_cfg.get("push_groups", [])
    skill_dir = Path(task_cfg.get("run_cwd") or task_cfg.get("skill_dir"))
    state_rel = task_cfg.get("tdoc_state_file", "tr_data/build_state.json")
    state_path = skill_dir / state_rel
    today = dt.datetime.now().date().isoformat()
    url, prod_date = "", ""
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
        prod_date = str(st.get("prod_date", ""))
        url = str(st.get("url", ""))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"{task_name}: 读取腾讯文档状态失败 {state_path}: {exc}")
    text_ok = True
    if not url or prod_date != today:
        text_ok = False
        logger.error(f"{task_name}: build_state 无当日({today})腾讯文档链接 "
                     f"(prod_date={prod_date or '空'}, url={'有' if url else '空'})，不推送链接")
        push_alarm(cfg, logger, f"⚠️ {task_name} 文档链接缺失",
                   f"build_state({state_path}) 无当日 url，未推送，请人工检查上云步骤")
    else:
        msg = f"【{title_prefix}】{prod_date}\n腾讯文档：{url}"
        text_ok = push_content(cfg, task_cfg, push_groups, msg, logger)
        if not text_ok:
            push_alarm(cfg, logger, f"⚠️ {task_name} 链接推送失败", f"prod_date={prod_date}")
    if gen_status == "skipped_exists":
        status = "ok" if text_ok else "push_failed"
    elif gen_status == "ok_fallback":
        status = "ok_fallback" if text_ok else "push_failed"
    else:
        status = "ok" if text_ok else "push_failed"
    entry = {"task": task_name, "time": now_iso(),
             "status": status,
             "today_tr": today_tr,
             "tdoc_url": url if text_ok else None,
             "text_push": text_ok, "file_push": None,
             "elapsed_sec": round(time.time() - started, 1)}
    write_run_log(log_dir, entry)
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

    # 腾讯文档链接推送（仅对显式配置 push_tencent_doc_link=true 的任务生效，如 zh_flash）：
    # 不推送 .md 文件，改推技能 build_state.json 里【当日】prod_date 对应的文档链接。
    if task_cfg.get("push_tencent_doc_link"):
        return _do_push_tdoc_link(cfg, task_cfg, task_name, today_tr, logger,
                                  started, log_dir, gen_status, title_prefix)

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
        # 无简报版：发通知文本（标题 + 报告已生成提示）。
        # tdoc_upload 任务不再发 .md 文件，通知文案指向腾讯文档链接。
        where = "完整内容见下方腾讯文档链接。" if task_cfg.get("tdoc_upload") else "完整内容见文件。"
        notify = f"【{title_prefix}】{today_tr or ''} 报告已生成，{where}"
        text_ok = push_content(cfg, task_cfg, push_groups, notify, logger)
        if not text_ok:
            push_alarm(cfg, logger, f"⚠️ {task_name} 通知推送失败", "通知文本发送失败")

    file_ok = True
    tdoc_url: str | None = None
    if task_cfg.get("tdoc_upload"):
        # 通用腾讯文档分支（仅 tdoc_upload=true 的任务，如 aa_morning_briefing）：
        # 完整报告上传为腾讯智能文档（同日状态文件去重，绝不重复建文档），
        # 推送 "【标题】日期\n腾讯文档：<url>" 链接文本，不发送 .md 文件。
        # 与 zh_flash 的 push_tencent_doc_link 分支互不影响（该分支在前面已 return）。
        # 上传/权限/状态失败必须告警，不能静默。
        if full_file:
            fid, url, err, warn = _tdoc_ensure_upload(task_cfg, task_name, today_tr, full_file, logger)
            if err:
                logger.error(f"{task_name}: 腾讯文档上传失败: {err}")
                push_alarm(cfg, logger, f"⚠️ {task_name} 腾讯文档上传失败", err)
                file_ok = False
            else:
                if warn:
                    # 回读接口 get_privilege 对智能文档恒返回 0（公开文档同样返回 0，
                    # 2026-10-13 实测无区分度），此 warn 每日必触发且不可自愈；
                    # set_privilege 已受理即视为生效，链接经人工 10-13~10-19 多日
                    # 验证可正常打开。2026-10-19 起降级为日志，不再每日推送人工
                    # 确认告警；上传/设权限/状态文件/链接推送的真实失败仍走告警。
                    logger.info(f"{task_name}: {warn}")
                tdoc_url = url
                msg = f"【{title_prefix}】{today_tr}\n腾讯文档：{url}"
                file_ok = push_content(cfg, task_cfg, push_groups, msg, logger)
                if not file_ok:
                    push_alarm(cfg, logger, f"⚠️ {task_name} 文档链接推送失败",
                               f"file_id={fid}, url={url}")
        else:
            # 走到 _do_push 必有 full_file（调用点均保证），防御性告警
            file_ok = False
            push_alarm(cfg, logger, f"⚠️ {task_name} 腾讯文档上传失败", "完整报告文件缺失，无法上传")
    elif task_cfg.get("send_full_as_file", True) and full_file:
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
    if task_cfg.get("tdoc_upload"):
        entry["tdoc_url"] = tdoc_url
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
