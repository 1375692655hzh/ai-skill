#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch recent posts from X watchlist via xAI Responses API + x_search."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from zoneinfo import ZoneInfo


SKILL_DIR = Path(__file__).resolve().parent.parent
HERMES_STATE = Path.home() / ".hermes" / "x-influencer-market-monitor"
TR_TZ = ZoneInfo("Europe/Istanbul")
URL_RE = re.compile(r"https?://[^\s\]\)\"']+")
STATUS_URL_RE = re.compile(
    r"https?://(?:x|twitter)\.com/([^/\s\"'?]+)/status/(\d+)",
    re.IGNORECASE,
)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _load_env_files() -> None:
    for env_path in (HERMES_STATE / ".env", SKILL_DIR / ".env"):
        if not env_path.exists():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except (OSError, UnicodeDecodeError):
            continue


_load_env_files()


@dataclass
class Account:
    handle: str
    name: str = ""
    homepage: str = ""
    priority: str = "medium"
    notes: str = ""
    enabled: bool = True

    @property
    def clean_handle(self) -> str:
        handle = self.handle.strip()
        if handle.startswith(("https://x.com/", "https://twitter.com/")):
            return "@" + handle.rstrip("/").split("/")[-1]
        return handle if handle.startswith("@") else f"@{handle}"

    @property
    def bare_handle(self) -> str:
        return self.clean_handle.lstrip("@")

    @property
    def homepage_url(self) -> str:
        if self.homepage:
            return self.homepage
        return f"https://x.com/{self.bare_handle}"


def _progress(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}


def load_accounts_yaml(
    path: Path,
    *,
    min_priority: str = "low",
    tiers: Optional[list[str]] = None,
) -> list[Account]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("accounts", []) if isinstance(data, dict) else []
    min_rank = _PRIORITY_RANK.get(str(min_priority).lower(), 1)
    tier_set = {t.lower() for t in (tiers or []) if t}
    accounts: list[Account] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        handle = str(item.get("handle") or item.get("username") or "").strip()
        if not handle:
            continue
        priority = str(item.get("priority") or "medium").lower()
        if _PRIORITY_RANK.get(priority, 2) < min_rank:
            continue
        tier = str(item.get("tier") or "").lower()
        if tier_set and tier not in tier_set:
            continue
        accounts.append(
            Account(
                handle=handle,
                name=str(item.get("name") or ""),
                homepage=str(item.get("homepage") or ""),
                priority=priority,
                notes=str(item.get("notes") or ""),
            )
        )
    return accounts


def resolve_accounts_path(skill_dir: Path, configured: str) -> Path:
    candidates = []
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = (skill_dir / p).resolve()
        candidates.append(p)
    candidates.extend(
        [
            skill_dir / "accounts.yaml",
            HERMES_STATE / "accounts.yaml",
            skill_dir / "accounts.example.yaml",
        ]
    )
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "No accounts file found. Copy accounts.example.yaml to accounts.yaml "
        "or set accounts_path in config.json."
    )


def compact_text(text: str, limit: int = 4000) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def first_str(item: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        v = item.get(key)
        if isinstance(v, (str, int, float)):
            return str(v)
    return ""


def parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        number = int(value)
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(number, tz=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def normalize_metrics(raw: Any) -> dict[str, int]:
    out = {"likes": 0, "retweets": 0, "replies": 0, "quotes": 0, "views": 0, "bookmarks": 0}
    if not isinstance(raw, dict):
        return out
    mapping = {
        "likes": ["likes", "like_count", "favorite_count", "favorites"],
        "retweets": ["retweets", "retweet_count", "reposts"],
        "replies": ["replies", "reply_count"],
        "quotes": ["quotes", "quote_count"],
        "views": ["views", "view_count", "impression_count"],
        "bookmarks": ["bookmarks", "bookmark_count"],
    }
    for out_key, aliases in mapping.items():
        for alias in aliases:
            v = raw.get(alias)
            if v is None:
                continue
            try:
                out[out_key] = int(v)
                break
            except (TypeError, ValueError):
                continue
    return out


def extract_media_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("media", "images", "image_urls", "media_urls", "photos"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.startswith("http"):
            urls.append(raw)
        elif isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, str) and entry.startswith("http"):
                    urls.append(entry)
                elif isinstance(entry, dict):
                    u = first_str(entry, ["url", "media_url", "media_url_https", "src", "href"])
                    if u.startswith("http"):
                        urls.append(u)
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def normalize_post(item: Any, account: Account) -> dict[str, Any]:
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        item = {}

    text = first_str(item, ["text", "full_text", "content", "body"])
    created_raw = first_str(
        item,
        ["createdAtISO", "createdAt", "created_at", "date", "datetime", "timestamp", "published_at"],
    )
    url = first_str(item, ["url", "link", "permalink", "tweet_url"])
    tweet_id = str(item.get("id") or item.get("tweet_id") or item.get("rest_id") or "")
    if not tweet_id and url:
        m = STATUS_URL_RE.search(url)
        if m:
            tweet_id = m.group(2)
    if not url and tweet_id:
        url = f"{account.homepage_url}/status/{tweet_id}"

    parsed_time = parse_datetime(created_raw) if created_raw else None
    metrics = normalize_metrics(item.get("metrics") or item.get("public_metrics") or item.get("engagement") or {})
    author = item.get("author") or item.get("user") or {}
    if isinstance(author, dict):
        author_handle = str(
            author.get("handle")
            or author.get("screenName")
            or author.get("screen_name")
            or author.get("username")
            or account.clean_handle
        )
        author_name = str(author.get("name") or account.name or "")
    else:
        author_handle = str(author or account.clean_handle)
        author_name = account.name

    is_retweet = bool(item.get("isRetweet") or item.get("is_retweet") or item.get("retweeted"))
    quoted = item.get("quotedTweet") or item.get("quoted_tweet") or item.get("quoted")
    quoted_text = ""
    quoted_handle = ""
    if isinstance(quoted, dict):
        quoted_text = str(quoted.get("text") or quoted.get("full_text") or "")
        q_author = quoted.get("author") or {}
        if isinstance(q_author, dict):
            quoted_handle = str(
                q_author.get("screenName") or q_author.get("screen_name") or q_author.get("handle") or ""
            )
    media = extract_media_urls(item)

    return {
        "account": account.clean_handle,
        "account_name": account.name or author_name,
        "account_priority": account.priority,
        "account_homepage": account.homepage_url,
        "author_handle": "@" + author_handle.lstrip("@"),
        "author_name": author_name,
        "text": compact_text(text, limit=4000),
        "url": url or "",
        "id": tweet_id,
        "created_at": parsed_time.isoformat() if parsed_time else None,
        "raw_time": created_raw,
        "metrics": metrics,
        "media": media,
        "is_retweet": is_retweet,
        "quoted_text": quoted_text,
        "quoted_handle": quoted_handle,
        "translation": "",
        "quoted_translation": "",
    }


def resolve_fetch_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return TR_TZ


def resolve_fetch_window(
    *,
    now: Optional[datetime] = None,
    tz_name: str = "Europe/Istanbul",
) -> tuple[datetime, datetime]:
    """Return (window_start, window_end) in UTC: yesterday 00:00 local → now."""
    tz = resolve_fetch_timezone(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    start_local = datetime.combine(now_local.date() - timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start_local.astimezone(timezone.utc), now_local.astimezone(timezone.utc)


def filter_window(posts: list[dict], window_start: datetime, window_end: datetime) -> list[dict]:
    kept = []
    for post in posts:
        created = parse_datetime(post.get("created_at") or "")
        if created is None:
            kept.append(post)  # keep uncertain timestamps
            continue
        if window_start <= created <= window_end:
            kept.append(post)
    return kept


def dedupe_posts(posts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for p in posts:
        key = p.get("id") or p.get("url") or (str(p.get("account")) + "|" + (p.get("text") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _extract_json_candidates(text: str) -> list[Any]:
    candidates: list[Any] = []
    stripped = (text or "").strip()
    if not stripped:
        return candidates
    try:
        candidates.append(json.loads(stripped))
    except json.JSONDecodeError:
        pass
    for m in JSON_BLOCK_RE.finditer(stripped):
        block = m.group(1).strip()
        try:
            candidates.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    # brace / bracket scan for embedded JSON
    for opener, closer in (("[", "]"), ("{", "}")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start >= 0 and end > start:
            snippet = stripped[start : end + 1]
            try:
                candidates.append(json.loads(snippet))
            except json.JSONDecodeError:
                continue
    return candidates


def _items_from_parsed(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("posts", "tweets", "results", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        if any(k in data for k in ("text", "full_text", "url", "id")):
            return [data]
    return []


def _walk_collect_strings(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_collect_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_collect_strings(v, out)


def _collect_output_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    if isinstance(response.get("output_text"), str):
        chunks.append(response["output_text"])
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        t = part.get("text") or part.get("output_text") or ""
                        if isinstance(t, str) and t.strip():
                            chunks.append(t)
            elif isinstance(content, str) and content.strip():
                chunks.append(content)
            if isinstance(item.get("text"), str):
                chunks.append(item["text"])
    return "\n".join(chunks)


def _posts_from_citations(response: dict[str, Any], account: Account) -> list[dict]:
    posts: list[dict] = []
    citations = response.get("citations") or []
    if not isinstance(citations, list):
        citations = []
    # Also dig URLs from nested structures
    url_blobs: list[str] = []
    _walk_collect_strings(response, url_blobs)
    urls: list[str] = []
    for c in citations:
        if isinstance(c, str):
            urls.append(c)
        elif isinstance(c, dict):
            u = first_str(c, ["url", "uri", "link", "permalink", "source"])
            if u:
                urls.append(u)
    for blob in url_blobs:
        for m in STATUS_URL_RE.finditer(blob):
            urls.append(m.group(0))

    seen_ids: set[str] = set()
    for url in urls:
        m = STATUS_URL_RE.search(url)
        if not m:
            continue
        handle, tweet_id = m.group(1), m.group(2)
        if handle.lower() != account.bare_handle.lower():
            continue
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        posts.append(
            normalize_post(
                {
                    "id": tweet_id,
                    "url": f"https://x.com/{account.bare_handle}/status/{tweet_id}",
                    "text": "",
                    "created_at": "",
                },
                account,
            )
        )
    return posts


def parse_xai_response_posts(response: dict[str, Any], account: Account) -> list[dict]:
    # Normalize chat/completions shape → output_text for shared parser
    if isinstance(response.get("choices"), list) and response["choices"]:
        choice0 = response["choices"][0] if isinstance(response["choices"][0], dict) else {}
        msg = choice0.get("message") if isinstance(choice0, dict) else {}
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            response = {**response, "output_text": msg["content"]}

    text = _collect_output_text(response)
    posts: list[dict] = []
    for candidate in _extract_json_candidates(text):
        for item in _items_from_parsed(candidate):
            posts.append(normalize_post(item, account))

    # Merge citation URLs (fill gaps when model omitted JSON body)
    by_id: dict[str, dict] = {}
    for p in posts:
        key = p.get("id") or p.get("url") or ""
        if key:
            by_id[key] = p
    for cite_post in _posts_from_citations(response, account):
        key = cite_post.get("id") or cite_post.get("url") or ""
        if not key:
            continue
        if key not in by_id:
            by_id[key] = cite_post
        else:
            existing = by_id[key]
            if not existing.get("url") and cite_post.get("url"):
                existing["url"] = cite_post["url"]
            if not existing.get("id") and cite_post.get("id"):
                existing["id"] = cite_post["id"]

    result = list(by_id.values()) if by_id else posts
    return [p for p in result if p.get("text") or p.get("url")]


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: Optional[dict] = None,
    timeout: int = 120,
) -> dict[str, Any]:
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urlrequest.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urlerror.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {err_body[:800]}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def normalize_xai_endpoint(base_url: str, api_mode: str = "auto") -> tuple[str, str]:
    """Return (root_base_url, mode) where mode is responses|chat_completions."""
    raw = (base_url or "").strip().rstrip("/")
    mode = (api_mode or "auto").strip().lower()
    if raw.endswith("/chat/completions"):
        return raw[: -len("/chat/completions")], "chat_completions"
    if raw.endswith("/responses"):
        return raw[: -len("/responses")], "responses"
    if mode in ("chat", "chat_completions", "completions"):
        return raw, "chat_completions"
    if mode in ("responses", "response"):
        return raw, "responses"
    if "kovar.ai" in raw.lower():
        return raw, "chat_completions"
    return raw, "responses"


def call_xai_x_search(
    *,
    api_key: str,
    base_url: str,
    model: str,
    account: Account,
    window_start: datetime,
    window_end: datetime,
    older_than: Optional[datetime] = None,
    timeout: int = 120,
    enable_image_understanding: bool = True,
    api_mode: str = "auto",
) -> dict[str, Any]:
    start_local = window_start.astimezone(TR_TZ)
    end_local = window_end.astimezone(TR_TZ)
    from_day = start_local.date().isoformat()
    to_day = end_local.date().isoformat()
    handle = account.bare_handle

    until_clause = ""
    older_clause = ""
    if older_than is not None:
        older_local = older_than.astimezone(TR_TZ)
        until_day = older_local.date().isoformat()
        until_clause = f" until:{until_day}"
        older_clause = (
            f"\nOnly include posts strictly older than {older_than.astimezone(timezone.utc).isoformat()} (UTC)."
        )

    query = f"from:{handle} since:{from_day}{until_clause}"
    prompt = (
        f"Use x_search / X search in Latest / keyword mode for this exact query: {query}\n"
        f"Return ALL original posts by @{handle} published between "
        f"{window_start.astimezone(timezone.utc).isoformat()} and "
        f"{window_end.astimezone(timezone.utc).isoformat()} (UTC)."
        f"{older_clause}\n"
        "Do NOT summarize. For each post output the FULL original text.\n"
        "Respond with ONLY a JSON array. Each element must have keys:\n"
        '  id, url, created_at (ISO8601), text, media (array of image/video URLs), '
        "metrics (likes/retweets/replies/views if available), is_retweet (bool), "
        "quoted_text, quoted_handle.\n"
        "If there are no posts, return []."
    )

    root, mode = normalize_xai_endpoint(base_url, api_mode)
    headers = {"Authorization": f"Bearer {api_key}"}
    tool: dict[str, Any] = {
        "type": "x_search",
        "allowed_x_handles": [handle],
        "from_date": from_day,
        "to_date": to_day,
        "enable_image_understanding": bool(enable_image_understanding),
        "enable_video_understanding": False,
    }

    if mode == "chat_completions":
        url = root.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [tool],
            "temperature": 0,
        }
        try:
            return _http_json("POST", url, headers=headers, body=body, timeout=timeout)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "tool" in msg or "400" in msg or "invalid" in msg or "unsupported" in msg:
                body_plain = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                }
                return _http_json("POST", url, headers=headers, body=body_plain, timeout=timeout)
            raise

    url = root.rstrip("/") + "/responses"
    body = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "tools": [tool],
    }
    try:
        return _http_json(
            "POST",
            url,
            headers=headers,
            body={**body, "include": ["x_search_call_output"]},
            timeout=timeout,
        )
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "include" in msg or "400" in msg or "invalid" in msg:
            return _http_json("POST", url, headers=headers, body=body, timeout=timeout)
        raise


def _oldest_created(posts: list[dict]) -> Optional[datetime]:
    times = []
    for p in posts:
        dt = parse_datetime(p.get("created_at") or "")
        if dt is not None:
            times.append(dt)
    return min(times) if times else None


# ---------------------------------------------------------------------------
# FxTwitter provider (free, keyless). Endpoints mirror the x-daily-watch
# contract: /2/profile/<handle>/statuses (cursor.bottom pagination),
# /2/status/<id>, /2/thread/<id>. `since` is only a 204 fast-path hint on the
# server side — the window is enforced locally via filter_window().
# ---------------------------------------------------------------------------

FXTWITTER_DEFAULT_BASE = "https://api.fxtwitter.com"
FXTWITTER_UA = "turkey-x-influencer-skill/1.2"


def _fxt_media_urls(media: Any) -> list[str]:
    """Flatten FxTwitter media dict ({photos|videos|gifs: [{url,...}]}) to URL list."""
    urls: list[str] = []
    if isinstance(media, dict):
        buckets = [v for v in media.values() if isinstance(v, (list, dict))]
    elif isinstance(media, list):
        buckets = [media]
    else:
        return []
    for bucket in buckets:
        entries = bucket if isinstance(bucket, list) else [bucket]
        for entry in entries:
            if isinstance(entry, str) and entry.startswith("http"):
                urls.append(entry)
            elif isinstance(entry, dict):
                for key in ("url", "thumbnail", "preview"):
                    u = entry.get(key)
                    if isinstance(u, str) and u.startswith("http"):
                        urls.append(u)
                        break
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _fxt_iso_time(status: dict[str, Any]) -> str:
    ts = status.get("created_timestamp")
    try:
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return str(status.get("created_at") or "")


def _fxt_to_item(status: dict[str, Any]) -> dict[str, Any]:
    """Map a FxTwitter v2 status dict onto the normalize_post() input shape."""
    author = status.get("author") if isinstance(status.get("author"), dict) else {}
    quote = status.get("quote") if isinstance(status.get("quote"), dict) else None
    quoted_item = None
    if quote:
        q_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
        quoted_item = {
            "text": (quote.get("raw_text") or {}).get("text") or quote.get("text") or "",
            "author": {
                "screen_name": q_author.get("screen_name") or "",
                "name": q_author.get("name") or "",
            },
        }
    return {
        "id": str(status.get("id") or ""),
        "url": status.get("url") or "",
        "created_at": _fxt_iso_time(status),
        "text": (status.get("raw_text") or {}).get("text") or status.get("text") or "",
        "author": {
            "screen_name": author.get("screen_name") or "",
            "name": author.get("name") or "",
        },
        "metrics": {
            "likes": status.get("likes"),
            "retweets": status.get("reposts"),
            "replies": status.get("replies"),
            "quotes": status.get("quotes"),
            "views": status.get("views"),
            "bookmarks": status.get("bookmarks"),
        },
        "media": _fxt_media_urls(status.get("media")),
        "is_retweet": bool(status.get("reposted_by")),
        "quoted": quoted_item,
    }


def _fxt_expand_results(results: list[Any]) -> list[dict[str, Any]]:
    """Expand type:thread groupings into ordered plain statuses."""
    flat: list[dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "thread" and isinstance(entry.get("statuses"), list):
            for member in entry["statuses"]:
                if isinstance(member, dict):
                    merged = dict(member)
                    merged.setdefault("author", entry.get("author"))
                    flat.append(merged)
        else:
            flat.append(entry)
    return flat


def fetch_account_posts_fxtwitter(
    account: Account,
    *,
    base_url: str = FXTWITTER_DEFAULT_BASE,
    window_start: datetime,
    window_end: datetime,
    per_account_limit: int = 50,
    max_pages: int = 5,
    timeout: int = 60,
    retries: int = 2,
    retry_delay: float = 5.0,
    count: int = 100,
    with_replies: bool = True,
    groupthreads: bool = True,
) -> tuple[list[dict], Optional[dict], bool]:
    """Fetch one account via FxTwitter v2 profile timeline. Returns (posts, failure, truncated)."""
    from urllib.parse import urlencode

    handle = account.bare_handle
    url = f"{(base_url or FXTWITTER_DEFAULT_BASE).rstrip('/')}/2/profile/{handle}/statuses"
    headers = {"Accept": "application/json", "User-Agent": FXTWITTER_UA}
    since_epoch = int(window_start.timestamp())

    all_posts: list[dict] = []
    cursor = ""
    truncated = False
    last_error = ""

    for page in range(max(1, max_pages)):
        params = {
            "count": str(max(1, count)),
            "since": str(since_epoch),
            "with_replies": "true" if with_replies else "false",
            "groupthreads": "true" if groupthreads else "false",
        }
        if cursor:
            params["cursor"] = cursor
        request_url = f"{url}?{urlencode(params)}"

        data: Optional[dict[str, Any]] = None
        for attempt in range(max(1, retries)):
            try:
                data = _http_json("GET", request_url, headers=headers, timeout=timeout)
                last_error = ""
                break
            except Exception as exc:
                last_error = str(exc)
                # 404 = handle gone/renamed; retrying cannot help.
                if "HTTP 404" in last_error:
                    break
                if attempt + 1 < max(1, retries):
                    _progress(f"  retry {attempt + 1} for {account.clean_handle}: {last_error[:120]}")
                    time.sleep(retry_delay)
        if data is None:
            return [], {
                "handle": account.clean_handle,
                "provider": "fxtwitter",
                "error": last_error or "fxtwitter request failed",
            }, truncated

        results = data.get("results") if isinstance(data.get("results"), list) else []
        if not results:
            break

        page_posts = [
            normalize_post(_fxt_to_item(s), account) for s in _fxt_expand_results(results)
        ]
        page_posts = filter_window(page_posts, window_start, window_end)
        before = len(all_posts)
        all_posts = dedupe_posts(all_posts + page_posts)
        new_count = len(all_posts) - before
        _progress(
            f"  page {page + 1}/{max_pages}: parsed={len(page_posts)} "
            f"new={new_count} total={len(all_posts)}"
        )

        # Timeline is newest-first: an empty/fully-duplicated page means older
        # pages cannot contribute anything inside the window.
        if new_count == 0:
            break

        next_cursor = ""
        raw_cursor = data.get("cursor")
        if isinstance(raw_cursor, dict):
            next_cursor = str(raw_cursor.get("bottom") or "")
        if not next_cursor:
            break

        oldest = _oldest_created(page_posts)
        if len(all_posts) >= per_account_limit:
            truncated = True
            break
        if oldest is not None and oldest <= window_start + timedelta(minutes=1):
            break
        cursor = next_cursor
    else:
        truncated = True

    all_posts = dedupe_posts(filter_window(all_posts, window_start, window_end))
    all_posts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    if len(all_posts) > per_account_limit:
        all_posts = all_posts[:per_account_limit]
        truncated = True
    return all_posts, None, truncated


def fetch_watchlist_fxtwitter(
    accounts: list[Account],
    *,
    base_url: str = FXTWITTER_DEFAULT_BASE,
    window_start: datetime,
    window_end: datetime,
    per_account_limit: int = 50,
    max_pages_per_account: int = 5,
    timeout: int = 60,
    account_delay: float = 1.0,
    retries: int = 2,
    retry_delay_seconds: float = 5.0,
    count: int = 100,
    with_replies: bool = True,
    groupthreads: bool = True,
) -> dict[str, Any]:
    posts: list[dict] = []
    failures: list[dict] = []
    truncated_accounts: list[str] = []
    ok_accounts = 0

    for idx, account in enumerate(accounts):
        _progress(f"Fetching [{idx + 1}/{len(accounts)}] {account.clean_handle} via FxTwitter...")
        account_posts, failure, truncated = fetch_account_posts_fxtwitter(
            account,
            base_url=base_url,
            window_start=window_start,
            window_end=window_end,
            per_account_limit=per_account_limit,
            max_pages=max_pages_per_account,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay_seconds,
            count=count,
            with_replies=with_replies,
            groupthreads=groupthreads,
        )
        if failure and not account_posts:
            failures.append(failure)
            _progress(f"  {account.clean_handle}: failed — {failure.get('error', '')[:160]}")
        else:
            posts.extend(account_posts)
            ok_accounts += 1
            if truncated:
                truncated_accounts.append(account.clean_handle)
            _progress(
                f"  {account.clean_handle}: {len(account_posts)} posts"
                + (" (truncated)" if truncated else "")
            )
        if account_delay > 0 and idx < len(accounts) - 1:
            time.sleep(account_delay)

    window_posts = dedupe_posts(filter_window(posts, window_start, window_end))
    window_posts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    hours = max(0.0, (window_end - window_start).total_seconds() / 3600.0)
    return {
        "ok": True,
        "provider": "fxtwitter",
        "posts": window_posts,
        "hours": round(hours, 2),
        "window": "yesterday_start_to_now",
        "since": window_start.isoformat(),
        "until": window_end.isoformat(),
        "raw_count": len(posts),
        "window_count": len(window_posts),
        "failures": failures,
        "truncated_accounts": truncated_accounts,
        "truncated": bool(truncated_accounts),
        "accounts": [a.clean_handle for a in accounts],
        "ok_accounts": ok_accounts,
        "fail_accounts": len(failures),
    }


def fetch_account_posts_xai(
    account: Account,
    *,
    api_key: str,
    base_url: str,
    model: str,
    window_start: datetime,
    window_end: datetime,
    per_account_limit: int = 50,
    max_pages: int = 5,
    page_full_threshold: int = 8,
    timeout: int = 120,
    enable_image_understanding: bool = True,
    retries: int = 2,
    retry_delay: float = 5.0,
    api_mode: str = "auto",
) -> tuple[list[dict], Optional[dict], bool]:
    """Fetch one account with time-slice pagination. Returns (posts, failure, truncated)."""
    all_posts: list[dict] = []
    older_than: Optional[datetime] = None
    truncated = False
    last_error = ""

    for page in range(max(1, max_pages)):
        response = None
        attempt_error = ""
        for attempt in range(max(1, retries)):
            try:
                response = call_xai_x_search(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    account=account,
                    window_start=window_start,
                    window_end=window_end,
                    older_than=older_than,
                    timeout=timeout,
                    enable_image_understanding=enable_image_understanding,
                    api_mode=api_mode,
                )
                attempt_error = ""
                break
            except Exception as exc:
                attempt_error = str(exc)
                last_error = attempt_error
                if attempt + 1 < max(1, retries):
                    _progress(f"  retry {attempt + 1} for {account.clean_handle}: {attempt_error[:120]}")
                    time.sleep(retry_delay)
        if response is None:
            return [], {
                "handle": account.clean_handle,
                "provider": "xai",
                "error": last_error or "xai request failed",
            }, truncated

        page_posts = parse_xai_response_posts(response, account)
        page_posts = filter_window(page_posts, window_start, window_end)
        before = len(all_posts)
        all_posts = dedupe_posts(all_posts + page_posts)
        new_count = len(all_posts) - before
        _progress(
            f"  page {page + 1}/{max_pages}: parsed={len(page_posts)} new={new_count} total={len(all_posts)}"
        )

        if len(all_posts) >= per_account_limit:
            all_posts = sorted(all_posts, key=lambda x: x.get("created_at") or "", reverse=True)[
                :per_account_limit
            ]
            truncated = True
            break

        # Continue if this page looks full and we still have room before window_start
        oldest = _oldest_created(page_posts)
        page_looks_full = len(page_posts) >= page_full_threshold or new_count >= page_full_threshold
        if not page_looks_full or oldest is None:
            break
        if oldest <= window_start + timedelta(minutes=1):
            break
        # Avoid infinite loop on same cursor
        if older_than is not None and oldest >= older_than:
            break
        older_than = oldest
    else:
        # exhausted max_pages without natural stop
        truncated = True

    all_posts = dedupe_posts(filter_window(all_posts, window_start, window_end))
    all_posts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    if len(all_posts) > per_account_limit:
        all_posts = all_posts[:per_account_limit]
        truncated = True
    return all_posts, None, truncated


def fetch_watchlist(
    accounts: list[Account],
    *,
    api_key: str,
    base_url: str = "https://api.x.ai/v1",
    model: str = "grok-4-1-fast-non-reasoning",
    api_mode: str = "auto",
    window_start: datetime,
    window_end: datetime,
    per_account_limit: int = 50,
    max_pages_per_account: int = 5,
    timeout: int = 120,
    account_delay: float = 1.0,
    max_consecutive_failures: int = 8,
    cooldown_seconds: float = 30.0,
    retries: int = 2,
    retry_delay_seconds: float = 5.0,
    enable_image_understanding: bool = True,
    page_full_threshold: int = 8,
) -> dict[str, Any]:
    posts: list[dict] = []
    failures: list[dict] = []
    truncated_accounts: list[str] = []
    consecutive = 0
    ok_accounts = 0
    _, resolved_mode = normalize_xai_endpoint(base_url, api_mode)

    for idx, account in enumerate(accounts):
        _progress(
            f"Fetching [{idx + 1}/{len(accounts)}] {account.clean_handle} via xAI ({resolved_mode})..."
        )
        account_posts, failure, truncated = fetch_account_posts_xai(
            account,
            api_key=api_key,
            base_url=base_url,
            model=model,
            window_start=window_start,
            window_end=window_end,
            per_account_limit=per_account_limit,
            max_pages=max_pages_per_account,
            page_full_threshold=page_full_threshold,
            timeout=timeout,
            enable_image_understanding=enable_image_understanding,
            retries=retries,
            retry_delay=retry_delay_seconds,
            api_mode=api_mode,
        )
        if failure and not account_posts:
            failures.append(failure)
            consecutive += 1
            _progress(f"  {account.clean_handle}: failed — {failure.get('error', '')[:160]}")
            if max_consecutive_failures > 0 and consecutive >= max_consecutive_failures:
                _progress(
                    f"Circuit breaker: {consecutive} consecutive failures — "
                    f"cooldown {cooldown_seconds}s then continue..."
                )
                time.sleep(max(0.0, cooldown_seconds))
                consecutive = 0
        else:
            posts.extend(account_posts)
            ok_accounts += 1
            consecutive = 0
            if truncated:
                truncated_accounts.append(account.clean_handle)
            _progress(f"  {account.clean_handle}: {len(account_posts)} posts" + (" (truncated)" if truncated else ""))

        if account_delay > 0 and idx < len(accounts) - 1:
            time.sleep(account_delay)

    window_posts = dedupe_posts(filter_window(posts, window_start, window_end))
    window_posts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    hours = max(0.0, (window_end - window_start).total_seconds() / 3600.0)
    return {
        "ok": True,
        "provider": "xai",
        "providers": ["xai"],
        "provider_hits": {"xai": ok_accounts},
        "hours": round(hours, 2),
        "window": "yesterday_start_to_now",
        "timezone": "Europe/Istanbul",
        "since": window_start.isoformat(),
        "until": window_end.isoformat(),
        "posts": window_posts,
        "raw_count": len(posts),
        "window_count": len(window_posts),
        "failures": failures,
        "truncated_accounts": truncated_accounts,
        "truncated": bool(truncated_accounts),
        "accounts": [a.clean_handle for a in accounts],
        "ok_accounts": ok_accounts,
        "fail_accounts": len(failures),
    }


def fetch_and_cache(
    accounts_path: Path,
    cache_dir: Path,
    target_date_iso: str,
    fetch_cfg: dict,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    tz_name = str(fetch_cfg.get("timezone") or "Europe/Istanbul")
    window_start, window_end = resolve_fetch_window(tz_name=tz_name)
    cache_file = cache_dir / f"posts_{target_date_iso}.json"

    primary_provider = str(fetch_cfg.get("provider") or "xai").lower()
    fallback_provider = str(fetch_cfg.get("fallback_provider") or "").lower()
    if cache_file.exists() and not force_refresh:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if (
            cached.get("posts") is not None
            and cached.get("window") == "yesterday_start_to_now"
            and cached.get("since")
            and cached.get("provider") == primary_provider
        ):
            return cached

    tiers = fetch_cfg.get("tiers") or None
    if isinstance(tiers, str):
        tiers = [tiers]
    accounts = load_accounts_yaml(
        accounts_path,
        min_priority=str(fetch_cfg.get("min_priority", "low")),
        tiers=tiers,
    )
    if not accounts:
        result = {"ok": False, "reason": "no_accounts", "posts": [], "failures": []}
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    api_key_env = str(fetch_cfg.get("api_key_env") or "XAI_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    providers = [primary_provider]
    if fallback_provider and fallback_provider != primary_provider:
        providers.append(fallback_provider)

    if primary_provider == "xai" and not api_key:
        raise RuntimeError(
            f"Missing {api_key_env}. Set it in the environment or skill .env "
            "(get a key at https://console.x.ai)."
        )

    provider_order: list[str] = []
    provider_hits: dict[str, int] = {}
    merged_posts: list[dict] = []
    pending_failures: dict[str, dict] = {}
    truncated_accounts: list[str] = []
    remaining = list(accounts)

    for prov in providers:
        if not remaining:
            break
        if prov == "fxtwitter":
            provider_order.append(prov)
            result = fetch_watchlist_fxtwitter(
                remaining,
                base_url=str(fetch_cfg.get("fxtwitter_base_url") or FXTWITTER_DEFAULT_BASE),
                window_start=window_start,
                window_end=window_end,
                per_account_limit=int(fetch_cfg.get("per_account_limit", 50)),
                max_pages_per_account=int(fetch_cfg.get("max_pages_per_account", 5)),
                timeout=int(fetch_cfg.get("timeout_seconds", 60)),
                account_delay=float(fetch_cfg.get("account_delay_seconds", 1.0)),
                retries=int(fetch_cfg.get("retries", 2)),
                retry_delay_seconds=float(fetch_cfg.get("retry_delay_seconds", 5.0)),
                count=int(fetch_cfg.get("fxtwitter_count", 100)),
                with_replies=bool(fetch_cfg.get("fxtwitter_with_replies", True)),
                groupthreads=bool(fetch_cfg.get("fxtwitter_groupthreads", True)),
            )
        elif prov == "xai":
            if not api_key:
                if prov == primary_provider:
                    raise RuntimeError(
                        f"Missing {api_key_env}. Set it in the environment or skill .env "
                        "(get a key at https://console.x.ai)."
                    )
                _progress(f"Fallback xai skipped: {api_key_env} not set.")
                continue
            provider_order.append(prov)
            result = fetch_watchlist(
                remaining,
                api_key=api_key,
                base_url=str(fetch_cfg.get("xai_base_url") or "https://api.x.ai/v1"),
                model=str(fetch_cfg.get("xai_model") or "grok-4-1-fast-non-reasoning"),
                api_mode=str(fetch_cfg.get("xai_api_mode") or "auto"),
                window_start=window_start,
                window_end=window_end,
                per_account_limit=int(fetch_cfg.get("per_account_limit", 50)),
                max_pages_per_account=int(fetch_cfg.get("max_pages_per_account", 5)),
                timeout=int(fetch_cfg.get("timeout_seconds", 120)),
                account_delay=float(fetch_cfg.get("account_delay_seconds", 1.0)),
                max_consecutive_failures=int(fetch_cfg.get("max_consecutive_failures", 8)),
                cooldown_seconds=float(fetch_cfg.get("cooldown_seconds", 30.0)),
                retries=int(fetch_cfg.get("retries_per_provider", fetch_cfg.get("retries", 2))),
                retry_delay_seconds=float(fetch_cfg.get("retry_delay_seconds", 5.0)),
                enable_image_understanding=bool(fetch_cfg.get("enable_image_understanding", True)),
                page_full_threshold=int(fetch_cfg.get("page_full_threshold", 8)),
            )
        else:
            continue

        provider_hits[prov] = result.get("ok_accounts", 0)
        before = len(merged_posts)
        merged_posts = dedupe_posts(merged_posts + (result.get("posts") or []))
        truncated_accounts.extend(result.get("truncated_accounts") or [])
        ok_handles = {str(p.get("account") or "").lower() for p in (result.get("posts") or [])}
        for h in ok_handles:
            pending_failures.pop(h, None)
        for failure in result.get("failures") or []:
            pending_failures[str(failure.get("handle") or "").lower()] = failure
        if prov == providers[-1]:
            remaining = []
        else:
            remaining = [
                a for a in remaining if a.clean_handle.lower() in pending_failures
            ]
        _progress(
            f"[{prov}] ok={result.get('ok_accounts', 0)} failed={result.get('fail_accounts', 0)} "
            f"posts=+{len(merged_posts) - before}"
        )

    window_posts = dedupe_posts(filter_window(merged_posts, window_start, window_end))
    window_posts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    hours = max(0.0, (window_end - window_start).total_seconds() / 3600.0)
    merged_failures = list(pending_failures.values())
    result = {
        "ok": True,
        "provider": primary_provider,
        "providers": provider_order,
        "provider_hits": provider_hits,
        "hours": round(hours, 2),
        "window": "yesterday_start_to_now",
        "since": window_start.isoformat(),
        "until": window_end.isoformat(),
        "posts": window_posts,
        "raw_count": len(merged_posts),
        "window_count": len(window_posts),
        "failures": merged_failures,
        "truncated_accounts": truncated_accounts,
        "truncated": bool(truncated_accounts),
        "accounts": [a.clean_handle for a in accounts],
        "ok_accounts": len({p.get("account", "").lower() for p in window_posts}),
        "fail_accounts": len(merged_failures),
    }
    result["target_date"] = target_date_iso
    result["accounts_path"] = str(accounts_path)
    result["lookback_rule"] = "yesterday_start_to_now"
    result["timezone"] = tz_name
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# Back-compat alias used by older callers / docs
def resolve_lookback_hours(target_date_iso: str, fetch_cfg: dict) -> float:
    start, end = resolve_fetch_window(tz_name=str(fetch_cfg.get("timezone") or "Europe/Istanbul"))
    return max(0.0, (end - start).total_seconds() / 3600.0)
