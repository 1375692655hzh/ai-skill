#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch BloombergHT closing review + breaking news + featured articles."""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from bht_closing_fetcher import (
    BASE,
    HEADERS,
    _SESSION,
    fetch_closing_review as _fetch_closing_review,
)

BORSA_URL = f"{BASE}/borsa"
TR_TZ = timezone(timedelta(hours=3))


def article_publish_date(url: str, *, timeout: int = 20) -> Optional[date]:
    """Parse <time datetime> (or visible TR date) from a BloombergHT article page."""
    if not url or "/sondakika" in url.rstrip("/").split("/")[-1]:
        return None
    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for node in soup.find_all("time"):
            raw = (node.get("datetime") or "").strip()
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TR_TZ)
                return dt.astimezone(TR_TZ).date()
            except ValueError:
                continue
        text = soup.get_text(" ", strip=True)
        m = re.search(
            r"(\d{1,2})\s+(Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)\s+(20\d{2})",
            text,
        )
        if not m:
            return None
        months = {
            "Ocak": 1, "Şubat": 2, "Mart": 3, "Nisan": 4, "Mayıs": 5, "Haziran": 6,
            "Temmuz": 7, "Ağustos": 8, "Eylül": 9, "Ekim": 10, "Kasım": 11, "Aralık": 12,
        }
        return date(int(m.group(3)), months[m.group(2)], int(m.group(1)))
    except Exception:
        return None


def filter_items_for_today(
    items: List[Dict[str, str]],
    today: date,
    *,
    undated_policy: str = "drop",
    resolve_urls: bool = True,
    delay_seconds: float = 0.4,
) -> List[Dict[str, str]]:
    """
    Keep only headlines published on `today` (Turkey calendar).

    undated_policy:
      - "drop": discard items without a resolvable date (default for featured)
      - "assume_today": treat undated live ticker as today (breaking SON DAKİKA)
    """
    kept: list[dict[str, str]] = []
    for idx, item in enumerate(items or []):
        entry = dict(item)
        pub: Optional[date] = None
        raw = entry.get("published_date") or entry.get("date")
        if raw:
            try:
                pub = date.fromisoformat(str(raw)[:10])
            except ValueError:
                pub = None
        if pub is None and resolve_urls:
            pub = article_publish_date(entry.get("url") or "")
            if delay_seconds and idx + 1 < len(items):
                time.sleep(delay_seconds)
        if pub is None and undated_policy == "assume_today":
            pub = today
        if pub != today:
            title = (entry.get("title") or "")[:60]
            print(
                f"Skip non-today headline ({pub}): {title}",
                file=sys.stderr,
            )
            continue
        entry["published_date"] = pub.isoformat()
        kept.append(entry)
    return kept


def fetch_today_headlines(today: date) -> Dict[str, List[Dict[str, str]]]:
    """Fresh SON DAKİKA + Öne Çıkan, filtered to Turkey `today` only."""
    print(f"Fetching live BHT headlines for today={today.isoformat()}...", file=sys.stderr)
    breaking_raw = fetch_breaking_news()
    featured_raw = fetch_featured_news()
    # Live ticker has no article URL/date → only valid when freshly scraped as "now".
    breaking = filter_items_for_today(
        breaking_raw,
        today,
        undated_policy="assume_today",
        resolve_urls=False,
    )
    featured = filter_items_for_today(
        featured_raw,
        today,
        undated_policy="drop",
        resolve_urls=True,
    )
    print(
        f"Today headlines: breaking={len(breaking)}/{len(breaking_raw)}, "
        f"featured={len(featured)}/{len(featured_raw)}",
        file=sys.stderr,
    )
    return {"breaking_news": breaking, "featured_news": featured}


def fetch_breaking_news(url: str = BORSA_URL) -> List[Dict[str, str]]:
    """Fetch SON DAKİKA headlines from BloombergHT /borsa."""
    items: list[dict[str, str]] = []
    try:
        resp = _SESSION.get(url, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        son_dakika_header = soup.find(string=re.compile("SON DAKİKA", re.I))
        if not son_dakika_header:
            return items

        parent = son_dakika_header.find_parent()
        if not parent:
            return items

        seen_titles: set[str] = set()
        for link in parent.find_all_next("a", limit=30):
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 20 or title in seen_titles:
                continue
            if href.startswith("/") and not href.startswith("/sondakika"):
                continue
            items.append({
                "title": title,
                "url": urljoin(BASE, href),
            })
            seen_titles.add(title)
            if len(items) >= 10:
                break
    except Exception as e:
        print(f"Breaking news fetch failed: {e}", file=sys.stderr)
    return items


def fetch_featured_news(url: str = BORSA_URL) -> List[Dict[str, str]]:
    """Fetch Öne Çıkan Haberler from BloombergHT /borsa."""
    items: list[dict[str, str]] = []
    try:
        resp = _SESSION.get(url, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        featured_header = soup.find(string=re.compile("Öne Çıkan", re.I))
        if not featured_header:
            return items

        parent = featured_header.find_parent()
        if not parent:
            return items

        seen_titles: set[str] = set()
        for link in parent.find_all_next("a", limit=30):
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or len(title) < 20 or title in seen_titles:
                continue
            if href.startswith("/"):
                href = urljoin(BASE, href)
            elif not href.startswith("http"):
                continue
            items.append({"title": title, "url": href})
            seen_titles.add(title)
            if len(items) >= 10:
                break
    except Exception as e:
        print(f"Featured news fetch failed: {e}", file=sys.stderr)
    return items


def fetch_all_news(
    target_date: date,
    cache_dir: Path,
    *,
    workdir: Path | None = None,
    closing_cfg: dict | None = None,
) -> Dict:
    """Fetch closing review + breaking + featured news for target_date."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"bloomberght_all_{target_date.isoformat()}.json"
    closing_cfg = closing_cfg or {}

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            closing = cached.get("closing_review") or {}
            if cached.get("ok") and closing.get("ok"):
                # Only the closing review is date-stable and safe to reuse.
                # Breaking/featured news MUST be re-fetched live every run,
                # otherwise stale yesterday headlines bleed into today's prompt.
                print("Closing review cache hit; re-fetching live news...", file=sys.stderr)
                breaking = fetch_breaking_news()
                featured = fetch_featured_news()
                result = {
                    "ok": True,
                    "target_date": target_date.isoformat(),
                    "source": "bloomberght",
                    "closing_review": closing,
                    "breaking_news": breaking,
                    "featured_news": featured,
                    "total_items": len(breaking) + len(featured) + 1,
                }
                cache_file.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return result
        except Exception:
            pass

    print("Fetching BloombergHT closing review...", file=sys.stderr)
    closing = fetch_closing_review(
        target_date=target_date,
        cache_dir=cache_dir,
        workdir=workdir,
        closing_cfg=closing_cfg,
    )

    print("Fetching breaking news (SON DAKİKA)...", file=sys.stderr)
    breaking = fetch_breaking_news()

    print("Fetching featured news (Öne Çıkan Haberler)...", file=sys.stderr)
    featured = fetch_featured_news()

    # Prefer nested closing_review dict for downstream; fall back to flat.
    closing_nested = closing.get("closing_review") if isinstance(closing.get("closing_review"), dict) else closing
    result = {
        "ok": bool(closing.get("ok")),
        "target_date": target_date.isoformat(),
        "source": "bloomberght",
        "closing_review": closing_nested,
        "breaking_news": breaking,
        "featured_news": featured,
        "total_items": len(breaking) + len(featured) + (1 if closing.get("ok") else 0),
        "error": None if closing.get("ok") else (closing.get("error") or "closing review missing"),
    }
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def fetch_closing_review(
    target_date: date,
    cache_dir: Path,
    rss_url: str = f"{BASE}/rss",
    *,
    workdir: Path | None = None,
    closing_cfg: dict | None = None,
) -> dict:
    closing_cfg = closing_cfg or {}
    if closing_cfg.get("enabled") is False:
        return {
            "ok": False,
            "target_date": target_date.isoformat(),
            "error": "BloombergHT closing fetch disabled in config.",
            "title": None,
            "url": None,
            "text": None,
            "source": "bloomberght",
        }
    # Default use_project_fetcher=False: morning skill is self-contained and
    # shares bht_closing_fetcher with the close-report skill (list_page →
    # borsa_related → small forecast). Project delegation is opt-in only.
    payload = _fetch_closing_review(
        target_date=target_date,
        cache_dir=cache_dir,
        workdir=workdir,
        rss_url=closing_cfg.get("rss_url", rss_url),
        list_page_url=closing_cfg.get(
            "list_page_url",
            f"{BASE}/tum-piyasa-haberleri",
        ),
        use_project_fetcher=closing_cfg.get("use_project_fetcher", False),
    )
    # Expose both flat fields (legacy callers) and nested closing_review
    # (fetch_all_news / source headers).
    if isinstance(payload, dict) and "closing_review" not in payload:
        payload = {
            **payload,
            "closing_review": {
                "ok": payload.get("ok"),
                "title": payload.get("title"),
                "url": payload.get("url"),
                "text": payload.get("text"),
                "fetch_method": payload.get("fetch_method"),
                "article_id": payload.get("article_id"),
                "error": payload.get("error"),
            },
        }
    return payload


if __name__ == "__main__":
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".cache/turkey-morning-report")
    workdir = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
    result = fetch_all_news(target, cache, workdir=workdir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
