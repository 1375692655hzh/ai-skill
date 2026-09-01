#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TCMB (Turkish central bank) fetchers: press-release Atom + daily FX fixing.

Contract quirks (verified 2026-08-31):
- Press Atom is served with Content-Type text/html but the body is valid Atom.
- Raw date format inside entries: "Aug 28, 2026, 5:23:07 PM" (TRT local).
- today.xml is overwritten each trading day ~15:30 TRT; on non-trading days the
  embedded Tarih date is simply older — that is normal, not an error.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import feedparser
import requests

TRT = timezone(timedelta(hours=3))
PRESS_URL = (
    "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB+EN/Bottom+Menu/"
    "Other/RSS/Press+Releases"
)
FX_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (turkey-flash-news-skill/1.0)"}
TIME_FORMATS = [
    "%b %d, %Y, %I:%M:%S %p",
    "%b %d, %Y, %I:%M %p",
    "%d %b %Y %H:%M:%S",
]


def _parse_press_date(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TRT).astimezone(timezone.utc)
        except ValueError:
            continue
    # RFC2822 fallback (feedparser-style dates)
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TRT)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def fetch_tcmb_press(*, timeout: int = 40) -> list[dict[str, Any]]:
    """Recent press releases (rate decisions, liquidity ops, statements)."""
    resp = requests.get(PRESS_URL, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    fp = feedparser.parse(resp.content)
    from store import stable_id

    items: list[dict[str, Any]] = []
    for entry in fp.entries:
        title = re.sub(r"\s+", " ", entry.get("title") or "").strip()
        if not title:
            continue
        raw_date = entry.get("published") or entry.get("updated") or ""
        ts = None
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            ts = datetime(*parsed[:6], tzinfo=timezone.utc)
        if ts is None:
            ts = _parse_press_date(raw_date)
        link = entry.get("link") or ""
        items.append(
            {
                "id": stable_id("tcmb_press", link or title, raw_date),
                "source": "tcmb_press",
                "kind": "central_bank",
                "ts": ts.isoformat() if ts else None,
                "raw_time": raw_date,
                "title": title,
                "body": title,
                "url": link,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return items


def fetch_tcmb_fx(*, timeout: int = 30) -> dict[str, Any]:
    """Daily official FX fixing snapshot (fact card, not an event stream)."""
    out: dict[str, Any] = {"ok": False, "date": None, "rates": {}, "url": FX_URL}
    try:
        resp = requests.get(FX_URL, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception:
        return out
    out["date"] = root.get("Tarih") or None
    keep = {"USD", "EUR", "GBP"}
    for cur in root.findall("Currency"):
        code = (cur.get("CurrencyCode") or "").strip()
        if code not in keep:
            continue
        buy = cur.findtext("ForexBuying") or ""
        sell = cur.findtext("ForexSelling") or ""
        out["rates"][code] = {"buy": buy, "sell": sell}
    out["ok"] = bool(out["rates"])
    return out


if __name__ == "__main__":
    press = fetch_tcmb_press()
    print(f"press entries: {len(press)}")
    for p in press[:3]:
        print(" ", p["raw_time"], "|", p["title"][:80])
    fx = fetch_tcmb_fx()
    print("fx:", fx["ok"], fx["date"], fx["rates"])
