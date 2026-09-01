#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic RSS flash fetcher for the vetted Turkey source pool.

Source tiers (2026-08-31 research, quality-ranked):
  cnbce       — wire-grade, ~60+/day, 5-min freshness (TR)
  foreks      — terminal-grade finance feed, KAP/index transcriptions (TR)
  dunya       — financial daily, deep buffer only ~3-4h → must poll often (TR)
  dailysabah  — English business/policy, low volume (disabled by default)
  sabah       — macro/policy daily ~10/day (disabled by default)
  hurriyet    — CUT: stale mixed politics feed, worst timeliness
Common parsing traps: RFC2822 pubDate + ISO fallback, CDATA titles,
r.encoding = r.apparent_encoding for Turkish chars (ç/ğ/ı/ö/ş/ü).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import feedparser
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (turkey-flash-news-skill/1.0)"}


def _entry_time(entry: dict) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        raw = (entry.get(key) or "").strip()
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", text).strip()


def fetch_feed(
    *,
    source_id: str,
    url: str,
    lang: str = "tr",
    timeout: int = 40,
    max_items: int = 120,
) -> list[dict[str, Any]]:
    """Fetch one RSS feed → store-ready items (id deduped downstream)."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    if resp.encoding in (None, "iso-8859-1"):
        resp.encoding = resp.apparent_encoding
    resp.raise_for_status()
    fp = feedparser.parse(resp.content)

    from store import stable_id

    items: list[dict[str, Any]] = []
    for entry in fp.entries[:max_items]:
        title = _clean(entry.get("title") or "")
        if not title:
            continue
        summary = _clean(entry.get("summary") or entry.get("description") or "")
        link = entry.get("link") or ""
        ts = _entry_time(entry)
        items.append(
            {
                "id": stable_id(source_id, link or title, str(entry.get("id") or "")),
                "source": source_id,
                "kind": "rss",
                "lang": lang,
                "ts": ts.isoformat() if ts else None,
                "raw_time": entry.get("published") or "",
                "title": title,
                "body": (title + (" — " + summary[:300] if summary and summary != title else "")),
                "url": link,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return items


DEFAULT_FEEDS = [
    {"id": "cnbce", "url": "https://www.cnbce.com/rss", "lang": "tr", "enabled": True},
    {"id": "foreks", "url": "https://www.foreks.com/rss/", "lang": "tr", "enabled": True},
    {"id": "dunya", "url": "https://www.dunya.com/rss", "lang": "tr", "enabled": True},
    {"id": "dailysabah", "url": "https://www.dailysabah.com/rssfeed/business", "lang": "en", "enabled": False},
    {"id": "sabah", "url": "https://www.sabah.com.tr/rss/ekonomi.xml", "lang": "tr", "enabled": False},
]
