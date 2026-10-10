#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch BloombergHT close-of-day review and page headlines."""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from bht_closing_fetcher import (
    BASE,
    HEADERS,
    _SESSION,
    fetch_closing_review as _fetch_closing_review,
)
from runtime_utils import configure_stdio

BORSA_URL = f"{BASE}/borsa"


def fetch_main_page() -> Tuple[List[str], List[str]]:
    """Fetch breaking and featured headlines from /borsa."""
    try:
        resp = _SESSION.get(BORSA_URL, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        print(f"Warning: main page fetch failed: {exc}", file=sys.stderr)
        return [], []

    breaking: list[str] = []
    featured: list[str] = []

    son_dakika_header = soup.find(string=re.compile("SON DAKİKA", re.I))
    if son_dakika_header:
        parent = son_dakika_header.find_parent()
        if parent:
            seen: set[str] = set()
            for link in parent.find_all_next("a", limit=30):
                title = link.get_text(strip=True)
                if not title or len(title) < 20 or title in seen:
                    continue
                href = link.get("href", "")
                if href.startswith("/") and not href.startswith("/sondakika"):
                    continue
                breaking.append(title)
                seen.add(title)
                if len(breaking) >= 10:
                    break

    featured_header = soup.find(string=re.compile("Öne Çıkan", re.I))
    if featured_header:
        parent = featured_header.find_parent()
        if parent:
            seen = set()
            for link in parent.find_all_next("a", limit=30):
                title = link.get_text(strip=True)
                href = link.get("href", "")
                if not title or len(title) < 20 or title in seen:
                    continue
                if href.startswith("/"):
                    href = urljoin(BASE, href)
                elif not href.startswith("http"):
                    continue
                featured.append(title)
                seen.add(title)
                if len(featured) >= 10:
                    break

    return breaking, featured


def fetch_close_review(
    target_date: date,
    cache_dir: Path,
    *,
    workdir: Path | None = None,
    use_project_fetcher: bool = True,
    list_page_url: str = f"{BASE}/tum-piyasa-haberleri",
    force_refresh: bool = False,
) -> dict:
    cache_dir = Path(cache_dir)
    cache_file = cache_dir / f"bloomberght_close_{target_date.isoformat()}.json"

    if not force_refresh and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            closing = cached.get("closing_review") or {}
            if cached.get("ok") and closing.get("text"):
                return cached
        except Exception:
            pass

    # When force_refresh is requested, also purge the inner fetcher's cache so
    # the second verification pass cannot silently reuse a stale "not found".
    if force_refresh:
        inner_cache = cache_dir / f"bloomberght_closing_{target_date.isoformat()}.json"
        for stale in (cache_file, inner_cache):
            try:
                if stale.exists():
                    stale.unlink()
            except OSError:
                pass

    closing = _fetch_closing_review(
        target_date=target_date,
        cache_dir=cache_dir,
        workdir=workdir,
        list_page_url=list_page_url,
        use_project_fetcher=use_project_fetcher,
    )
    breaking, featured = fetch_main_page()

    result = {
        "ok": closing.get("ok", False),
        "target_date": target_date.isoformat(),
        "closing_review": {
            "title": closing.get("title") or "",
            "url": closing.get("url") or "",
            "text": closing.get("text") or "",
            "fetch_method": closing.get("fetch_method"),
            "article_id": closing.get("article_id"),
        },
        "breaking_news": breaking,
        "featured_news": featured,
        "fallback_url": closing.get("fallback_url"),
        "error": closing.get("error"),
    }

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    configure_stdio()
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".cache/turkey-close-report")
    workdir = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
    result = fetch_close_review(target, cache, workdir=workdir)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
