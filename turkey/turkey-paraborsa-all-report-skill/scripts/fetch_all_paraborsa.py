#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch full article bodies for discovered Paraborsa posts."""
from __future__ import annotations

import faulthandler
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scan_paraborsa import scan_all

# Enable faulthandler so hard crashes (segfault from native parsers like lxml,
# C-extension bugs, deadlocks) leave a traceback instead of a bare Exit 1.
faulthandler.enable()

BASE = "https://www.paraborsa.net"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
# Bypass local proxy: it triggers Cloudflare's managed challenge for paraborsa.net.
NO_PROXY = {"http": None, "https": None}

# Status codes that typically indicate transient Cloudflare / origin flaps and
# should be retried rather than treated as permanent per-slug failures.
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").replace("&#8211;", "–").strip()


def html_to_text(html: str) -> str:
    # Use the stdlib parser. On some Windows/env combinations lxml's native
    # binding segfaults on Paraborsa HTML, killing the process with no traceback.
    # html.parser is slower but rock-solid and dependency-free.
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return "\n".join(line for line in soup.get_text("\n", strip=True).splitlines() if line.strip())


def fetch_post_by_slug(
    slug: str,
    *,
    retries: int = 3,
    base_delay: float = 1.5,
) -> tuple[Optional[str], str]:
    url = f"{BASE}/wp-json/wp/v2/posts?slug={slug}"
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, proxies=NO_PROXY, timeout=25)
        except requests.RequestException as exc:
            if attempt + 1 >= retries:
                return None, f"network_error:{exc}"
            time.sleep(base_delay * (attempt + 1))
            continue
        # Retry transient Cloudflare / origin failures instead of giving up.
        if resp.status_code in _TRANSIENT_STATUS:
            if attempt + 1 < retries:
                time.sleep(base_delay * (attempt + 1) * 2)
                continue
            return None, f"status_{resp.status_code}"
        if resp.status_code != 200:
            return None, f"status_{resp.status_code}"
        # Parse JSON defensively: some 200 responses return an HTML error page
        # or empty body, which would raise JSONDecodeError ("Expecting value").
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            preview = re.sub(r"\s+", " ", resp.text or "")[:200]
            print(
                f"Warning: Paraborsa API bad JSON for {slug}: "
                f"{type(exc).__name__}: {exc}; body[:200]={preview!r}",
                file=sys.stderr,
            )
            return None, f"bad_json:{exc}"
        if not data:
            return None, "not_found"
        post = data[0]
        rendered = post.get("content", {}).get("rendered", "")
        text = html_to_text(rendered)
        if len(text) < 80:
            return None, "content_too_short"
        return text, "ok"
    return None, "failed"


def fetch_all_paraborsa(
    target_date: date,
    cache_dir: Path,
    *,
    allowed_prefixes: list[str] | None = None,
    include_period_ranges: bool = True,
    delay_seconds: float = 1.2,
    retry_on_429: int = 3,
    force_refresh: bool = False,
) -> dict:
    cache_file = cache_dir / f"paraborsa_all_{target_date.isoformat()}.json"
    if cache_file.exists() and not force_refresh:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("articles") and all(a.get("content") for a in cached["articles"] if a.get("fetch_status") == "ok"):
            return cached

    discovered = scan_all(
        target_date,
        allowed_prefixes=allowed_prefixes,
        include_period_ranges=include_period_ranges,
    )
    articles: list[dict] = []
    ok_count = 0
    # Abort the long broker walk after consecutive hard network failures so a
    # dead Paraborsa origin cannot burn the whole runner timeout.
    consecutive_net_fails = 0
    max_consecutive_net_fails = 4
    for idx, meta in enumerate(discovered):
        if consecutive_net_fails >= max_consecutive_net_fails:
            print(
                f"Warning: Paraborsa abort after {consecutive_net_fails} consecutive "
                f"network failures (remaining {len(discovered) - idx} slugs skipped)",
                file=sys.stderr,
            )
            break
        content, status = fetch_post_by_slug(meta["slug"], retries=min(retry_on_429, 2), base_delay=delay_seconds)
        if status.startswith("network_error") or status.startswith("status_5") or status == "status_429":
            consecutive_net_fails += 1
        else:
            consecutive_net_fails = 0
        entry = {
            **meta,
            "content": content or "",
            "content_chars": len(content or ""),
            "fetch_status": status,
        }
        if status == "ok":
            ok_count += 1
        articles.append(entry)
        if idx + 1 < len(discovered):
            time.sleep(delay_seconds)

    # Second pass for rate-limited / transient failures
    for entry in articles:
        if entry.get("fetch_status") == "ok":
            continue
        time.sleep(delay_seconds * 2)
        content, status = fetch_post_by_slug(
            entry["slug"],
            retries=retry_on_429 + 1,
            base_delay=delay_seconds * 2,
        )
        if status == "ok":
            entry["content"] = content
            entry["content_chars"] = len(content)
            entry["fetch_status"] = status
            ok_count += 1

    result = {
        "ok": ok_count > 0,
        "target_date": target_date.isoformat(),
        "discovered_count": len(discovered),
        "fetched_ok_count": ok_count,
        "fetched_failed_count": len(discovered) - ok_count,
        "fallback_url": "https://www.paraborsa.net/",
        "articles": articles,
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".cache/turkey-paraborsa-all-report")
    data = fetch_all_paraborsa(target, cache)
    print(json.dumps({
        "ok": data.get("ok"),
        "discovered_count": data.get("discovered_count"),
        "fetched_ok_count": data.get("fetched_ok_count"),
        "titles": [a.get("title") for a in data.get("articles", [])],
    }, ensure_ascii=False, indent=2))
