#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turkey market daily hotspots report — aggregate upstream skill outputs
(same-day Info/X/AA + previous-trading-day close/Paraborsa) into a two-chapter
Chinese daily report, validated and rendered to .docx."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from runtime_utils import configure_stdio, resolve_paths
from resolve_inputs import collect_inputs


def _log(msg: str) -> None:
    print(f"[{__import__('datetime').datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _load_env_file(skill_dir: Path) -> None:
    env_path = skill_dir / ".env"
    if not env_path.exists():
        return
    import os

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _input_report(bundle: dict) -> str:
    lines = [
        f"报告日期: {bundle['target_date']} | 前一交易日: {bundle['prev_trading_day']}",
        f"当日素材: {sorted(bundle['same_day']) or '无'}",
        f"前交易日素材: {sorted(bundle['prev_day']) or '无'}",
        f"缺失核心: {bundle['missing_core'] or '无'} | 缺失前日: {bundle['missing_prev'] or '无'} | 缺失可选: {bundle['missing_optional'] or '无'}",
    ]
    return "\n".join(lines)


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Generate Turkey daily hotspots report.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--no-llm", action="store_true", help="Locate inputs + build prompt only.")
    parser.add_argument("--force-refresh", action="store_true", help="Overwrite existing outputs.")
    parser.add_argument("--md-only", action="store_true", help="Skip docx rendering.")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    skill_dir, workdir, output_dir, cache_dir, template_path = resolve_paths(
        config_path, cfg,
        default_template="templates/generate_template.txt",
        default_cache=".cache/turkey-market-daily-report",
    )
    _load_env_file(skill_dir)

    bundle = collect_inputs(cfg)
    _log("Input bundle:\n" + _input_report(bundle))

    missing = bundle["missing_core"]
    has_anchor = (not bundle["same_day"].get("info_daily")) is False or (
        not bundle["same_day"].get("morning_briefing")
    ) is False
    if "x_influencer" in missing or not has_anchor:
        _log(
            f"核心输入不足（missing={missing}）。规范要求：X大V全文必须有；"
            "Info日报与早报至少其一（同事锚定顺序 info_daily→daily_briefing）。"
            "不生成虚假热点/个股表，请先跑对应上游 skill。"
        )
        return 2

    target = bundle["target_date"]
    md_path = output_dir / f"{target.isoformat()}_market_daily_zh.md"

    if md_path.exists() and not args.force_refresh and not args.no_llm:
        _log(f"{md_path.name} 已存在 — 用 --force-refresh 重做。")
        return 0

    from build_prompt import build_prompt

    prompt = build_prompt(template_path=template_path, target=target, bundle=bundle)
    cache_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = cache_dir / f"prompt_{target.isoformat()}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    _log(f"Prompt saved: {prompt_file} ({len(prompt)} chars)")

    if args.no_llm:
        _log("no_llm=true — stopping after prompt build.")
        return 0

    llm_cfg = dict(cfg["llm"])
    import os

    api_key_env = llm_cfg.get("api_key_env", "MINIMAX_API_KEY")
    if not os.environ.get(api_key_env, "").strip():
        _log(f"Missing {api_key_env} — set it in env or skill .env (fail fast).")
        return 2

    from llm_runner import generate_with_validation
    from validate_output import validate

    content, result = generate_with_validation(prompt, llm_cfg, validate)
    if content is None or not result.get("ok"):
        if content:
            (cache_dir / f"daily_raw_{target.isoformat()}.txt").write_text(content, encoding="utf-8")
        _log(f"Validation failed: {result.get('errors', [])}")
        return 1
    if result.get("warnings"):
        _log(f"Validation warnings: {result['warnings']}")
    _log(f"hotspots={result.get('hotspots')} stock_rows={result.get('stock_rows')}")

    import re

    content = re.sub(r"\n{3,}", "\n\n", content.replace("\r\n", "\n")).strip() + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content, encoding="utf-8")
    _log(f"Markdown written: {md_path}")

    if args.md_only:
        return 0

    from make_docx import markdown_to_docx, qc_docx

    docx_cfg = cfg.get("docx", {})
    docx_name = docx_cfg.get("filename", "turkey_daily_{yyyymmdd}.docx").replace(
        "{yyyymmdd}", target.strftime("%Y%m%d")
    )
    docx_path = output_dir / docx_name
    markdown_to_docx(content, docx_path, font=str(docx_cfg.get("font", "Noto Sans CJK SC")))
    qc = qc_docx(docx_path)
    if qc["ok"]:
        _log(f"DOCX written + QC passed: {docx_path} (stock_rows={qc['stock_rows']})")
    else:
        _log(f"DOCX QC FAILED: {qc['errors']} — file at {docx_path} needs a fix pass.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
