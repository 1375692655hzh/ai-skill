#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turkey flash-news skill — ingest + curated Chinese digest.

Two modes:
  --ingest-only   fetch all sources into the store (cheap, cron every 30 min)
  default         ingest → window read → prefilter → LLM digest → outputs

Window start = the EARLIER of (last_digest_ts - 5min, now - window_hours), clamped to 72h;
minus already-delivered ids — nothing repeats, nothing is lost across a
skipped Sunday (the window automatically widens on Monday).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_utils import configure_stdio, resolve_paths
from store import FlashStore, iso, parse_iso, stable_id, utc_now

TRT = timezone(timedelta(hours=3))


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

def ingest_all(store: FlashStore, cfg: dict) -> dict:
    sources_cfg = cfg.get("sources", {})
    per_source: dict[str, dict] = {}
    new_total = 0

    # --- KAP ---
    if sources_cfg.get("kap", {}).get("enabled", True):
        lookback_days = int(sources_cfg.get("kap", {}).get("ingest_lookback_days", 3))
        to_date = datetime.now(TRT).date()
        from_date = to_date - timedelta(days=lookback_days)
        try:
            from fetch_kap import fetch_kap_disclosures

            items = fetch_kap_disclosures(from_date=from_date.isoformat(), to_date=to_date.isoformat())
            new = store.append(items)
            per_source["kap"] = {"ok": True, "fetched": len(items), "new": new}
            _log(f"[kap] fetched={len(items)} new={new}")
        except Exception as exc:
            per_source["kap"] = {"ok": False, "error": str(exc)[:200]}
            _log(f"[kap] FAILED: {str(exc)[:160]}")

    # --- TCMB press ---
    if sources_cfg.get("tcmb_press", {}).get("enabled", True):
        try:
            from fetch_tcmb import fetch_tcmb_press

            items = fetch_tcmb_press()
            new = store.append(items)
            per_source["tcmb_press"] = {"ok": True, "fetched": len(items), "new": new}
            _log(f"[tcmb_press] fetched={len(items)} new={new}")
        except Exception as exc:
            per_source["tcmb_press"] = {"ok": False, "error": str(exc)[:200]}
            _log(f"[tcmb_press] FAILED: {str(exc)[:160]}")

    # --- RSS pool ---
    for feed in sources_cfg.get("rss", []):
        if not feed.get("enabled", True):
            continue
        try:
            from fetch_rss import fetch_feed

            items = fetch_feed(
                source_id=str(feed.get("id")),
                url=str(feed.get("url")),
                lang=str(feed.get("lang", "tr")),
                max_items=int(feed.get("max_items", 120)),
            )
            new = store.append(items)
            per_source[str(feed.get("id"))] = {"ok": True, "fetched": len(items), "new": new}
            _log(f"[{feed.get('id')}] fetched={len(items)} new={new}")
        except Exception as exc:
            per_source[str(feed.get("id"))] = {"ok": False, "error": str(exc)[:200]}
            _log(f"[{feed.get('id')}] FAILED: {str(exc)[:160]}")

    # --- BloombergHT breaking headlines (fresh live ticker) ---
    if sources_cfg.get("bht_headlines", {}).get("enabled", True):
        try:
            from fetch_bloomberght_closing import fetch_today_headlines

            today_tr = datetime.now(TRT).date()
            heads = fetch_today_headlines(today_tr)
            items = []
            now_iso = iso(utc_now())
            for bucket, kind in (("breaking_news", "breaking"), ("featured_news", "featured")):
                for h in heads.get(bucket) or []:
                    title = re.sub(r"\s+", " ", str(h.get("title") or "")).strip()
                    if not title:
                        continue
                    # Prefer the resolved publish date (featured items carry one);
                    # undated live ticker items fall back to first-seen time.
                    ts_iso = now_iso
                    pub = str(h.get("published_date") or "")[:10]
                    if pub:
                        try:
                            ts_iso = (
                                datetime.fromisoformat(pub)
                                .replace(hour=12, tzinfo=TRT)
                                .astimezone(timezone.utc)
                                .isoformat()
                            )
                        except ValueError:
                            pass
                    items.append(
                        {
                            "id": stable_id("bht", h.get("url") or title),
                            "source": "bht",
                            "kind": "headline",
                            "bucket": kind,
                            "ts": ts_iso,
                            "raw_time": str(h.get("published_date") or ""),
                            "title": title,
                            "body": title,
                            "url": str(h.get("url") or ""),
                            "fetched_at": now_iso,
                        }
                    )
            new = store.append(items)
            per_source["bht"] = {"ok": True, "fetched": len(items), "new": new}
            _log(f"[bht] fetched={len(items)} new={new}")
        except Exception as exc:
            per_source["bht"] = {"ok": False, "error": str(exc)[:200]}
            _log(f"[bht] FAILED: {str(exc)[:160]}")

    # Health snapshot lives in a SEPARATE file: the ingest-only cron can never
    # clobber the digest checkpoint in state.json (audit H2/H3).
    ingest_state = {}
    if store.ingest_state_file.exists():
        try:
            ingest_state = json.loads(store.ingest_state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ingest_state = {}
    streaks = ingest_state.get("fail_streaks", {})
    for name, info in per_source.items():
        key = name.lower()
        streaks[key] = 0 if info.get("ok") else streaks.get(key, 0) + 1
    ingest_state["last_ingest_at"] = iso(utc_now())
    ingest_state["ingest"] = per_source
    ingest_state["fail_streaks"] = streaks
    for name, n in streaks.items():
        if n >= 6:  # ~3h of consecutive failures at 30-min cadence
            _log(f"WARNING: source '{name}' failed {n} consecutive ingests — check health")
    # Once a day: drop store items older than 35 days (windows never exceed 72h).
    today = datetime.now(TRT).strftime("%Y-%m-%d")
    if ingest_state.get("last_prune_date") != today:
        dropped = store.prune()
        if dropped:
            _log(f"store pruned: -{dropped} items older than 35d")
        ingest_state["last_prune_date"] = today
    store.save_ingest_state(ingest_state)

    new_total = sum(v.get("new", 0) for v in per_source.values())
    return {"sources": per_source, "new": new_total}


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def run_digest(
    store: FlashStore,
    cfg: dict,
    paths: tuple,
    *,
    force_window_hours=None,
    force_refresh=False,
) -> int:
    from prefilter import prefilter
    from build_prompt import build_prompt
    from validate_output import validate
    from validate_brief_output import validate_brief
    from llm_runner import generate_with_validation, generate_brief_with_retry
    from call_llm import call_llm

    skill_dir, workdir, output_dir, cache_dir, template_path = paths
    window_hours = float(force_window_hours or cfg.get("window_hours", 24))
    min_items = int(cfg.get("min_items_for_digest", 5))

    if force_window_hours:
        # Event-day short window: strictly [now - hours, now]; cursor bypassed
        # and delivered ignored so the short run really is short (audit H2).
        window_end = utc_now()
        window_start = window_end - timedelta(hours=force_window_hours)
        delivered: set[str] = set()
    else:
        state = store.load_state()
        delivered = set(state.get("delivered_ids") or [])
        if force_refresh:
            delivered = set()
        window_start, window_end = store.resolve_window(window_hours=window_hours)
    _log(
        f"Window: {window_start.astimezone(TRT).strftime('%m-%d %H:%M')} → "
        f"{window_end.astimezone(TRT).strftime('%m-%d %H:%M')} TRT "
        f"({round((window_end - window_start).total_seconds()/3600, 1)}h), "
        f"delivered-excluded={len(delivered)}"
    )

    items = store.read_window(window_start, window_end, exclude_ids=delivered)
    # Items without ts (undated) can't be placed in a window; skip them.
    if len(items) < min_items:
        _log(f"Only {len(items)} items in window (< {min_items}) — nothing to digest.")
        return 0

    cards = prefilter(
        items,
        noise_patterns=cfg.get("prefilter", {}).get("noise_patterns"),
        p0_patterns=cfg.get("prefilter", {}).get("p0_patterns"),
        kap_cap=int(cfg.get("prefilter", {}).get("kap_cap", 120)),
        rss_cap_per_source=int(cfg.get("prefilter", {}).get("rss_cap_per_source", 40)),
    )
    from prefilter import enrich_kap_with_rss, is_label_only

    enriched = enrich_kap_with_rss(cards["kap"], cards["rss"])
    label_only_n = sum(1 for k in cards["kap"] if is_label_only(k) and not k.get("enriched"))
    _log(
        f"Prefilter: kap={len(cards['kap'])} rss={len(cards['rss'])} "
        f"tcmb={len(cards['tcmb'])} (from {len(items)} raw) | "
        f"wire-enriched={enriched} label-only={label_only_n}"
    )

    # Fresh FX fixing snapshot at generation time (fact card, not event stream).
    fx = {"ok": False, "rates": {}, "date": None}
    if cfg.get("sources", {}).get("tcmb_fx", {}).get("enabled", True):
        try:
            from fetch_tcmb import fetch_tcmb_fx

            fx = fetch_tcmb_fx()
        except Exception as exc:
            _log(f"[tcmb_fx] failed (non-blocking): {str(exc)[:120]}")

    date_label = window_end.astimezone(TRT).strftime("%Y-%m-%d")
    calendar_events = _calendar_in_scope(cfg, window_end)
    prompt = build_prompt(
        template_path=template_path,
        date_label=date_label,
        window_start=window_start,
        window_end=window_end,
        kap_items=cards["kap"],
        rss_items=cards["rss"],
        tcmb_items=cards["tcmb"],
        fx_snapshot=fx,
        stats={
            "kap_total": len(cards["kap"]),
            "rss_total": len(cards["rss"]),
            "tcmb_total": len(cards["tcmb"]),
        },
        calendar_events=calendar_events,
    )
    prompt_file = cache_dir / f"prompt_{date_label}.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt, encoding="utf-8")
    _log(f"Prompt saved: {prompt_file} ({len(prompt)} chars)")

    if cfg.get("no_llm"):
        _log("no_llm=true — stopping after prompt build.")
        return 0

    llm_cfg = dict(cfg.get("llm") or {})
    api_key_env = llm_cfg.get("api_key_env", "MINIMAX_API_KEY")
    import os

    if not os.environ.get(api_key_env, "").strip():
        raise RuntimeError(
            f"Missing {api_key_env}. Set it in the environment or skill .env "
            "before generating the digest (fail fast, no silent 401)."
        )

    from validate_output import SOFT_ERROR_MARKERS

    cal_dates = [str(ev.get("date", "")) for ev in (calendar_events or [])]
    if force_window_hours:
        def validate_fn(text: str) -> dict:
            return validate(text, min_items=3, min_chars=300, calendar_dates=cal_dates)
    else:
        def validate_fn(text: str) -> dict:
            return validate(text, calendar_dates=cal_dates)

    content, result = generate_with_validation(prompt, llm_cfg, validate_fn)
    soft_failure = bool(content) and bool(result.get("errors")) and all(
        any(marker in e for marker in SOFT_ERROR_MARKERS) for e in result["errors"]
    )
    if content is None or not (result.get("ok") or soft_failure):
        if content:
            (cache_dir / f"flash_raw_output_{date_label}.txt").write_text(content, encoding="utf-8")
        _log(f"Full digest validation failed: {result.get('errors', [])}")
        return 1
    if soft_failure:
        _log(
            "WARNING: attribution/star-marker issues survived the rewrite retry — "
            f"shipping the digest with this warning instead of dropping it: {result['errors']}"
        )
    if result.get("warnings"):
        _log(f"Validation warnings: {result['warnings']}")

    content = _format_sections(content)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Event-day short runs get their own file; the main daily digest is never
    # overwritten by them (audit H2/M4).
    name_suffix = "_event" if force_window_hours else ""
    out_file = output_dir / f"{date_label}_flash_news{name_suffix}_zh.md"
    out_file.write_text(content, encoding="utf-8")
    _log(f"Full digest written: {out_file}")

    # ---- brief (push) version ----
    if cfg.get("brief", {}).get("enabled", True):
        brief_cfg = cfg.get("brief", {})
        brief_prompt = (
            prompt
            + "\n\n【第二任务】基于上述快讯内容，另写一份极简推送简报，格式：\n"
            "首行【土耳其快讯简报 — "
            f"{date_label}】；随后每个【字段】独占一行，顺序固定："
            "【头条】（最重要的1–2件事，一句带数字）、【公告】（最关键 KAP 一句）、"
            "【数据】（央行/牌价一句）、【关注】（下一窗口盯什么）、【风险】（一句）。\n"
            "禁止列表符号、Markdown、表格、Emoji；只要换行不要空行；"
            "正文（不含标题行）按汉字计 200–400 字。只输出简报本身。"
        )
        brief_llm = dict(llm_cfg)
        brief_llm["max_tokens"] = int(brief_cfg.get("max_tokens", 2000))
        brief_llm["temperature"] = float(brief_cfg.get("temperature", 0.3))
        brief, brief_result = generate_brief_with_retry(
            brief_prompt,
            brief_llm,
            validate_brief,
            fix_hint=(
                "首行必须是【土耳其快讯简报 — 日期】；"
                "字段顺序固定且每个【字段】独占一行；"
                "禁止列表符号、Markdown、表格、Emoji；"
                "研究机构观点用「外资行研究」等泛称，不写机构名；"
                "篇幅按汉字+中文标点计 200–400 字；只要换行、不要空行。"
            ),
        )
        brief_attr_only = bool(brief) and not brief_result.get("ok") and all(
            "来源归属" in e for e in brief_result.get("errors", [])
        )
        if brief_attr_only:
            _log(f"WARNING: brief attribution leak persisted — shipping with warning: {brief_result['errors']}")
        if brief is not None and (brief_result.get("ok") or brief_attr_only):
            brief = brief.replace("\r\n", "\n")
            # The model sometimes re-emits the whole digest before the brief;
            # keep only from the brief title marker onward.
            marker = brief.find("【土耳其快讯简报")
            if marker > 0:
                brief = brief[marker:]
            brief = re.sub(r"\n{2,}", "\n", brief).strip() + "\n"
            brief_file = output_dir / f"{date_label}_flash_news{name_suffix}_brief_zh.md"
            brief_file.write_text(brief, encoding="utf-8")
            _log(f"Brief written: {brief_file}")
        else:
            _log(f"Brief skipped (validation failed): {brief_result.get('errors', [])[:3]}")

    # ---- commit checkpoint: everything in window counts as delivered ----
    store.commit_digest(window_end=window_end, item_ids=[it["id"] for it in items])
    _log(f"Checkpoint advanced: {len(items)} items marked delivered, last_digest_ts={iso(window_end)}")
    return 0


# --------------------------------------------------------------------------

def _load_env_file(skill_dir: Path) -> None:
    """Load skill-dir .env (gitignored) — key/value lines, no override of real env."""
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


def _calendar_in_scope(cfg: dict, window_end) -> list[dict]:
    """Config calendar entries dated within [now-12h, now+72h] (TRT dates)."""
    events = cfg.get("calendar") or []
    if not isinstance(events, list):
        return []
    from datetime import date as _date

    today = window_end.astimezone(TRT).date()
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        raw = str(ev.get("date") or "")
        try:
            d = _date.fromisoformat(raw)
        except ValueError:
            continue
        if 0 <= (d - today).days <= 3:
            out.append(ev)
    return out


def _format_sections(text: str) -> str:
    """Deterministic layout: blank line before every 【…】 line (section header
    or starred item brief) except the very first; blank lines removed elsewhere."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln.startswith("【") and out and out[-1].strip():
            out.append("")
        if ln.strip():
            out.append(ln)
    return "\n".join(out).strip() + "\n"


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Generate Turkey flash-news digest.")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--ingest-only", action="store_true", help="Only fetch into store, no digest.")
    parser.add_argument("--no-llm", action="store_true", help="Ingest + build prompt, skip LLM.")
    parser.add_argument("--force-window-hours", type=float, help="Override window hours (testing).")
    parser.add_argument("--force-refresh", action="store_true", help="Re-run even if today's digest exists.")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    paths = resolve_paths(
        config_path, cfg,
        default_template="templates/flash_news_template.txt",
        default_cache=".cache/turkey-flash-news",
    )
    skill_dir, workdir, output_dir, cache_dir, template_path = paths

    _load_env_file(skill_dir)

    if args.no_llm:
        cfg["no_llm"] = True

    store = FlashStore(cache_dir / "store")

    # Sunday skip (digest only; ingest still runs — KAP is 7x24)
    if not args.ingest_only and cfg.get("skip_sunday", True):
        if datetime.now(TRT).weekday() == 6 and not args.force_refresh:
            _log("Sunday (TR) — digest skipped by config (Monday window auto-extends).")
            return 0

    if not args.ingest_only and not args.force_refresh and not args.force_window_hours:
        date_label = datetime.now(TRT).strftime("%Y-%m-%d")
        if (output_dir / f"{date_label}_flash_news_zh.md").exists():
            _log(f"Digest for {date_label} already exists — use --force-refresh to redo.")
            return 0

    ingest_summary = ingest_all(store, cfg)
    _log(f"Ingest done: +{ingest_summary['new']} new items.")

    if args.ingest_only:
        return 0
    return run_digest(
        store,
        cfg,
        paths,
        force_window_hours=args.force_window_hours,
        force_refresh=args.force_refresh,
    )


if __name__ == "__main__":
    sys.exit(main())
