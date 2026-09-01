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

MIN_CHARS = 800
MAX_CHARS = 2000
MIN_ITEMS = 6
MAX_ITEMS = 14


def validate(text: str) -> dict:
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
    if length < MIN_CHARS:
        errors.append(f"report too short: {length} < {MIN_CHARS} chars (too short).")
    if length > MAX_CHARS * 2:
        errors.append(f"report too long: {length} > {MAX_CHARS * 2} chars (too long).")
    elif length > MAX_CHARS:
        warnings.append(f"report longer than target {MAX_CHARS} chars ({length}).")

    # --- unified item stream checks ---
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").splitlines()]
    star_lines = [i for i, ln in enumerate(lines) if re.match(r"^【★+】", ln.strip())]
    n_items = len(star_lines)
    if n_items < MIN_ITEMS:
        errors.append(
            f"only {n_items} starred items (< {MIN_ITEMS}); item stream too thin — "
            "structured slots violated."
        )
    if n_items > MAX_ITEMS:
        errors.append(
            f"{n_items} starred items (> {MAX_ITEMS}); structured slots exceeded."
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
