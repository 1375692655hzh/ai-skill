#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Locate upstream skill outputs for the daily hotspots report.

Date semantics (from the colleague workflow spec):
- Report date T = the Turkey "today" the report is for.
- SAME-DAY inputs: info_daily, x_influencer, aa_morning (+ optional
  morning_briefing / info_technical). If T's file is missing, fall back to the
  newest available earlier file and MARK the fallback (the report must state
  which material is from the previous day).
- PREVIOUS-TRADING-DAY inputs: close_report, paraborsa — strictly the last
  trading day before T (Monday → Friday; holidays skipped).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

TRT = timezone(timedelta(hours=3))

# Fixed TR holidays (month, day) — mirror other skills' calendar.
FIXED_HOLIDAYS = {(1, 1), (4, 23), (5, 1), (5, 19), (7, 15), (8, 30), (10, 29)}


def tr_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(TRT).date()


def prev_trading_day(d: date, extra_holidays: Optional[list[str]] = None) -> date:
    extra = set()
    for raw in extra_holidays or []:
        try:
            extra.add(date.fromisoformat(raw))
        except ValueError:
            continue
    cur = d - timedelta(days=1)
    for _ in range(30):
        if cur.weekday() < 5 and (cur.month, cur.day) not in FIXED_HOLIDAYS and cur not in extra:
            return cur
        cur -= timedelta(days=1)
    return d - timedelta(days=1)


def resolve_target_date(cfg: dict, now: Optional[datetime] = None) -> date:
    raw = str(cfg.get("target_date", "auto")).lower()
    if raw not in ("", "auto"):
        return date.fromisoformat(raw)
    return tr_today(now)


def _read_capped(path: Path, cap: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > cap:
        head = text[: cap // 2]
        tail = text[-cap // 2 :]
        text = head + "\n\n…[中段截断]…\n\n" + tail
    return text


def find_input(
    roots: dict[str, str],
    patterns: dict[str, str],
    key: str,
    wanted: date,
    *,
    fallback_days: int = 4,
) -> Optional[dict[str, Any]]:
    """Find the newest file for `key` dated <= wanted. Returns meta with actual date."""
    pattern = patterns.get(key) or ""
    root = Path(roots.get(key) or "")
    if not pattern or not root.exists():
        return None
    for offset in range(0, fallback_days + 1):
        d = wanted - timedelta(days=offset)
        name = pattern.replace("{date}", d.isoformat())
        p = root / name
        if p.is_file():
            return {"key": key, "path": p, "date": d, "fallback": offset > 0}
    return None


CORE_KEYS = ("info_daily", "x_influencer")
OPTIONAL_KEYS = ("aa_morning", "morning_briefing", "info_technical")
PREV_KEYS = ("close_report", "paraborsa")


def collect_inputs(cfg: dict, now: Optional[datetime] = None) -> dict[str, Any]:
    inputs_cfg = cfg.get("inputs", {})
    roots = inputs_cfg.get("roots", {})
    patterns = inputs_cfg.get("patterns", {})
    caps = inputs_cfg.get("max_chars_per_input", 60000)
    x_cap = inputs_cfg.get("x_max_chars", 150000)

    target = resolve_target_date(cfg, now)
    prev_day = prev_trading_day(target, cfg.get("holidays"))

    result: dict[str, Any] = {
        "target_date": target,
        "prev_trading_day": prev_day,
        "same_day": {},
        "prev_day": {},
        "missing_core": [],
        "missing_optional": [],
        "missing_prev": [],
    }
    date_for_key = {
        "info_daily": target,
        "x_influencer": target,
        "aa_morning": target,
        "morning_briefing": target,
        "info_technical": target,
        "close_report": prev_day,
        "paraborsa": prev_day,
    }
    for key in CORE_KEYS + OPTIONAL_KEYS + PREV_KEYS:
        hit = find_input(roots, patterns, key, date_for_key[key])
        if hit is None:
            bucket = (
                result["missing_core"]
                if key in CORE_KEYS
                else result["missing_prev"] if key in PREV_KEYS
                else result["missing_optional"]
            )
            bucket.append(key)
            continue
        cap = x_cap if key == "x_influencer" else caps
        hit["text"] = _read_capped(hit["path"], int(cap))
        day_bucket = result["same_day"] if key in CORE_KEYS + OPTIONAL_KEYS else result["prev_day"]
        day_bucket[key] = hit
    return result
