#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the structured flash-news brief (push version)."""
from __future__ import annotations

import re

from sanitize_sources import validate_no_attribution

REQUIRED_FIELDS = (
    "【头条】",
    "【公告】",
    "【数据】",
    "【关注】",
    "【风险】",
)

_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

RETRYABLE_PATTERNS = (
    "too short",
    "too long",
    "Missing field",
    "Missing brief title",
    "Found list bullet",
    "separator",
    "markdown bold",
    "emoji",
    "must be a header line",
)


def count_cn_chars(text: str) -> int:
    return len(_CN_CHAR_RE.findall(text or ""))


def is_retryable_error(err: str) -> bool:
    return any(p in err for p in RETRYABLE_PATTERNS)


def has_retryable_errors(errors: list[str]) -> bool:
    return any(is_retryable_error(e) for e in errors)


def validate_brief(text: str, *, min_chars: int = 200, max_chars: int = 520) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    length = count_cn_chars(text)

    if length < min_chars:
        warnings.append(f"Brief too short (< {min_chars} Chinese chars).")
    if length > max_chars:
        warnings.append(f"Brief too long (> {max_chars} Chinese chars).")

    if "快讯简报" not in text:
        errors.append("Missing brief title marker (快讯简报).")

    for marker, name in [
        ("===", "separator ==="),
        ("---", "separator ---"),
        ("**", "markdown bold"),
        ("##", "markdown header"),
    ]:
        if marker in text:
            errors.append(f"Found forbidden {name}: {marker}")

    if re.search(r"(?m)^\s*[-•*·]\s", text):
        errors.append("Found list bullet at line start.")

    for field in REQUIRED_FIELDS:
        if field not in text:
            errors.append(f"Missing field: {field}")

    attribution = validate_no_attribution(text)
    errors.extend(attribution["errors"])

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "length": length,
        "attribution_hits": attribution.get("hits", []),
    }
