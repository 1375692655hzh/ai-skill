#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the flash-news digest: unified importance-ranked item stream."""
from __future__ import annotations

import re

from sanitize_sources import validate_no_attribution

REQUIRED_SECTIONS = (
    "【土耳其市场快讯",
    "【明日关注】",
    "风险提示：",
)

MIN_ITEMS = 8
MAX_ITEMS = 12
MIN_CHARS = 800
MAX_CHARS = 2000

# Errors matching any marker are eligible for "ship with warning" after the
# rewrite retry (attribution / star-structure / thin-length) — see
# generate_flash_news.run_digest. Structural breakage stays a hard fail.
SOFT_ERROR_MARKERS = (
    "来源归属",
    "starred items",
    "structured slots",
    "star-order",
    "too short",
)


def validate(
    text: str,
    *,
    min_items: int = MIN_ITEMS,
    max_items: int = MAX_ITEMS,
    min_chars: int = MIN_CHARS,
    max_chars: int = MAX_CHARS,
    calendar_dates: list[str] | None = None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"missing section: {section}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "attribution_hits": []}

    for marker, name in [
        ("===", "separator ==="),
        ("---", "separator ---"),
        ("**", "markdown bold"),
        ("##", "markdown header"),
    ]:
        if marker in text:
            errors.append(f"found forbidden {name}")

    if re.search(r"(?m)^\s*[-•*·]\s", text):
        errors.append("found list bullet at line start")
    if re.search(r"(?m)^\s*\d+[.、)]\s", text):
        errors.append("found numbered list at line start")
    emoji = [
        ch
        for ch in re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text)
        if ch not in "★☆"
    ]
    if emoji:
        errors.append(f"found emoji: {emoji[:5]}")

    length = len(re.sub(r"\s", "", text))
    if length < min_chars:
        errors.append(f"report too short: {length} < {min_chars} chars (too short).")
    if length > max_chars * 2:
        errors.append(f"report too long: {length} > {max_chars * 2} chars (too long).")
    elif length > max_chars:
        warnings.append(f"report longer than target {max_chars} chars ({length}).")

    # --- unified item stream checks ---
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").splitlines()]
    star_lines = [i for i, ln in enumerate(lines) if re.match(r"^【★+】", ln.strip())]
    n_items = len(star_lines)
    if n_items < min_items:
        errors.append(
            f"only {n_items} starred items (< {min_items}); item stream too thin — "
            "structured slots violated."
        )
    if n_items > max_items:
        errors.append(
            f"{n_items} starred items (> {max_items}); structured slots exceeded."
        )

    # star counts must be non-increasing down the stream (importance desc)
    star_counts = [len(re.match(r"^【(★+)", lines[i].strip()).group(1)) for i in star_lines]
    if any(b > a for a, b in zip(star_counts, star_counts[1:])):
        errors.append(
            "star-order violated: importance must be non-increasing down the stream "
            "(★★ cannot follow ★) — structured slots/star-order."
        )

    # each starred brief must be followed by a detail line (not another item/header)
    missing_detail = 0
    for idx in star_lines:
        nxt = idx + 1
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if nxt >= len(lines) or re.match(r"^【", lines[nxt].strip()):
            missing_detail += 1
    if missing_detail:
        warnings.append(
            f"{missing_detail}/{n_items} items lack a detail line under the star brief."
        )

    # routine-announcement merge line should be the last starred unit
    if n_items >= 2:
        last_star = lines[star_lines[-1]].strip()
        any_merge = [ln for ln in lines if ln.strip().startswith("另有")]
        if any_merge and not last_star.startswith("另有"):
            warnings.append(
                "「另有 N 家例行公告」merged line is not the last starred unit."
            )

    # 明日关注 should cite a calendar date when the calendar card had events
    if calendar_dates:
        focus = text.split("【明日关注】", 1)[-1] if "【明日关注】" in text else ""
        def _forms(d: str):
            try:
                from datetime import date as _d

                dt = _d.fromisoformat(d)
                return {d, f"{dt.month:02d}-{dt.day:02d}", f"{dt.month}月{dt.day}日"}
            except ValueError:
                return {d}
        if focus and not any(f in focus for d in calendar_dates for f in _forms(d)):
            warnings.append("【明日关注】cites no calendar-card date (check for invented dates).")

    attribution = validate_no_attribution(text)
    errors.extend(attribution["errors"])

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "length": length,
        "items": n_items,
        "attribution_hits": attribution.get("hits", []),
    }
