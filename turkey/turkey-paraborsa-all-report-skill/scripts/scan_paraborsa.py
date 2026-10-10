#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover all Paraborsa commentary posts for a target date."""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from typing import Iterable

import requests

BASE = "https://www.paraborsa.net"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
# Paraborsa serves real content directly; the local HTTP proxy (if any) trips
# Cloudflare's managed challenge and returns an interstitial page. Bypass it.
NO_PROXY = {"http": None, "https": None}

# Known broker slugs for REST slug probing (supplements WP search).
BROKER_SLUGS = [
    "a1-capital", "ahlatci-yatirim", "alnus-yatirim", "anadolu-yatirim", "ata-yatirim",
    "bizim-menkul", "bulls-yatirim", "deniz-yatirim", "destek-yatirim", "gedik-yatirim",
    "garanti-bbva-yatirim", "global-menkul", "halk-yatirim", "hsbc-yatirim", "icbc-yatirim",
    "info-yatirim", "integral-yatirim", "isyatirim", "marbas-menkul", "meksa-yatirim",
    "ncm-investment", "nurol-yatirim", "osmanli-yatirim", "oyak-yatirim", "phillip-capital",
    "qnb-yatirim", "seker-yatirim", "sentiment-algo", "tacirler-yatirim", "turkish-bank",
    "unlu-yatirim", "vakif-yatirim", "yatirim-finansman", "ziraat-yatirim", "colendi-yatirim",
    "acar-yatirim", "allbatross-yatirim", "btcturk-yatirim",
]

DEFAULT_PREFIXES = ["borsa-yorumu", "gun-ici-borsa-yorumu", "viop-yorumu", "haftalik-borsa-yorumu"]


def date_token(d: date, *, pad_day: bool = False) -> str:
    # Paraborsa currently uses UNPADDED day in slugs (e.g. "3-09-2026"),
    # but earlier titles sometimes had padded day. Expose both variants.
    day_fmt = f"{d.day:02d}" if pad_day else f"{d.day}"
    return f"{day_fmt}-{d.month:02d}-{d.year}"


def date_token_variants(d: date) -> list[str]:
    variants = {date_token(d, pad_day=False), date_token(d, pad_day=True)}
    return list(variants)


def dot_date(d: date, *, pad_day: bool = False) -> str:
    day_fmt = f"{d.day:02d}" if pad_day else f"{d.day}"
    return f"{day_fmt}.{d.month:02d}.{d.year}"


def dot_date_variants(d: date) -> list[str]:
    variants = {dot_date(d, pad_day=False), dot_date(d, pad_day=True)}
    return list(variants)


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").replace("&#8211;", "–").strip()


def matches_target(title: str, slug: str, target: date, *, include_period: bool = True) -> bool:
    dots = dot_date_variants(target)
    tokens = date_token_variants(target)
    text = f"{title} {slug}"
    if any(dot in text for dot in dots) or any(token in slug for token in tokens):
        return True
    if include_period:
        for dot in dots:
            if re.search(
                rf"{re.escape(dot)}\s*[-–/]\s*\d{{1,2}}\.{target.month:02d}\.{target.year}",
                title,
            ):
                return True
    return False


def is_commentary(title: str, slug: str, allowed_prefixes: Iterable[str]) -> bool:
    slug_l = slug.lower()
    if any(slug_l.startswith(p + "-") for p in allowed_prefixes):
        return True
    t = (title + " " + slug).lower()
    return any(k in t for k in ("borsa yorumu", "viop yorumu", "gun ici", "haftalik"))


def article_type_from_slug(slug: str) -> str:
    for prefix in DEFAULT_PREFIXES:
        if slug.startswith(prefix + "-"):
            return prefix
    return "other"


def broker_from_slug(slug: str) -> str:
    token = date_token(date.today())  # placeholder replaced below
    return slug


def parse_broker_slug(slug: str, target: date) -> str:
    for token in date_token_variants(target):
        for prefix in DEFAULT_PREFIXES:
            needle = f"{prefix}-"
            if slug.startswith(needle) and slug.endswith(f"-{token}"):
                return slug[len(needle): -len(token) - 1]
    return slug


def wp_search(target: date, *, delay: float = 0.8) -> dict[str, dict]:
    found: dict[str, dict] = {}
    queries = set()
    queries.update(dot_date_variants(target))
    queries.update(date_token_variants(target))
    queries.add("borsa yorumu")
    for dot in dot_date_variants(target):
        queries.add(f"borsa-yorumu {dot}")
    for q in queries:
        try:
            resp = requests.get(
                f"{BASE}/wp-json/wp/v2/posts",
                params={"search": q, "per_page": 100},
                headers=HEADERS,
                proxies=NO_PROXY,
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            try:
                posts_data = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                preview = re.sub(r"\s+", " ", resp.text or "")[:200]
                print(
                    f"Warning: wp_search bad JSON for {q!r}: {exc}; body[:200]={preview!r}",
                    file=sys.stderr,
                )
                continue
            for post in posts_data:
                slug = post.get("slug", "")
                if not slug or slug in found:
                    continue
                title = strip_html(post.get("title", {}).get("rendered", ""))
                found[slug] = {
                    "slug": slug,
                    "title": title,
                    "url": post.get("link", ""),
                    "article_type": article_type_from_slug(slug),
                    "broker_slug": parse_broker_slug(slug, target),
                    "discovered_via": f"search:{q}",
                }
            time.sleep(delay)
        except requests.RequestException:
            continue
    return found


def slug_probe(
    target: date,
    *,
    prefixes: Iterable[str] = DEFAULT_PREFIXES,
    brokers: Iterable[str] = BROKER_SLUGS,
    delay: float = 0.35,
) -> dict[str, dict]:
    found: dict[str, dict] = {}
    tokens = date_token_variants(target)
    for prefix in prefixes:
        for broker in brokers:
            for token in tokens:
                slug = f"{prefix}-{broker}-{token}"
                try:
                    resp = requests.get(
                        f"{BASE}/wp-json/wp/v2/posts",
                        params={"slug": slug},
                        headers=HEADERS,
                        proxies=NO_PROXY,
                        timeout=20,
                    )
                    if resp.status_code != 200:
                        continue
                    try:
                        posts_data = resp.json()
                    except (ValueError, json.JSONDecodeError) as exc:
                        preview = re.sub(r"\s+", " ", resp.text or "")[:200]
                        print(
                            f"Warning: slug_probe bad JSON for {slug}: {exc}; "
                            f"body[:200]={preview!r}",
                            file=sys.stderr,
                        )
                        continue
                    if not posts_data:
                        continue
                    post = posts_data[0]
                    title = strip_html(post.get("title", {}).get("rendered", ""))
                    found[slug] = {
                        "slug": slug,
                        "title": title,
                        "url": post.get("link", ""),
                        "article_type": prefix,
                        "broker_slug": broker,
                        "discovered_via": "slug_probe",
                    }
                    time.sleep(delay)
                except requests.RequestException:
                    continue
    return found


def scan_all(
    target: date,
    *,
    allowed_prefixes: Iterable[str] | None = None,
    include_period_ranges: bool = True,
    search_delay: float = 0.8,
    probe_delay: float = 0.35,
    slug_probe_min_commentary: int = 10,
) -> list[dict]:
    prefixes = list(allowed_prefixes or DEFAULT_PREFIXES)

    # wp_search usually covers the day's commentaries; only burn time on
    # slug_probe if the broad search came up short.
    merged = wp_search(target, delay=search_delay)
    articles = [
        it for it in merged.values()
        if is_commentary(it["title"], it["slug"], prefixes)
        and matches_target(it["title"], it["slug"], target, include_period=include_period_ranges)
    ]

    if len(articles) < slug_probe_min_commentary:
        probed = slug_probe(target, prefixes=prefixes, delay=probe_delay)
        merged = {**merged, **probed}
        articles = [
            it for it in merged.values()
            if is_commentary(it["title"], it["slug"], prefixes)
            and matches_target(it["title"], it["slug"], target, include_period=include_period_ranges)
        ]

    articles.sort(key=lambda x: (x["article_type"], x["broker_slug"], x["slug"]))
    return articles
