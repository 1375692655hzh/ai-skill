#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Main entry: generate Turkey close-of-day report."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_brief_prompt import build_brief_prompt
from build_prompt import build_prompt
from check_source_date import is_content_for_date
from fetch_bloomberght import fetch_close_review
from fetch_info_yatirim import fetch_info_yatirim
from fetch_paraborsa import fetch_paraborsa
from fetch_live_quotes import fetch_live_quotes
from llm_runner import generate_brief_with_retry, generate_with_validation
from resolve_target_date import resolve_target_date, today_tr, is_trading_day_open
from runtime_utils import configure_stdio, resolve_paths
from source_header import prepend_header
from validate_brief_output import validate_brief
from validate_output import validate
from bht_fact_sheet import format_bloomberght_for_prompt, build_bht_fact_sheet


WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# BHT fallback/verification state files live in cache_dir.
VERIFY_PENDING_NAME = "{date}_bht_verify_pending.json"
VERIFY_PENDING_MAX_AGE_HOURS = 6  # ignore stale markers


def _verify_pending_path(cache_dir: Path, target_date: date) -> Path:
    return cache_dir / VERIFY_PENDING_NAME.format(date=target_date.isoformat())


def _safe_unlink(path: Path) -> None:
    """Delete a cache file, tolerating sandbox safe-delete failures.

    Some WorkBuddy sandboxes wrap file deletion in a safe-delete shim that can
    fail closed (e.g. Windows recycle-bin unavailable). A failed cache eviction
    must NOT abort report generation — the worst case is stale cache reuse,
    which is caught by the date-mismatch guard above. So swallow OSError here.
    """
    try:
        if path.exists():
            path.unlink()
    except (OSError, PermissionError) as exc:
        print(f"Warning: could not evict cache file {path.name}: {exc}", file=sys.stderr)


def load_config(config_path: Path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


def resolve_brief_template(skill_dir: Path, config: dict) -> Path | None:
    brief_cfg = config.get("brief", {})
    if not brief_cfg.get("enabled", True):
        return None
    rel = brief_cfg.get("template_path", "templates/close_report_brief_template.txt")
    candidates = [skill_dir / rel, Path(__file__).resolve().parent.parent / rel]
    for path in candidates:
        if path.is_file():
            return path
    return None


def format_bloomberght(data: dict, live_quotes: dict | None = None) -> str:
    """BHT close text: TL→亿里拉, strip clocks, plus Chinese fact card for four sections.

    live_quotes (optional): used to override the article's BIST 100 close/pct
    with the authoritative number from /piyasalar market page.
    """
    return format_bloomberght_for_prompt(data, live_quotes=live_quotes)


def format_paraborsa(data: dict) -> str:
    if not data.get("selected"):
        return "暂无市场观点数据。"
    sel = data["selected"]
    return f"{sel.get('content', '')[:8000]}"


def format_info_yatirim(data: dict) -> str:
    lines = []
    if data.get("daily", {}).get("content"):
        lines.append("【每日公告】")
        lines.append(data["daily"]["content"][:5000])
    if data.get("technical", {}).get("content"):
        lines.append("\n【技术分析】")
        lines.append(data["technical"]["content"][:5000])
    return "\n".join(lines) if lines else "暂无技术/公告数据。"


def _load_verify_pending(cache_dir: Path, target_date: date) -> dict | None:
    path = _verify_pending_path(cache_dir, target_date)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        created = data.get("created_at")
        if created:
            try:
                dt = datetime.fromisoformat(created)
                if datetime.now(timezone.utc) - dt > timedelta(
                    hours=VERIFY_PENDING_MAX_AGE_HOURS
                ):
                    _safe_unlink(path)
                    return None
            except ValueError:
                _safe_unlink(path)
                return None
        return data
    except Exception:
        return None


def _write_verify_pending(cache_dir: Path, target_date: date, payload: dict) -> None:
    path = _verify_pending_path(cache_dir, target_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"created_at": datetime.now(timezone.utc).isoformat(), **payload},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _remove_verify_pending(cache_dir: Path, target_date: date) -> None:
    _safe_unlink(_verify_pending_path(cache_dir, target_date))


def generate(
    config_path: Path,
    force_date: str | None = None,
    no_llm: bool = False,
    verify_bht: bool = False,
) -> Path | None:
    configure_stdio()
    config = load_config(config_path)
    skill_dir, workdir, output_dir, cache_dir, template_path = resolve_paths(
        config_path,
        config,
        default_template="templates/close_report_template.txt",
        default_cache=".cache/turkey-close-report",
    )
    os.chdir(workdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not template_path.is_file():
        print(f"Template not found: {template_path}", file=sys.stderr)
        return None

    holidays = config.get("holidays", [])
    target_date = resolve_target_date(force_date, holidays)
    today = today_tr()
    use_project_fetcher = config.get("sources", {}).get("bloomberght_closing", {}).get(
        "use_project_fetcher", False
    )

    if not is_trading_day_open(target_date, holidays):
        print(f"Target date {target_date} is a weekend or holiday. Skip.")
        return None

    print(f"Today (TR): {today}")
    print(f"Target date: {target_date}")

    pending = _load_verify_pending(cache_dir, target_date)

    # --verify-bht mode: only act if a previous run left a pending marker and
    # actually produced a fallback report. Otherwise there is nothing to fix.
    if verify_bht:
        if not pending:
            print(f"No BHT verification pending for {target_date}; nothing to do.")
            return None
        if not pending.get("fallback_generated"):
            print(f"No fallback report was generated for {target_date}; nothing to verify.")
            _remove_verify_pending(cache_dir, target_date)
            return None
        print(f"Verification pass: force-refreshing BHT cache for {target_date}")

    bloomberght = fetch_close_review(
        target_date,
        cache_dir,
        workdir=workdir,
        use_project_fetcher=use_project_fetcher,
        force_refresh=(verify_bht or (pending is not None and not pending.get("fallback_generated"))),
    )
    closing_text = bloomberght.get("closing_review", {}).get("text", "")
    if closing_text and not is_content_for_date(target_date, closing_text, "bloomberght"):
        print(f"Warning: closing review date mismatch, discarding cache for {target_date}")
        # Purge BOTH cache layers: the wrapper and the fetcher's internal cache,
        # otherwise fetch_closing_review silently re-reads the stale inner file.
        for fname in (
            f"bloomberght_close_{target_date.isoformat()}.json",
            f"bloomberght_closing_{target_date.isoformat()}.json",
        ):
            _safe_unlink(cache_dir / fname)
        bloomberght = fetch_close_review(
            target_date,
            cache_dir,
            workdir=workdir,
            use_project_fetcher=use_project_fetcher,
            force_refresh=verify_bht,
        )
    bht_missing = not bloomberght.get("ok")

    # If this is the verification pass and BHT is still missing, leave the
    # pending marker in place and exit without changing files. The runner will
    # retry again later (up to its max_retries).
    if verify_bht and bht_missing:
        print(f"Verification pass: BHT still not found for {target_date}; keeping pending marker")
        return None

    # If BHT is missing on the first pass, we still generate a fallback report
    # using live quotes + Paraborsa/Info when available. The runner will then
    # schedule a verification pass.
    is_fallback = bht_missing and not verify_bht
    if is_fallback:
        print(f"Fallback mode: generating close report without BHT article for {target_date}")

    # Live XU100 from /piyasalar overrides the close-review article's BIST 100
    # close/pct (article has been observed stale/mistyped; market page is
    # authoritative). Fetched unconditionally — after TR close the page shows
    # the same-day close; before close it shows prior close, which still
    # corrects stale article numbers.
    live_quotes = fetch_live_quotes(cache_dir)
    if not live_quotes.get("ok"):
        print(f"Warning: live quotes (XU100) fetch failed: {live_quotes.get('error')}")
    # The /borsa snapshot is authoritative for today's close only. When a
    # historical date is forced, keep the article's BIST/stock facts instead
    # of accidentally injecting the current market page.
    live_quotes_for_target = live_quotes
    # In fallback mode we still want today's live quote facts (we are after
    # market close, so /borsa already reflects the target trading day).
    if target_date != today and not is_fallback:
        live_quotes_for_target = dict(live_quotes)
        live_quotes_for_target["quotes"] = dict(live_quotes.get("quotes") or {})
        live_quotes_for_target["quotes"].pop("XU100", None)
        live_quotes_for_target["borsa"] = {}
        live_quotes_for_target["borsa_fact_cn"] = ""

    paraborsa = fetch_paraborsa(target_date, cache_dir)
    selected_content = paraborsa.get("selected", {}).get("content", "")
    selected_title = paraborsa.get("selected", {}).get("title", "")
    if selected_content and not is_content_for_date(
        target_date, selected_title + " " + selected_content, "paraborsa"
    ):
        print(f"Warning: commentary date mismatch, discarding cache for {target_date}")
        cache_file = cache_dir / f"paraborsa_{target_date.isoformat()}.json"
        _safe_unlink(cache_file)
        paraborsa = fetch_paraborsa(target_date, cache_dir)

    info_yatirim = fetch_info_yatirim(target_date, cache_dir)
    daily_content = info_yatirim.get("daily", {}).get("content", "")
    if daily_content and not is_content_for_date(target_date, daily_content, "info_yatirim"):
        print(f"Warning: bulletin date mismatch, discarding cache for {target_date}")
        cache_file = cache_dir / f"info_yatirim_{target_date.isoformat()}.json"
        _safe_unlink(cache_file)
        info_yatirim = fetch_info_yatirim(target_date, cache_dir)

    weekday_cn = WEEKDAYS_CN[target_date.weekday()]
    prompt = build_prompt(
        template_path=template_path,
        today_date=target_date.isoformat(),
        target_date=target_date.isoformat(),
        weekday_cn=weekday_cn,
        bloomberght_text=format_bloomberght(
            bloomberght,
            live_quotes=live_quotes_for_target,
        ),
        paraborsa_text=format_paraborsa(paraborsa),
        info_yatirim_text=format_info_yatirim(info_yatirim),
    )

    prompt_file = cache_dir / f"close_prompt_{target_date.isoformat()}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    print(f"Prompt saved to: {prompt_file}")

    if no_llm:
        return prompt_file

    llm_cfg = config["llm"]
    # Build the BHT fact card once and pass it to the validator as the
    # data-provenance fingerprint, so fabricated numbers/percentages are caught.
    # Must use the same live XU100 index_override as the prompt, otherwise the
    # model writes live numbers while the validator fingerprints the article.
    index_override = None
    if live_quotes_for_target and live_quotes_for_target.get("quotes", {}).get("XU100"):
        if live_quotes_for_target.get("xu100_status") == "after_close":
            xu = live_quotes_for_target["quotes"]["XU100"]
            index_override = {"last": xu.get("last"), "pct": xu.get("pct")}
    # The provenance fingerprint must cover EVERY number the prompt legitimately
    # offers for the four fact sections — not just the parsed fact sheet.
    # 2026-09-04: THYAO turnover appeared as 204.99亿里拉 in the normalized
    # article text (20.499.267.628 TL) but 205亿里拉 in the live /borsa snapshot
    # (20.499.704.878 TL → :.2f rounds up). The LLM quoted the article figure,
    # the fingerprint only knew the snapshot figure → false-positive
    # "numbers not found in BHT source". So fingerprint = sheet + normalized
    # article text + live borsa card (all three feed the fact sections).
    from bht_fact_sheet import normalize_bht_text
    # In fallback mode the BHT article is absent; use live quote cards as the
    # provenance fingerprint so numbers still get validated.
    source_facts_parts: list[str] = []
    if bloomberght.get("ok"):
        source_facts_parts.append(
            build_bht_fact_sheet(
                bloomberght.get("closing_review", {}).get("text", ""),
                index_override=index_override,
                live_borsa=(
                    live_quotes_for_target.get("borsa") or {}
                    if live_quotes_for_target.get("xu100_status") == "after_close"
                    else {}
                ),
            )
        )
        cr_text = bloomberght.get("closing_review", {}).get("text", "")
        if cr_text:
            source_facts_parts.append(normalize_bht_text(cr_text))
    if live_quotes_for_target.get("xu100_status") == "after_close":
        source_facts_parts.append(live_quotes_for_target.get("borsa_fact_cn") or "")
    if not bloomberght.get("ok"):
        source_facts_parts.append(live_quotes_for_target.get("fact_cn") or "")
    source_facts = "\n".join(p for p in source_facts_parts if p) or None
    validate_with_source = (
        (lambda text: validate(text, source_facts=source_facts))
        if source_facts
        else validate
    )
    content, result = generate_with_validation(prompt, llm_cfg, validate_with_source)
    if content is None or not result.get("ok"):
        if content:
            raw_output = cache_dir / f"close_raw_output_{target_date.isoformat()}.txt"
            raw_output.write_text(content, encoding="utf-8")
        print(f"Validation failed: {result.get('errors', [])}")
        if content:
            print(f"Raw output saved to: {raw_output}")
        return None

    if result.get("warnings"):
        print(f"Validation warnings: {result['warnings']}")

    # 只要换行、不要空行：折叠多余空行
    content = re.sub(r"\n{2,}", "\n", content.replace("\r\n", "\n")).strip() + "\n"

    # 数据来源元数据：每个抓取源的 URL + 状态
    cr = bloomberght.get("closing_review") or {}
    bht_url = cr.get("url") or "https://www.bloomberght.com/borsa/kapanis"
    bht_detail = ""
    if cr.get("article_id"):
        bht_detail = f"文章 id={cr['article_id']}"
    elif bloomberght.get("ok"):
        bht_detail = "已抓取收盘综述"
    xu = (live_quotes_for_target.get("quotes") or {}).get("XU100") or {}
    xu_status = live_quotes_for_target.get("xu100_status") or ""
    xu_detail = ""
    if xu:
        xu_detail = f"XU100={xu.get('last')} ({xu.get('pct')}%) [{xu_status}]"
    par_sel = paraborsa.get("selected") or {}
    iy_daily = info_yatirim.get("daily") or {}
    iy_tech = info_yatirim.get("technical") or {}
    sources = [
        {
            "name": "BHT 收评文章",
            "url": bht_url,
            "status": "ok" if bloomberght.get("ok") else "fail",
            "detail": bht_detail or (bloomberght.get("error") or ""),
        },
        {
            "name": "BHT 实时行情 /piyasalar",
            "url": "https://www.bloomberght.com/piyasalar",
            "status": "ok" if live_quotes.get("piyasalar_ok") else "fail",
            "detail": xu_detail or (live_quotes.get("error") or ""),
        },
        {
            "name": "BHT 实时行情 /borsa",
            "url": "https://www.bloomberght.com/borsa/",
            "status": "ok" if live_quotes.get("borsa_ok") else "fail",
            "detail": (
                "XU100/个股实时快照"
                if live_quotes.get("borsa_ok")
                else "未解析到 /borsa 快照"
            ),
        },
        {
            "name": "Paraborsa 市场评论",
            "url": par_sel.get("url") or "https://www.paraborsa.com/",
            "status": "ok" if par_sel.get("content") else "fail",
            "detail": (par_sel.get("title", "")[:40] or paraborsa.get("error") or ""),
        },
        {
            "name": "Info Yatırım 每日简报",
            "url": iy_daily.get("url") or "https://infoyatirim.com/arastirma/gunluk-bulten",
            "status": "ok" if iy_daily.get("content") else "fail",
            "detail": (info_yatirim.get("reason") or ""),
        },
    ]
    if iy_tech.get("content"):
        sources.append({
            "name": "Info Yatırım 技术简报",
            "url": iy_tech.get("url") or "https://infoyatirim.com/arastirma/teknik-bulten",
            "status": "ok",
        })
    title = f"土耳其股市收评 — {target_date.isoformat()}（{weekday_cn}）"
    if is_fallback:
        title = f"土耳其股市收评（临时版） — {target_date.isoformat()}（{weekday_cn}）"
    content = prepend_header(content, sources, title=title)

    # Fallback reports use a distinct file suffix so the runner can distinguish
    # them and schedule a verification pass; final reports overwrite the normal
    # filename.
    if is_fallback:
        output_file = output_dir / f"{target_date.isoformat()}_close_report_fallback_zh.md"
        _write_verify_pending(
            cache_dir,
            target_date,
            {
                "fallback_generated": True,
                "fallback_file": str(output_file),
                "bht_error": bloomberght.get("error", ""),
            },
        )
    else:
        output_file = output_dir / f"{target_date.isoformat()}_close_report_zh.md"
        _remove_verify_pending(cache_dir, target_date)
        # Clean up a previously published fallback file so downstream tooling
        # only sees the authoritative final report.
        fb_file = output_dir / f"{target_date.isoformat()}_close_report_fallback_zh.md"
        _safe_unlink(fb_file)
        fb_brief = output_dir / f"{target_date.isoformat()}_close_report_fallback_brief_zh.md"
        _safe_unlink(fb_brief)

    output_file.write_text(content, encoding="utf-8")
    print(f"Close report written to: {output_file}")

    brief_template = resolve_brief_template(skill_dir, config)
    if brief_template:
        brief_cfg = config.get("brief", {})
        brief_prompt = build_brief_prompt(
            brief_template,
            target_date.isoformat(),
            weekday_cn,
            content,
        )
        brief_llm_cfg = {
            **llm_cfg,
            "max_tokens": brief_cfg.get("max_tokens", 1200),
            "temperature": brief_cfg.get("temperature", 0.3),
        }
        brief_output, brief_result = generate_brief_with_retry(
            brief_prompt,
            brief_llm_cfg,
            lambda text: validate_brief(
                text,
                min_chars=brief_cfg.get("min_chars", 200),
                max_chars=brief_cfg.get("max_chars", 600),
            ),
            fix_hint=(
                "首行必须是【土耳其股市收评简报 — 日期（周x）】；"
                "字段顺序固定：【指数】【汇率】【驱动】【个股】【板块】【信号】【操作】【风险】；"
                "【个股】标签独占一行，其后 3–5 只个股，每只一行，格式「代码 涨跌/要点」；"
                "禁止列表符号、Markdown、表格、Emoji；"
                "篇幅 200–600 个汉字+中文标点（英文代码、数字不计入）；"
                "只要换行、不要空行。"
                f"{'当前为临时版（BHT收盘综述未发布），简报正文须明确标注“临时版”或“待BHT收盘综述发布后更新”。' if is_fallback else ''}"
            ),
        )
        if is_fallback:
            brief_file = output_dir / f"{target_date.isoformat()}_close_report_fallback_brief_zh.md"
        else:
            brief_file = output_dir / f"{target_date.isoformat()}_close_report_brief_zh.md"
            # Remove stale fallback brief if final succeeded.
            _safe_unlink(
                output_dir / f"{target_date.isoformat()}_close_report_fallback_brief_zh.md"
            )
        if brief_output and brief_result.get("ok"):
            brief_file.write_text(brief_output, encoding="utf-8")
            print(f"Brief close report written to: {brief_file}")
            if brief_result.get("warnings"):
                print(f"Brief warnings (non-blocking): {brief_result['warnings']}", file=sys.stderr)
        else:
            print(f"Brief validation failed: {brief_result.get('errors', [])}", file=sys.stderr)
            if brief_output:
                raw_brief = cache_dir / f"close_raw_brief_{target_date.isoformat()}.txt"
                raw_brief.write_text(brief_output, encoding="utf-8")
                print(f"Raw brief saved to: {raw_brief}", file=sys.stderr)

    return output_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Turkey close-of-day report")
    parser.add_argument("--config", type=Path, required=True, help="Path to config.json")
    parser.add_argument("--force-date", type=str, default=None, help="Force target date (YYYY-MM-DD)")
    parser.add_argument("--no-llm", action="store_true", help="Only build prompt, do not call LLM")
    parser.add_argument(
        "--verify-bht",
        action="store_true",
        help="Second-pass verification: force-refresh BHT and regenerate if found",
    )
    args = parser.parse_args()

    try:
        generate(
            args.config,
            force_date=args.force_date,
            no_llm=args.no_llm,
            verify_bht=args.verify_bht,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
