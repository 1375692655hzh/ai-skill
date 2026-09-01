#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BloombergHT closing review fetcher — aligned with Turkey-investment/piyasa_ozesi.py."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.bloomberght.com"
FALLBACK_URL = f"{BASE}/borsa"
LIST_PAGE_URL = f"{BASE}/tum-piyasa-haberleri"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

# Module-level session with retry/backoff. BHT is behind Cloudflare and the ID
# scan path issues many requests; a single 5xx / TLS flap shouldn't gen_fail.
# probe_id and _forecast_scan run hundreds of requests, so retries are bounded.
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)
_RETRY = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD"}),
    raise_on_status=False,
)
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY))
_SESSION.mount("http://", HTTPAdapter(max_retries=_RETRY))

TITLE_RE = re.compile(
    r"(piyasa\s*özeti|piyasalarda\s*günün\s*özeti)\s*:?", re.IGNORECASE
)
# Slug forms used on tum-piyasa-haberleri / /borsa related links.
SLUG_RE = re.compile(
    r"(?:piyasalarda-gunun-ozeti|piyasa-ozeti)", re.IGNORECASE
)

_TR_MONTHS = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4,
    "mayıs": 5, "mayis": 5, "haziran": 6, "temmuz": 7, "ağustos": 8,
    "agustos": 8, "eylül": 9, "eylul": 9, "ekim": 10, "kasım": 11,
    "kasim": 11, "aralık": 12, "aralik": 12,
}

RSS_LAG_BUFFER = 100
MIN_RECENT_SCAN = 80

# List / related-news discovery.
# tum-piyasa-haberleri is newest→oldest. Same-day articles sit in the top
# cards; history is further down on the same HTML payload. Site ?page=N does
# not deepen history (pages repeat), so we never infinite-scroll — we use the
# closing-review date window on the fetched HTML to decide published/absent.
TOP_LIST_LINKS = 15
TOP_BORSA_RELATED = 10
MAX_LIST_PAGES = 2                 # try page=2 once; stop if URLs unchanged
MAX_CLOSING_LINKS_SCAN = 40        # hard cap on closing-slug links inspected
# Ignore orphan ancient links when computing the newest→oldest window
# (sidebar/footer chrome). Exact date match still searches all links.
MAX_STREAM_GAP_DAYS = 45

# Forecasting is a LAST RESORT only when the target date is OLDER than the
# list page's oldest closing review (outside the HTML window). Keep small.
FORECAST_ID_PER_DAY = 110
FORECAST_SCAN_WINDOW = 250         # ± IDs; 150 missed when slope noise ~100
FORECAST_WORKERS = 8

# list_page scan outcomes (second return value of _scan_list_page)
LIST_HIT = "hit"
LIST_ABSENT = "absent"             # in window / not yet published — stop ID scan
LIST_BEYOND = "beyond_window"      # older than page — allow id_scan fallback
LIST_EMPTY = "empty"               # no closing links parsed — allow fallback
LIST_ERROR = "error"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def parse_tr_date_from_title(title: str) -> Optional[date]:
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", title)
    if not m:
        return None
    month = _TR_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _slug_date_token(d: date) -> str:
    """Turkish slug date fragment, e.g. 31-temmuz-2026."""
    month_names = {
        1: "ocak", 2: "subat", 3: "mart", 4: "nisan", 5: "mayis", 6: "haziran",
        7: "temmuz", 8: "agustos", 9: "eylul", 10: "ekim", 11: "kasim", 12: "aralik",
    }
    return f"{d.day}-{month_names[d.month]}-{d.year}"


def parse_tr_date_from_slug(href: str) -> Optional[date]:
    """Parse day-month-year from a closing-review URL slug."""
    if not href:
        return None
    m = re.search(
        r"(\d{1,2})-([a-zçğıöşüÇĞİÖŞÜ]+)-(\d{4})",
        href,
        re.IGNORECASE,
    )
    if not m:
        return None
    month = _TR_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _href_matches_closing(href: str, target_date: date) -> bool:
    """True if URL slug looks like a closing review for target_date."""
    if not href or not SLUG_RE.search(href):
        return False
    return parse_tr_date_from_slug(href) == target_date


def _link_label(link) -> str:
    """Best-effort title from anchor text / title / aria-label."""
    parts = [
        link.get_text(" ", strip=True) or "",
        (link.get("title") or "").strip(),
        (link.get("aria-label") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _clean_closing_title(title: str) -> str:
    clean = (title or "").split("|")[0].strip()
    m = re.match(
        r"((?:Piyasalarda günün özeti|Piyasa özeti):[^.]+(?:fiyatları|son durum))",
        clean,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else clean


def _article_id_from_href(href: str) -> Optional[int]:
    m = re.search(r"-(\d{6,})(?:[/?#]|$)", href or "")
    return int(m.group(1)) if m else None


def _candidate_from_link(
    link,
    target_date: date,
    *,
    method: str,
) -> Optional[dict]:
    """If link is today's closing review, fetch body and return success payload."""
    href = urljoin(BASE, link.get("href") or "")
    label = _link_label(link)
    by_slug = _href_matches_closing(href, target_date)
    by_title = bool(TITLE_RE.search(label) and parse_tr_date_from_title(label) == target_date)
    if not by_slug and not by_title:
        return None
    text = extract_article_text(href)
    if not text or text.startswith("ERROR"):
        # One retry, then fall back to probe_id HTML if we have an article id.
        text = extract_article_text(href)
    if (not text or text.startswith("ERROR")) and _article_id_from_href(href):
        art = probe_id(_article_id_from_href(href) or 0)
        if art and art.get("date") == target_date:
            text = extract_article_text(art["url"], art.get("html"))
            href = art.get("url") or href
            if art.get("title"):
                label = art["title"]
    if not text or text.startswith("ERROR"):
        return None
    title = _clean_closing_title(label) if label else href.rsplit("/", 1)[-1]
    return _success(
        target_date,
        title=title,
        url=href,
        text=text,
        method=method,
        article_id=_article_id_from_href(href),
    )


def _iter_top_links(soup: BeautifulSoup, limit: int):
    """Yield unique absolute-ish <a> tags in document order, capped at limit."""
    seen: set[str] = set()
    n = 0
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        # Prefer article-like paths
        if href in seen:
            continue
        seen.add(href)
        yield link
        n += 1
        if n >= limit:
            return


def _failure(target_date: date, error: str, method: str | None = None) -> dict:
    payload = {
        "ok": False,
        "target_date": target_date.isoformat(),
        "error": error,
        "title": None,
        "url": None,
        "text": None,
        "source": "bloomberght",
        "fallback_url": FALLBACK_URL,
    }
    if method:
        payload["fetch_method"] = method
    return payload


def _success(
    target_date: date,
    title: str,
    url: str,
    text: str,
    method: str,
    article_id: int | None = None,
) -> dict:
    payload = {
        "ok": True,
        "target_date": target_date.isoformat(),
        "title": title,
        "url": url,
        "text": text,
        "source": "bloomberght",
        "error": None,
        "fetch_method": method,
    }
    if article_id:
        payload["article_id"] = article_id
    return payload


def extract_article_text(url: str, html: str | None = None) -> str:
    try:
        if html is None:
            resp = _SESSION.get(url, timeout=40)
            resp.raise_for_status()
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        body = (
            soup.select_one("div.news-content")
            or soup.select_one("div.article-body")
            or soup.find("article")
            or soup
        )
        seen: set[str] = set()
        parts: list[str] = []
        for el in body.find_all(["h2", "h3", "p"], recursive=True):
            txt = el.get_text(" ", strip=True)
            if txt and len(txt) > 15 and txt not in seen:
                seen.add(txt)
                parts.append(txt)
        return "\n".join(parts)
    except Exception as e:
        return f"ERROR extracting article: {e}"


def _lookup_manifest(workdir: Path | None, target_date: date) -> Optional[dict]:
    if not workdir:
        return None
    date_iso = target_date.isoformat()
    manifest = (
        workdir / "reports" / "turkey-market-reports" / date_iso / f"{date_iso}_manifest.json"
    )
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    for entry in data.get("files", []):
        if (
            entry.get("source") == "bloomberght"
            and entry.get("position") == "closing_detail"
            and entry.get("status") == "ok"
        ):
            file_path = entry.get("file_path")
            if not file_path:
                continue
            fp = Path(file_path)
            if not fp.is_absolute():
                fp = workdir / fp
            if fp.is_file():
                text = fp.read_text(encoding="utf-8")
                return _success(
                    target_date,
                    title=entry.get("title") or fp.stem,
                    url=entry.get("url") or "",
                    text=text,
                    method="manifest",
                    article_id=entry.get("article_id"),
                )
    return None


def _extract_closing_links(soup: BeautifulSoup) -> list[tuple[date, object, str]]:
    """Closing-review links in DOM order: (article_date, link_tag, abs_url)."""
    out: list[tuple[date, object, str]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href or not SLUG_RE.search(href):
            continue
        abs_url = urljoin(BASE, href)
        if abs_url in seen:
            continue
        art_date = parse_tr_date_from_slug(href)
        if art_date is None:
            art_date = parse_tr_date_from_title(_link_label(link))
        if art_date is None:
            continue
        seen.add(abs_url)
        out.append((art_date, link, abs_url))
        if len(out) >= MAX_CLOSING_LINKS_SCAN:
            break
    return out


def _fetch_list_soup(list_url: str, page: int) -> Optional[BeautifulSoup]:
    url = list_url if page <= 1 else f"{list_url}?page={page}"
    try:
        resp = _SESSION.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"List page fetch failed (page={page}): {e}")
        return None
    return BeautifulSoup(resp.text, "html.parser")


def _stream_date_window(
    closing: list[tuple[date, object, str]],
) -> tuple[date, date]:
    """Newest/oldest from the leading newest→oldest stream only.

    Stops at DOM order breaks or a calendar gap > MAX_STREAM_GAP_DAYS so
    footer/sidebar orphans do not inflate the window.
    """
    stream: list[date] = []
    prev: date | None = None
    for art_date, _, _ in closing:
        if prev is not None:
            if art_date > prev:
                break
            if (prev - art_date).days > MAX_STREAM_GAP_DAYS:
                break
        stream.append(art_date)
        prev = art_date
    if not stream:
        dates = [d for d, _, _ in closing]
        return max(dates), min(dates)
    return stream[0], stream[-1]


def _scan_list_page(
    target_date: date,
    list_url: str = LIST_PAGE_URL,
) -> tuple[Optional[dict], str, list[tuple[date, int]]]:
    """Scan tum-piyasa-haberleri for target_date closing review.

    Returns (success_payload_or_None, status, anchors) where status is one of:
      hit | absent | beyond_window | empty | error
    anchors: (article_date, article_id) pairs from the list page for forecast seeding.

    Publication logic (newest→oldest, no infinite scroll):
      - Exact date match on page → hit (fetch body).
      - Newest closing on stream < target → not published yet → absent
        (hard stop; no id_scan). This is the only hard-absent case.
      - Weekend/holiday gap inside window → absent (no article expected).
      - Weekday miss inside window → beyond_window (list lag / alt slug;
        allow id_scan after borsa). Avoids false negatives from chrome
        polluting oldest or a missing card.
      - Target older than stream oldest → beyond_window.
      - ?page=N only tried once; if URL set unchanged, stop.
    """
    closing: list[tuple[date, object, str]] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_LIST_PAGES + 1):
        # Page 1: fetch twice and merge. BHT CDN occasionally serves a skewed
        # HTML snapshot missing the newest closing card for one response.
        attempts = 2 if page == 1 else 1
        page_new = 0
        for _attempt in range(attempts):
            soup = _fetch_list_soup(list_url, page)
            if soup is None:
                if page == 1 and _attempt == 0:
                    continue
                if page == 1 and not closing:
                    return None, LIST_ERROR, []
                break
            batch = _extract_closing_links(soup)
            for item in batch:
                if item[2] in seen_urls:
                    continue
                seen_urls.add(item[2])
                closing.append(item)
                page_new += 1
                if len(closing) >= MAX_CLOSING_LINKS_SCAN:
                    break
            if len(closing) >= MAX_CLOSING_LINKS_SCAN:
                break
        if page > 1 and page_new == 0:
            log(f"list_page: page={page} repeats page1; stop pagination")
            break
        if len(closing) >= MAX_CLOSING_LINKS_SCAN:
            break

    anchors: list[tuple[date, int]] = []
    for art_date, _link, abs_url in closing:
        aid = _article_id_from_href(abs_url)
        if aid:
            anchors.append((art_date, aid))

    if not closing:
        log("list_page: no dated closing-review links found")
        return None, LIST_EMPTY, anchors

    newest, oldest = _stream_date_window(closing)
    log(
        f"list_page window: newest={newest} oldest={oldest} "
        f"n={len(closing)} target={target_date.isoformat()}"
    )

    # 1) Exact match across all closing links (history further down is fine).
    for art_date, link, abs_url in closing:
        if art_date != target_date:
            continue
        hit = _candidate_from_link(link, target_date, method="list_page")
        if hit:
            log(f"list_page hit: {hit.get('url')}")
            return hit, LIST_HIT, anchors
        # Slug matched but body extract failed — do not claim absent.
        log(f"list_page: matched {abs_url} but body extract failed")
        return None, LIST_BEYOND, anchors

    # 2) Not on page — decide absent vs beyond window.
    if newest < target_date:
        log(
            f"list_page: absent (not published) — newest closing {newest} "
            f"< target {target_date.isoformat()}"
        )
        return None, LIST_ABSENT, anchors
    if oldest > target_date:
        log(
            f"list_page: beyond_window — oldest closing {oldest} "
            f"> target {target_date.isoformat()}"
        )
        return None, LIST_BEYOND, anchors

    # Inside stream window but no card for this date.
    # Weekends → hard absent. Weekdays → allow fallback (list lag / slug drift).
    if target_date.weekday() >= 5:
        log(
            f"list_page: absent — weekend {target_date.isoformat()} in "
            f"[{oldest}, {newest}] with no closing review"
        )
        return None, LIST_ABSENT, anchors

    log(
        f"list_page: beyond_window — weekday {target_date.isoformat()} in "
        f"[{oldest}, {newest}] missing from list (allow id_scan fallback)"
    )
    return None, LIST_BEYOND, anchors


def _seed_date_to_id_from_anchors(
    cache_dir: Path,
    workdir: Path | None,
    anchors: list[tuple[date, int]],
) -> None:
    """Persist list-page (date, id) pairs so forecast has anchors on cold cache."""
    if not anchors:
        return
    cache = _load_bht_cache(workdir, cache_dir)
    d2i = cache.setdefault("date_to_id", {})
    changed = False
    for art_date, aid in anchors:
        key = art_date.isoformat()
        if d2i.get(key) != aid:
            d2i[key] = aid
            changed = True
    if changed:
        _save_bht_cache(cache_dir, cache)
        log(f"Seeded date_to_id with {len(anchors)} list-page anchors")


def _find_in_list_page(target_date: date, list_url: str = LIST_PAGE_URL) -> Optional[dict]:
    """Backward-compatible wrapper: return hit payload or None."""
    hit, _status, _anchors = _scan_list_page(target_date, list_url)
    return hit


def _find_in_borsa_related(target_date: date, borsa_url: str = FALLBACK_URL) -> Optional[dict]:
    """Scan /borsa 'İlgili Haberler' (or bottom related links) for closing review."""
    try:
        resp = _SESSION.get(borsa_url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"Borsa page fetch failed: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    links: list = []

    # Locate a heading that looks like "İlgili Haberler" / related news.
    heading = None
    for el in soup.find_all(string=re.compile(r"İlgili\s+Haber", re.IGNORECASE)):
        heading = el.parent if hasattr(el, "parent") else None
        if heading:
            break
    if heading:
        container = heading.find_parent(["section", "div", "aside", "ul"]) or heading.parent
        if container:
            for link in container.find_all("a", href=True):
                links.append(link)
                if len(links) >= TOP_BORSA_RELATED:
                    break
            # Sibling containers sometimes hold the actual cards
            if len(links) < 3 and heading.parent:
                sib = heading.parent.find_next_sibling()
                hops = 0
                while sib is not None and hops < 3 and len(links) < TOP_BORSA_RELATED:
                    for link in sib.find_all("a", href=True):
                        if link not in links:
                            links.append(link)
                        if len(links) >= TOP_BORSA_RELATED:
                            break
                    sib = sib.find_next_sibling()
                    hops += 1

    if not links:
        # Degenerate: take the last TOP_BORSA_RELATED article-like links on page
        all_links = list(soup.find_all("a", href=True))
        related = [
            a for a in all_links
            if SLUG_RE.search(a.get("href") or "")
            or TITLE_RE.search(_link_label(a))
        ]
        links = related[-TOP_BORSA_RELATED:] if related else all_links[-TOP_BORSA_RELATED:]

    for link in links[:TOP_BORSA_RELATED]:
        hit = _candidate_from_link(link, target_date, method="borsa_related")
        if hit:
            log(f"borsa_related hit: {hit.get('url')}")
            return hit
    log(f"borsa_related: no closing review for {target_date.isoformat()}")
    return None


def _load_bht_cache(workdir: Path | None, cache_dir: Path) -> dict:
    empty = {"last_max_id": 0, "known_hit_ids": [], "date_to_id": {}}
    candidate_paths: list[Path] = []
    if workdir:
        candidate_paths.append(Path(workdir) / ".cache" / "bht_id_cache.json")
    # cache_dir is already the skill's .cache/turkey-close-report directory;
    # _save_bht_cache writes directly there (no nested .cache/).
    candidate_paths.append(Path(cache_dir) / "bht_id_cache.json")
    # Backward-compat: older layouts nested .cache/ under cache_dir too.
    candidate_paths.append(Path(cache_dir) / ".cache" / "bht_id_cache.json")
    for path in candidate_paths:
        if not path.is_file():
            continue
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
            cache.setdefault("date_to_id", {})
            cache.setdefault("known_hit_ids", [])
            cache.setdefault("last_max_id", 0)
            return cache
        except Exception:
            continue
    return empty


def _save_bht_cache(cache_dir: Path, cache: dict) -> None:
    path = cache_dir / "bht_id_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_date_to_id(cache_dir: Path, cache: dict, date_iso: str, article_id: int) -> None:
    cache.setdefault("date_to_id", {})[date_iso] = article_id
    known = set(cache.get("known_hit_ids", []))
    known.add(article_id)
    cache["known_hit_ids"] = sorted(known, reverse=True)[:50]
    _save_bht_cache(cache_dir, cache)


def fetch_rss_max_id(rss_url: str = f"{BASE}/rss") -> Optional[int]:
    try:
        resp = _SESSION.get(rss_url, timeout=40)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"RSS max-id fetch failed: {e}")
        return None
    soup = BeautifulSoup(resp.text, "xml")
    max_id = 0
    for link in soup.find_all("link"):
        m = re.search(r"-(\d{6,})$", link.get_text(strip=True))
        if m:
            max_id = max(max_id, int(m.group(1)))
    return max_id or None


def probe_id(article_id: int, retries: int = 2) -> Optional[dict]:
    url = f"{BASE}/x-{article_id}"
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        except requests.RequestException:
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return None
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return None
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.find("title")
        if not title_tag:
            return None
        title = title_tag.get_text(strip=True)
        if not TITLE_RE.search(title):
            return None
        return {
            "id": article_id,
            "url": resp.url,
            "title": title.split("|")[0].strip(),
            "date": parse_tr_date_from_title(title),
            "html": resp.text,
        }
    return None


def scan_ids(start_id: int, count: int, workers: int = 8) -> list[dict]:
    hits: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(probe_id, start_id - i): start_id - i for i in range(count)}
        for fut in as_completed(futures):
            art = fut.result()
            if art:
                hits.append(art)
    hits.sort(key=lambda a: -a["id"])
    return hits


def _scan_with_cache(
    current_max_id: int,
    cache: dict,
    cache_dir: Path,
    full_scan_count: int = 450,
) -> list[dict]:
    last_max = cache.get("last_max_id", 0)
    known_hit_ids = cache.get("known_hit_ids", [])

    if last_max and current_max_id <= last_max and known_hit_ids:
        hits = scan_ids(current_max_id, MIN_RECENT_SCAN)
        seen = {h["id"] for h in hits}
        for aid in known_hit_ids:
            if aid in seen:
                continue
            art = probe_id(aid)
            if art:
                hits.append(art)
        hits.sort(key=lambda a: -a["id"])
    elif last_max and current_max_id > last_max:
        scan_count = min(current_max_id - last_max + 50, full_scan_count)
        hits = scan_ids(current_max_id, scan_count)
        seen = {h["id"] for h in hits}
        for aid in known_hit_ids:
            if aid in seen:
                continue
            art = probe_id(aid)
            if art:
                hits.append(art)
        hits.sort(key=lambda a: -a["id"])
    else:
        hits = scan_ids(current_max_id, full_scan_count)

    if hits:
        cache["last_max_id"] = max(last_max, current_max_id)
        all_hit_ids = list({h["id"] for h in hits} | set(known_hit_ids))
        cache["known_hit_ids"] = sorted(all_hit_ids, reverse=True)[:50]
        for h in hits:
            if h.get("date"):
                cache.setdefault("date_to_id", {})[h["date"].isoformat()] = h["id"]
        _save_bht_cache(cache_dir, cache)
    return hits


def _forecast_target_id(
    target_date: date,
    date_to_id: dict[str, int],
) -> Optional[int]:
    """Extrapolate expected article ID from historical (date, id) pairs.

    Uses the oldest→newest span in date_to_id for slope (not just the last
    two days). Adjacent-day slopes are noisy (~150+/day) and push a 10-day
    backward extrapolation outside the ±FORECAST_SCAN_WINDOW band.
    """
    if not date_to_id:
        return None
    pairs: list[tuple[date, int]] = []
    for d_iso, aid in date_to_id.items():
        try:
            pairs.append((date.fromisoformat(d_iso), int(aid)))
        except (ValueError, TypeError):
            continue
    if not pairs:
        return None
    pairs.sort()
    d_new, id_new = pairs[-1]
    if len(pairs) >= 2:
        d_old, id_old = pairs[0]
        days = (d_new - d_old).days or 1
        per_day = max((id_new - id_old) // days, 1)
        per_day = min(per_day, FORECAST_ID_PER_DAY * 2)
    else:
        per_day = FORECAST_ID_PER_DAY
    # Negative delta = historical dates older than newest anchor.
    return id_new + (target_date - d_new).days * per_day


def _forecast_scan(
    target_date: date,
    cache_dir: Path,
    cache: dict,
    date_iso: str,
) -> Optional[dict]:
    """Probe a ±window around the extrapolated article ID in parallel.

    On first hit: cancel remaining futures and shut down immediately — do not
    wait for the whole window to finish.
    """
    center = _forecast_target_id(target_date, cache.get("date_to_id", {}))
    if not center:
        return None
    lo = max(center - FORECAST_SCAN_WINDOW, 1)
    hi = center + FORECAST_SCAN_WINDOW
    log(f"Forecast scan around {center} (range [{lo}, {hi}], target {date_iso})")
    candidates = list(range(hi, lo - 1, -1))
    hit: Optional[dict] = None
    ex = ThreadPoolExecutor(max_workers=FORECAST_WORKERS)
    try:
        futures = {ex.submit(probe_id, aid, 2): aid for aid in candidates}
        for fut in as_completed(futures):
            art = fut.result()
            if art and art.get("date") == target_date:
                _record_date_to_id(cache_dir, cache, date_iso, art["id"])
                text = extract_article_text(art["url"], art.get("html"))
                hit = _success(
                    target_date,
                    title=art["title"],
                    url=art["url"],
                    text=text,
                    method="forecast_scan",
                    article_id=art["id"],
                )
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return hit


def _clear_date_to_id(cache_dir: Path, cache: dict, date_iso: str) -> None:
    """Drop a stale date→id mapping so we don't keep probing the wrong article."""
    d2i = cache.get("date_to_id") or {}
    if date_iso in d2i:
        log(f"Clearing stale date_to_id[{date_iso}]={d2i[date_iso]}")
        d2i.pop(date_iso, None)
        cache["date_to_id"] = d2i
        _save_bht_cache(cache_dir, cache)


def _find_by_id_scan(
    target_date: date,
    cache_dir: Path,
    workdir: Path | None,
    rss_url: str,
) -> Optional[dict]:
    """Fallback ID discovery after list_page + borsa_related miss.

    Order: date_to_id probe → RSS recent scan → small forecast window.
    Blind / extended scans are intentionally removed — if list + related news
    don't show the article, treat it as unpublished rather than ID-scanning.
    """
    cache = _load_bht_cache(workdir, cache_dir)
    date_iso = target_date.isoformat()

    cached_id = cache.get("date_to_id", {}).get(date_iso)
    if cached_id:
        log(f"date_to_id cache hit: {date_iso} -> {cached_id}")
        art = probe_id(cached_id)
        if art and art.get("date") == target_date:
            text = extract_article_text(art["url"], art.get("html"))
            return _success(
                target_date,
                title=art["title"],
                url=art["url"],
                text=text,
                method="date_to_id",
                article_id=art["id"],
            )
        # Probe mismatched or dead — clear so we don't loop on a bad id.
        _clear_date_to_id(cache_dir, cache, date_iso)

    # Forecast BEFORE broad RSS scan. Beyond-window historical dates are usually
    # outside the recent RSS band; scanning ~130 IDs first burned ~100s in tests
    # before forecast found the article.
    forecast_hit = _forecast_scan(target_date, cache_dir, cache, date_iso)
    if forecast_hit:
        return forecast_hit

    upper = fetch_rss_max_id(rss_url)
    if upper:
        upper += RSS_LAG_BUFFER
        hits = _scan_with_cache(upper, cache, cache_dir, full_scan_count=MIN_RECENT_SCAN + 50)
        for art in hits:
            if art.get("date") == target_date:
                _record_date_to_id(cache_dir, cache, date_iso, art["id"])
                text = extract_article_text(art["url"], art.get("html"))
                return _success(
                    target_date,
                    title=art["title"],
                    url=art["url"],
                    text=text,
                    method="id_scan",
                    article_id=art["id"],
                )
    return None


def _fetch_via_project(workdir: Path, target_date: date) -> Optional[dict]:
    date_iso = target_date.isoformat()
    fetch_py = workdir / "fetch.py"
    piyasa_py = workdir / "piyasa_ozesi.py"

    if fetch_py.is_file():
        cmd = [sys.executable, str(fetch_py), "closing", date_iso, "--json"]
        script_name = "fetch.py closing"
    elif piyasa_py.is_file():
        cmd = [sys.executable, str(piyasa_py), date_iso, "--json", "--quiet"]
        script_name = "piyasa_ozesi.py"
    else:
        return None

    log(f"Delegating to project {script_name} for {date_iso}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"Project fetcher failed: {e}")
        return None

    if proc.returncode != 0 or not proc.stdout.strip():
        if proc.stderr:
            log(proc.stderr.strip()[:300])
        return None

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not payload.get("ok"):
        return None

    text = payload.get("markdown") or ""
    file_path = payload.get("file_path")
    if not text and file_path:
        fp = Path(file_path)
        if not fp.is_absolute():
            fp = workdir / fp
        if fp.is_file():
            text = fp.read_text(encoding="utf-8")
    if not text:
        return None

    article_id = payload.get("article_id")
    if not article_id and payload.get("url"):
        m = re.search(r"-(\d{6,})(?:[/?#]|$)", payload["url"])
        if m:
            article_id = int(m.group(1))

    return _success(
        target_date,
        title=payload.get("title") or "",
        url=payload.get("url") or "",
        text=text,
        method="project_fetch",
        article_id=article_id,
    )


def fetch_closing_review(
    target_date: date,
    cache_dir: Path,
    *,
    workdir: Path | None = None,
    rss_url: str = f"{BASE}/rss",
    list_page_url: str = LIST_PAGE_URL,
    use_project_fetcher: bool = True,
) -> dict:
    """Fetch BloombergHT closing review using the Turkey-investment fetch chain."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"bloomberght_closing_{target_date.isoformat()}.json"

    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("ok") and cached.get("text"):
                return cached
        except Exception:
            pass

    attempts: list[tuple[str, Optional[dict]]] = []

    manifest = _lookup_manifest(workdir, target_date)
    attempts.append(("manifest", manifest))
    if manifest:
        cache_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    list_hit, list_status, list_anchors = _scan_list_page(target_date, list_page_url)
    _seed_date_to_id_from_anchors(cache_dir, workdir, list_anchors)
    attempts.append(("list_page", list_hit))
    if list_hit:
        cache_file.write_text(json.dumps(list_hit, ensure_ascii=False, indent=2), encoding="utf-8")
        return list_hit

    # Same-day dual path: /borsa İlgili Haberler may still have today's piece
    # even when list_page briefly lags. Always try once after list miss.
    borsa_hit = _find_in_borsa_related(target_date)
    attempts.append(("borsa_related", borsa_hit))
    if borsa_hit:
        cache_file.write_text(json.dumps(borsa_hit, ensure_ascii=False, indent=2), encoding="utf-8")
        return borsa_hit

    # List window says not published / date gap → do NOT burn ID/forecast scans.
    if list_status == LIST_ABSENT:
        result = _failure(
            target_date,
            error=(
                "BloombergHT closing review not on tum-piyasa-haberleri "
                "(not published yet, or no article for this date in the "
                "newest→oldest list window). borsa_related also missed."
            ),
            method="list_absent",
        )
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    if use_project_fetcher and workdir:
        project_hit = _fetch_via_project(workdir, target_date)
        attempts.append(("project_fetch", project_hit))
        if project_hit:
            cache_file.write_text(json.dumps(project_hit, ensure_ascii=False, indent=2), encoding="utf-8")
            return project_hit

    # Only when target is older than the list HTML window (or list empty/error).
    scan_hit = _find_by_id_scan(target_date, cache_dir, workdir, rss_url)
    attempts.append(("id_scan", scan_hit))
    if scan_hit:
        cache_file.write_text(json.dumps(scan_hit, ensure_ascii=False, indent=2), encoding="utf-8")
        return scan_hit

    tried = [name for name, hit in attempts if hit is None]
    result = _failure(
        target_date,
        error=(
            "No matching BloombergHT closing review found. "
            f"Tried: {', '.join(tried)} (list_status={list_status}). "
            "If the article exists, retry later or use fetch.py closing by-id."
        ),
        method="none",
    )
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
