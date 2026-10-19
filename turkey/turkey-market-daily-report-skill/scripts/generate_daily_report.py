#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turkey market daily hotspots report — aggregate upstream skill outputs
(same-day X influencers + previous-trading-day Paraborsa) into a two-chapter
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
    # 编排器从 stderr 抓这个标记锚定 today_tr（runner.parse_today_tr）
    _log(f"Today (TR): {bundle['target_date'].isoformat()}")
    _log("Input bundle:\n" + _input_report(bundle))

    missing = bundle["missing_core"]
    if "x_influencer" in missing:
        _log(
            f"核心输入不足（missing={missing}）。规范要求：X大V全文必须有；"
            "不生成虚假热点/个股表，请先跑对应上游 skill。"
        )
        return 2

    target = bundle["target_date"]
    md_path = output_dir / f"{target.isoformat()}_market_daily_zh.md"

    docx_cfg = cfg.get("docx", {})
    docx_name = docx_cfg.get("filename", "turkey_daily_{yyyymmdd}.docx").replace(
        "{yyyymmdd}", target.strftime("%Y%m%d")
    )
    docx_path = output_dir / docx_name
    # Skip only when BOTH artifacts exist: a QC-failed docx left on disk must
    # not let a retry round claim success (2026-10-10 audit P1-3).
    if (
        md_path.exists()
        and docx_path.exists()
        and not args.force_refresh
        and not args.no_llm
        and not args.md_only
    ):
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
    from validate_output import strip_no_view_blocks, validate

    content, result = generate_with_validation(prompt, llm_cfg, validate)
    if content is None:
        _log(f"Validation failed: {result.get('errors', [])}")
        return 1
    if not result.get("ok"):
        # Deterministic salvage: no-view blocks (pure level lists / index rows /
        # placeholder fields) are stripped in code instead of burning more LLM
        # retries — the model keeps re-including bulk broker sweeps.
        strippable = ("纯技术位罗列", "不得进入个股分析", "个股块首行缺少", "个股块缺少观点行")
        only_title = all("missing report title" in e for e in result.get("errors", [])) and "土耳其股市热点日报" in content
        if only_title:
            # The model sometimes fences the title in ``` or drops the "# " prefix.
            import re as _re

            def _fix_title(m):
                return "# " + m.group(1).strip("` \n")

            content = _re.sub(r"```+\s*\n?土耳其股市热点日报[｜|][^\n`]*```?", _fix_title, content, count=1)
            if "# 土耳其股市热点日报" not in content:
                content = _re.sub(r"(?m)^(?#!)(土耳其股市热点日报[｜|][^\n]*)$", r"# \1", content, count=1)
            result = validate(content)
            _log(f"Title normalized; re-validate ok={result.get('ok')}")
        elif all(any(marker in e for marker in strippable) for e in result.get("errors", [])):
            pre_strip = content
            content, removed_blocks = strip_no_view_blocks(content)
            result = validate(content)
            # Audit trail (K3 P0): a wrong kill must be recoverable — persist the
            # pre-strip original and every removed block verbatim.
            (cache_dir / f"daily_raw_{target.isoformat()}_pre_strip.txt").write_text(pre_strip, encoding="utf-8")
            if removed_blocks:
                audit = "\n\n".join(f"=== 被剔除块 {i + 1} ===\n{b}" for i, b in enumerate(removed_blocks))
                (cache_dir / f"no_view_stripped_{target.isoformat()}.txt").write_text(audit, encoding="utf-8")
            _log(
                f"Deterministic strip: -{len(removed_blocks)} no-view blocks "
                f"(audit: no_view_stripped_{target.isoformat()}.txt); re-validate ok={result.get('ok')}"
            )
        if not result.get("ok"):
            (cache_dir / f"daily_raw_{target.isoformat()}.txt").write_text(content, encoding="utf-8")
            compact_len = len("".join(content.split()))
            if compact_len < 300:
                _log(f"Output too short ({compact_len} chars) — LLM 异常输出，按失败重试: {result.get('errors', [])}")
                return 1
            # 交付优先（用户铁律 2026-10-19）：格式/数据缺口只诊断不拦截，照常落盘推送
            _log("Validation issues (non-blocking, delivered anyway): " + str(result.get("errors", [])))
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

    markdown_to_docx(content, docx_path, font=str(docx_cfg.get("font", "Noto Sans CJK SC")))
    qc = qc_docx(docx_path, expect_stock_table=(result.get("stock_rows", 0) > 0))
    if qc["ok"]:
        _log(f"DOCX written + QC passed: {docx_path} (stock_rows={qc['stock_rows']})")
    else:
        # Delete the bad artifact: a QC-failed docx on disk would let the
        # orchestrator's locate_outputs treat today as done (2026-10-10 audit P1-3).
        try:
            docx_path.unlink()
        except OSError:
            pass
        _log(f"DOCX QC FAILED: {qc['errors']} — bad file deleted, fix and re-run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
