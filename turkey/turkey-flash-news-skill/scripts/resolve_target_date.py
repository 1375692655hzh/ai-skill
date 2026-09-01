#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve today's date and the most recent completed trading day in Turkey."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

TR_TZ = timezone(timedelta(hours=3))

# Fixed Turkish / BIST closed days (month, day). Religious movable holidays not included.
FIXED_HOLIDAY_MD = (
    (1, 1),
    (4, 23),
    (5, 1),
    (5, 19),
    (7, 15),
    (8, 30),
    (10, 29),
)


def now_tr() -> datetime:
    return datetime.now(TR_TZ)


def today_tr() -> date:
    return now_tr().date()


def fixed_tr_holidays(ref: Optional[date] = None) -> List[str]:
    """
    Auto-generate fixed holidays for the reference year.
    From June onward, also include the next calendar year.
    """
    if ref is None:
        ref = today_tr()
    years = [ref.year]
    if ref.month >= 6:
        years.append(ref.year + 1)
    return [date(y, m, d).isoformat() for y in years for m, d in FIXED_HOLIDAY_MD]


def merge_holidays(
    extra: Optional[List[str]] = None,
    ref: Optional[date] = None,
) -> List[str]:
    """Fixed auto holidays + optional config extras."""
    return sorted(set(fixed_tr_holidays(ref)) | set(extra or []))


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


def load_holidays(holidays: Optional[list[str]]) -> set[date]:
    """Load extras + auto fixed holidays as a date set."""
    result = set()
    for h in merge_holidays(holidays):
        try:
            result.add(date.fromisoformat(h))
        except ValueError:
            continue
    return result


def previous_trading_day(start: date, holidays: set[date]) -> date:
    """Move backward until a non-weekend, non-holiday date is found."""
    d = start - timedelta(days=1)
    while is_weekend(d) or d in holidays:
        d -= timedelta(days=1)
    return d


def resolve_dates(
    forced_target: Optional[str] = None,
    holidays: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    today_date: current Turkish trading day.
    target_date: most recent completed trading day for closing data.
    `holidays` are optional extras; fixed TR holidays are always merged in.
    """
    if now is None:
        now = now_tr()

    holiday_set = load_holidays(holidays)

    if forced_target:
        target = date.fromisoformat(forced_target)
        today = target  # For forced mode, assume today is the next trading day
        if is_weekend(target) or target in holiday_set:
            target = previous_trading_day(target + timedelta(days=1), holiday_set)
        return {
            "today_date": today.isoformat(),
            "target_date": target.isoformat(),
            "now_tr": now.isoformat(),
            "forced": True,
        }

    today = now.date()

    if is_weekend(today) or today in holiday_set:
        target = previous_trading_day(today, holiday_set)
        return {
            "today_date": today.isoformat(),
            "target_date": target.isoformat(),
            "now_tr": now.isoformat(),
            "forced": False,
            "holiday": True,
        }

    # Morning briefing always uses the previous close.
    if now.time() < time(10, 0):
        target = previous_trading_day(today, holiday_set)
    else:
        target = previous_trading_day(today, holiday_set)

    return {
        "today_date": today.isoformat(),
        "target_date": target.isoformat(),
        "now_tr": now.isoformat(),
        "forced": False,
        "holiday": False,
    }


if __name__ == "__main__":
    import json, sys

    force = None
    if "--force" in sys.argv:
        idx = sys.argv.index("--force")
        force = sys.argv[idx + 1]
    print(json.dumps(resolve_dates(forced_target=force), ensure_ascii=False, indent=2))
    print("holidays:", ", ".join(merge_holidays()))
