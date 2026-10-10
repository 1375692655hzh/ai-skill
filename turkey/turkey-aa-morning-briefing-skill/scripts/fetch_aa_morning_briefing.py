#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch Anadolu Agency English Morning Briefing for a given date."""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import warnings

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from reorder_sections import reorder_news_in_brief_last

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

TR_TZ = timezone(timedelta(hours=3))
SEARCH_URL = "https://www.aa.com.tr/en/search?s=Morning+Briefing"
WORLD_URL = "https://www.aa.com.tr/en/world"
WORLD_RSS_URL = "https://www.aa.com.tr/en/rss/default?cat=world"
BASE = "https://www.aa.com.tr"

MONTH_NAMES = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def slug_for_date(d: date) -> str:
    """AA slug uses unpadded day: morning-briefing-july-9-2026."""
    return f"morning-briefing-{MONTH_NAMES[d.month - 1]}-{d.day}-{d.year}"


def slug_variants_for_date(d: date) -> list[str]:
    """All slug variants AA may use for the day.

    AA's URL pattern is inconsistent:
    - Most days: `morning-briefing-<month>-<day>-<year>` (full month name)
    - Aug: `morning-briefing-aug-<day>-<year>` (3-letter month abbreviation)
    - Sep 1, 2026 onward: `morning-briefing-sept-<day>-<year>-<weekday>`
      (4-letter abbreviation + weekday suffix)

    Returns a deduplicated list of needles for fuzzy URL matching. The full
    canonical slug is always first; subsequent variants are tried if the
    primary fails.
    """
    canonical = slug_for_date(d)
    month_full = MONTH_NAMES[d.month - 1]
    month_abbr3 = month_full[:3]  # jan, feb, ..., sep, oct, nov, dec
    weekday = d.strftime("%A").lower()  # monday, tuesday, ...

    variants: list[str] = [canonical]
    # 3-letter abbreviation (e.g. aug, sep)
    if month_abbr3 != month_full:
        variants.append(f"morning-briefing-{month_abbr3}-{d.day}-{d.year}")
    # 4-letter abbreviation (e.g. sept) — distinct from 3-letter for September
    month_abbr4 = month_full[:4]
    if month_abbr4 not in (month_full, month_abbr3):
        variants.append(f"morning-briefing-{month_abbr4}-{d.day}-{d.year}")
    # Weekday suffix variants (Sep 1, 2026 introduced `-tuesday`)
    variants.append(f"morning-briefing-{month_full}-{d.day}-{d.year}-{weekday}")
    if month_abbr3 != month_full:
        variants.append(
            f"morning-briefing-{month_abbr3}-{d.day}-{d.year}-{weekday}"
        )
    if month_abbr4 not in (month_full, month_abbr3):
        variants.append(
            f"morning-briefing-{month_abbr4}-{d.day}-{d.year}-{weekday}"
        )

    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def title_for_date(d: date) -> str:
    return f"Morning Briefing: {MONTH_NAMES[d.month - 1].title()} {d.day}, {d.year}"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # AA is the highest-failure task (66.7%); add retry/backoff so a single
    # transient 5xx / TLS flap doesn't gen_fail the whole briefing.
    # Bound retries: AA is flaky but total=4×long timeout previously burned
    # multi-minute wall time before gen_fail. Two attempts is enough for flaps.
    retry = Retry(
        total=2,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _decode_js_unicode(text: str) -> str:
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


def _unescape_js_string(text: str) -> str:
    text = text.replace(r"\"", '"').replace(r"\/", "/")
    text = text.replace(r"\n", "\n").replace(r"\t", "\t")
    text = text.replace(r"\\", "\\")
    return unescape(text)


def _urls_from_slug_id(slug_id: str) -> list[str]:
    """Build candidate article URLs. AA may file under world/ or general/."""
    slug_id = slug_id.replace("\\", "").strip().strip("/")
    if not slug_id.startswith("morning-briefing-"):
        return []
    return [
        f"{BASE}/en/world/{slug_id}",
        f"{BASE}/en/general/{slug_id}",
    ]


def _extract_links_from_html(html: str) -> list[str]:
    links: list[str] = []

    # Full path variants: /en/world|general/morning-briefing-...
    for m in re.finditer(
        r"(?:https?:\\?/\\?/www\.aa\.com\.tr)?\\?/en\\?/(?:world|general)\\?/"
        r"(morning-briefing-[a-z]+-\d{1,2}-\d{4}/\d+)",
        html,
        re.I,
    ):
        links.extend(_urls_from_slug_id(m.group(1)))

    # RSC routeLink: "world/..." or "general/..."
    for m in re.finditer(
        r"routeLink\\?\"?\s*:\s*\\?\"?(?:world|general)/"
        r"(morning-briefing-[a-z]+-\d{1,2}-\d{4}/\d+)",
        html,
        re.I,
    ):
        links.extend(_urls_from_slug_id(m.group(1)))

    # Bare slug/id anywhere in payload
    for m in re.finditer(r"(morning-briefing-[a-z]+-\d{1,2}-\d{4}/\d+)", html, re.I):
        links.extend(_urls_from_slug_id(m.group(1)))

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "morning-briefing-" in href.lower():
            links.append(urljoin(BASE, href))

    seen = set()
    out = []
    for u in links:
        u = u.split("#")[0].split("?")[0]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_working_url(
    urls: list[str],
    needles: str | list[str],
    sess: requests.Session,
) -> Optional[str]:
    """Prefer /world/ URLs that contain any needle and return HTTP 200 with title match.

    `needles` may be a single string or a list of variants (full slug + 3-letter /
    4-letter abbreviations + weekday suffix). All variants are accepted as URL
    matches; the first match wins.
    """
    if isinstance(needles, str):
        needles = [needles]
    needles_lower = [n.lower() for n in needles]
    ordered = [u for u in urls if any(n in u.lower() for n in needles_lower)]
    # Prefer world over general
    ordered.sort(key=lambda u: (0 if "/en/world/" in u else 1, u))
    for url in ordered:
        try:
            resp = sess.get(url, timeout=25, allow_redirects=True)
            if resp.status_code != 200:
                continue
            # Reject soft-404 / wrong article: title or slug must match any needle
            url_lower = resp.url.lower()
            body_head = resp.text.lower()[:8000]
            if not any(n in url_lower or n in body_head for n in needles_lower):
                continue
            if "Morning Briefing" not in resp.text and not any(
                n in url_lower for n in needles_lower
            ):
                continue
            return resp.url.split("#")[0].split("?")[0]
        except Exception:
            continue
    # If HEAD/GET probing fails, still return first world candidate
    return ordered[0] if ordered else None


def discover_briefing_url(
    target_date: date,
    session: Optional[requests.Session] = None,
    *,
    force_url: Optional[str] = None,
) -> dict:
    """Find the Morning Briefing article URL for target_date."""
    if force_url:
        return {"ok": True, "url": force_url, "discover_source": "force_url"}

    sess = session or _session()
    needles = slug_variants_for_date(target_date)
    needle = needles[0]  # primary for logging
    title = title_for_date(target_date)
    candidates: list[tuple[str, str]] = []

    def _matches_any_needle(url: str) -> bool:
        u = url.lower()
        return any(n in u for n in needles)

    def _add_from_html(html: str, source: str) -> None:
        for url in _extract_links_from_html(html):
            if _matches_any_needle(url):
                candidates.append((url, source))

    # 1) Date-specific search (most reliable for older / general/ routes)
    try:
        # Build search queries: full title + abbreviated title (Sept. vs September)
        # AA's display title uses 4-letter abbreviations for some months.
        month_full_title = MONTH_NAMES[target_date.month - 1].title()
        month_abbr3_title = month_full_title[:3] + "."  # Sep., Aug.
        month_abbr4_title = (
            month_full_title[:4] + "." if month_full_title[:4] not in
            (month_full_title, month_full_title[:3]) else None
        )
        title_variants = [
            title,
            title.replace(month_full_title, month_abbr3_title),
        ]
        if month_abbr4_title and month_abbr4_title.replace(".", "") != month_abbr3_title.replace(".", ""):
            title_variants.append(title.replace(month_full_title, month_abbr4_title))
        q_urls = (
            [f"https://www.aa.com.tr/en/search?s={needle}"]
            + [f"https://www.aa.com.tr/en/search?s={t.replace(' ', '+')}" for t in title_variants]
            + [SEARCH_URL]
        )
        for qurl in q_urls:
            resp = sess.get(qurl, timeout=30)
            resp.raise_for_status()
            _add_from_html(resp.text, "search")
            if any(_matches_any_needle(u) for u, _ in candidates):
                break
    except Exception as exc:
        print(f"Warning: search discovery failed: {exc}", file=sys.stderr)

    # 2) World RSS
    try:
        resp = sess.get(WORLD_RSS_URL, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            link = getattr(entry, "link", "") or ""
            entry_title = getattr(entry, "title", "") or ""
            link_lower = link.lower()
            if any(n in link_lower for n in needles) or title.lower() in entry_title.lower():
                candidates.append((link, "world_rss"))
        _add_from_html(resp.text, "world_rss_raw")
    except Exception as exc:
        print(f"Warning: RSS discovery failed: {exc}", file=sys.stderr)

    # 3) World listing page
    try:
        resp = sess.get(WORLD_URL, timeout=30)
        resp.raise_for_status()
        _add_from_html(resp.text, "world_page")
    except Exception as exc:
        print(f"Warning: world page discovery failed: {exc}", file=sys.stderr)

    if not candidates:
        return {
            "ok": False,
            "url": "",
            "discover_source": "",
            "reason": f"not_found:{needle}",
            "search_url": SEARCH_URL,
        }

    # Keep source of first match for logging; probe for a working URL
    source = candidates[0][1]
    url = _pick_working_url([u for u, _ in candidates], needles, sess)
    if not url:
        return {
            "ok": False,
            "url": "",
            "discover_source": source,
            "reason": f"not_found:{needle}",
            "search_url": SEARCH_URL,
        }
    return {"ok": True, "url": url, "discover_source": source, "slug": needle}


def _parse_meta_times(html: str, soup: BeautifulSoup) -> dict:
    published = ""
    modified = ""
    page_date = ""
    page_update = ""
    author = ""

    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "NewsArticle":
                published = published or str(item.get("datePublished") or "")
                modified = modified or str(item.get("dateModified") or "")
                author_obj = item.get("author") or {}
                if isinstance(author_obj, dict):
                    author = author or str(author_obj.get("name") or "")
                elif isinstance(author_obj, list) and author_obj:
                    author = author or str(author_obj[0].get("name") or "")

    def meta_content(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return (tag.get("content") or "").strip() if tag else ""

    published = published or meta_content("article:published_time")
    modified = modified or meta_content("article:modified_time")

    # Visible page labels: "16 July 2026" + "Update : 16 July 2026"
    date_nodes = [
        n.get_text(" ", strip=True)
        for n in soup.select(".text-dateColor, .text-dateColor\\!")
    ]
    joined = " ".join(date_nodes)
    m_update = re.search(r"Update\s*:?\s*(\d{1,2}\s+\w+\s+\d{4})", joined, re.I)
    if m_update:
        page_update = m_update.group(1).strip()
    m_date = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", joined)
    if m_date:
        page_date = m_date.group(1).strip()

    # Normalize ISO-like timestamps: AA omits offset; treat as Turkey local (+03:00)
    def attach_tr_offset(ts: str) -> str:
        if not ts:
            return ""
        if re.search(r"(Z|[+-]\d{2}:?\d{2})$", ts):
            return ts
        # "2026-07-16T08:36:17.11" → assume TR
        return ts + "+03:00"

    published_tr = attach_tr_offset(published)
    modified_tr = attach_tr_offset(modified)

    # Human-readable TR / Beijing
    def format_dual(ts: str) -> dict:
        if not ts:
            return {"raw": "", "tr": "", "beijing": "", "unix": None}
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TR_TZ)
            tr = dt.astimezone(TR_TZ)
            bj = dt.astimezone(timezone(timedelta(hours=8)))
            return {
                "raw": ts,
                "tr": tr.strftime("%Y-%m-%d %H:%M:%S %z"),
                "beijing": bj.strftime("%Y-%m-%d %H:%M:%S %z"),
                "unix": int(tr.timestamp()),
            }
        except Exception:
            return {"raw": ts, "tr": ts, "beijing": "", "unix": None}

    return {
        "author": author,
        "page_date": page_date,
        "page_update": page_update,
        "published": format_dual(published_tr),
        "modified": format_dual(modified_tr),
    }


def _extract_body_html_from_scripts(html: str) -> str:
    """AA Next.js pages embed article HTML in script payloads with \\u003c escapes.

    Since 2026-08, AA also renders the body directly into HTML inside a
    <div class="embed-responsive prose ..."> container. We try the script
    pattern first (it has the longest, cleanest content), then fall back to
    the prose div if no script payload yielded anything. Both paths return
    HTML suitable for ``_html_to_text``.
    """
    soup = BeautifulSoup(html, "html.parser")
    best = ""

    # 1) Original: extract from Next.js script payload (preferred when AA still
    #    embeds the body in RSC payloads — gives the cleanest structure).
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if "TOP STORIES" not in raw and "NEWS IN BRIEF" not in raw:
            continue
        decoded = _decode_js_unicode(raw)
        # Prefer the rundown intro paragraph used by Morning Briefing
        patterns = [
            r'(<p>Here.?s a rundown[\s\S]*?</p>(?:[\s\S]*?<p>[\s\S]*?</p>)*)',
            r'(Here.?s a rundown[\s\S]*?)(?:\"\]\)|\"\])',
        ]
        for pat in patterns:
            m = re.search(pat, decoded, re.I)
            if not m:
                continue
            chunk = m.group(1)
            # Trim flight-data trailing junk
            for stop in ['"])', '"]]', "US-Iran war", "LATEST NEWS"]:
                cut = chunk.find(stop)
                if cut > 1000:
                    chunk = chunk[:cut]
            if len(chunk) > len(best):
                best = chunk
    if best:
        return best

    # 2) Fallback: body is rendered directly to the DOM inside
    #    <div class="embed-responsive prose ...">. AA changed its renderer in
    #    late Aug 2026; the prose div is the only reliable source after that
    #    switch. Long-term guard against further regression — see MEMORY.md
    #    "08-30: AA 改版，正文不再嵌入 RSC script payload".
    prose_div = soup.find(
        "div",
        class_=re.compile(r"embed-responsive\s+prose", re.I),
    )
    if prose_div:
        return str(prose_div)
    return best


def _html_to_text(body_html: str) -> str:
    cleaned = _unescape_js_string(body_html)
    # Drop trailing empty/noise tags leftovers
    cleaned = re.sub(r'"\]\)\s*$', "", cleaned)
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    lines: list[str] = []
    for el in soup.find_all(["p", "li", "h2", "h3", "h4"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    if lines:
        return "\n\n".join(lines).strip()

    # Fallback
    text = soup.get_text("\n", strip=True)
    return "\n".join(line for line in text.splitlines() if line.strip())


def _rss_pubdate_for_url(url: str, session: requests.Session) -> str:
    try:
        resp = session.get(WORLD_RSS_URL, timeout=25)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            if getattr(entry, "link", "") == url:
                return str(getattr(entry, "published", "") or "")
        # Regex fallback
        for m in re.finditer(r"(?is)<item>(.*?)</item>", resp.text):
            block = m.group(1)
            if url.split("/")[-1] in block or url in block:
                pm = re.search(r"<pubDate>(.*?)</pubDate>", block)
                if pm:
                    return pm.group(1).strip()
    except Exception:
        pass
    return ""


def fetch_article(url: str, session: Optional[requests.Session] = None) -> dict:
    sess = session or _session()
    resp = sess.get(url, timeout=40)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.select_one("h1.category-detail-subtitle, h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        title = (og.get("content") or "").strip() if og else ""

    times = _parse_meta_times(html, soup)
    rss_pub = _rss_pubdate_for_url(url, sess)

    body_html = _extract_body_html_from_scripts(html)
    body_text = _html_to_text(body_html) if body_html else ""
    if not body_text:
        # Last resort: web-reader style — strip chrome from whole page
        main = soup.find("main") or soup.body
        if main:
            for tag in main(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            body_text = main.get_text("\n", strip=True)

    reordered = reorder_news_in_brief_last(body_text) if body_text else ""

    return {
        "ok": bool(body_text),
        "url": url,
        "title": title,
        "author": times.get("author", ""),
        "page_date": times.get("page_date", ""),
        "page_update": times.get("page_update", ""),
        "published": times.get("published", {}),
        "modified": times.get("modified", {}),
        "rss_pubDate": rss_pub,
        "content_original": body_text,
        "content": reordered,
        "body_html_len": len(body_html),
    }


def build_output_text(article: dict, target_date: date) -> str:
    """Assemble final plain-text file with metadata header + reordered body."""
    pub = article.get("published") or {}
    mod = article.get("modified") or {}
    lines = [
        article.get("title") or title_for_date(target_date),
        "",
        f"Source: {article.get('url', '')}",
        f"Target date: {target_date.isoformat()}",
    ]
    if article.get("author"):
        lines.append(f"Author: {article['author']}")
    if article.get("page_date"):
        lines.append(f"Page date: {article['page_date']}")
    if article.get("page_update"):
        lines.append(f"Page update label: Update: {article['page_update']}")
    if pub.get("tr"):
        lines.append(f"Published (TR UTC+3): {pub['tr']}")
    if pub.get("beijing"):
        lines.append(f"Published (Beijing UTC+8): {pub['beijing']}")
    if mod.get("tr"):
        lines.append(f"Modified (TR UTC+3): {mod['tr']}")
    if mod.get("beijing"):
        lines.append(f"Modified (Beijing UTC+8): {mod['beijing']}")
    if article.get("rss_pubDate"):
        lines.append(f"RSS pubDate: {article['rss_pubDate']}")
    lines.append("Note: NEWS IN BRIEF section moved to bottom (content unchanged).")
    lines.append("")
    lines.append("=" * 72)
    lines.append("")
    lines.append((article.get("content") or "").rstrip())
    lines.append("")
    return "\n".join(lines)


def fetch_aa_morning_briefing(
    target_date: date,
    cache_dir: Path,
    *,
    force_url: Optional[str] = None,
    force_refresh: bool = False,
) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"aa_morning_briefing_{target_date.isoformat()}.json"
    if cache_file.exists() and not force_refresh:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("ok") and cached.get("content"):
            return cached

    sess = _session()
    discovery = discover_briefing_url(target_date, sess, force_url=force_url)
    if not discovery.get("ok"):
        result = {
            "ok": False,
            "reason": discovery.get("reason", "not_found"),
            "target_date": target_date.isoformat(),
            "url": "",
            "search_url": SEARCH_URL,
            "content": "",
            "content_original": "",
        }
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    article = fetch_article(discovery["url"], sess)
    result = {
        "ok": bool(article.get("ok")),
        "reason": "ok" if article.get("ok") else "empty_body",
        "target_date": target_date.isoformat(),
        "discover_source": discovery.get("discover_source", ""),
        "slug": discovery.get("slug", slug_for_date(target_date)),
        **article,
        "output_text": build_output_text(article, target_date) if article.get("ok") else "",
    }
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    from runtime_utils import configure_stdio

    configure_stdio()
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".cache/turkey-aa-morning-briefing")
    r = fetch_aa_morning_briefing(target, cache, force_refresh=True)
    print(json.dumps({k: v for k, v in r.items() if k not in ("content", "content_original", "output_text")}, ensure_ascii=False, indent=2))
    print("\n--- OUTPUT PREVIEW ---\n")
    print((r.get("output_text") or "")[:2500])
