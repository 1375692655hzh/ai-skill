#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate morning-briefing output format and forbid source attribution."""
from __future__ import annotations

import re

from sanitize_sources import validate_no_attribution


FACT_SECTIONS = (
    "关键个股",
    "行业板块表现",
    "汇市与大宗商品",
)

STRUCTURED_SECTIONS = {
    "今日操作参考": ("仓位：", "点位：", "回避："),
}

FACT_ANALYSIS_PATTERNS = [
    r"由于",
    r"因为",
    r"导致",
    r"呈现出[^。]*?格局",
    r"显示出",
    r"反映(了|出)",
    r"受到?.*?(压制|推动|拖累|提振)",
    r"市场认为",
    r"暗示",
    r"预计",
    r"展望",
    r"结构性轮动",
    r"系统性撤退",
    r"支撑位",
    r"阻力位",
    r"均线",
    r"RSI",
    r"MACD",
    r"情绪面",
    r"资金面",
    r"技术面",
    r"权重分化",
    r"整体承压",
    r"呈现出",
]

# Generic fluff phrases that indicate the model did NOT use concrete BHT data.
FACT_FLUFF_PATTERNS = [
    r"权重蓝筹",
    r"中小盘题材",
    r"权重股",
    r"周期与成长板块",
    r"防御属性",
    r"成交集中于",
    r"位居成交额前列",
    r"前期累计涨幅较大",
    r"高低(切换|轮动)",
    r"主要标的",
    r"等(权重|板块|个股)",
    r"及相关",
]

# Required concrete tickers in 关键个股 — at least N uppercase codes (length>=4)
MIN_TICKERS_IN_STOCKS = 3


def _extract_section(text: str, title: str) -> str:
    pat = rf"【{re.escape(title)}】\s*(.*?)(?=\n【|\n风险提示|$)"
    m = re.search(pat, text, re.S)
    return (m.group(1) if m else "").strip()


def _slot_on_own_line(body: str, slot: str) -> bool:
    for line in body.splitlines():
        if line.strip().startswith(slot):
            return True
    return False


def _slots_are_consecutive_lines(body: str, slots: tuple[str, ...]) -> bool:
    lines = [ln.strip() for ln in body.splitlines()]
    idxs: list[int] = []
    for i, line in enumerate(lines):
        if any(line.startswith(slot) for slot in slots):
            idxs.append(i)
    if len(idxs) < 2:
        return True
    for a, b in zip(idxs, idxs[1:]):
        if b != a + 1:
            return False
    return True


def _count_sentences(body: str) -> int:
    return len([p for p in re.split(r"[。！？]", body) if p.strip()])


def validate(text: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(text) < 300:
        errors.append("Output too short (< 300 chars).")
    if len(text) > 5000:
        warnings.append("Output longer than expected (> 5000 chars).")

    forbidden = [
        ("===", "separator ==="),
        ("---", "separator ---"),
        ("━━━", "line separator"),
        ("**", "markdown bold"),
        ("__", "markdown italic"),
        ("🔴", "emoji"),
        ("🟢", "emoji"),
        ("⚠️", "emoji"),
        ("❌", "emoji"),
        ("✅", "emoji"),
    ]
    for marker, name in forbidden:
        if marker in text:
            errors.append(f"Found forbidden {name}: {marker}")

    if re.search(r"(?m)^\s*[-•*]\s", text):
        errors.append("Found list bullet at line start.")
    if re.search(r"(?m)^\s*\d+[.)]\s", text):
        errors.append("Found numbered list.")

    if re.search(r"\b18:30\b", text) or "18：30" in text:
        errors.append("Found forbidden clock stamp 18:30.")
    if re.search(r"(?<!\d{4}-\d{2}-\d{2}T)\b\d{1,2}:\d{2}\b", text):
        errors.append("Found clock time HH:MM; remove time stamps from FX/commodities and body.")

    required_sections = [
        "核心观点",
        "国际新闻",
        "关键个股",
        "行业板块表现",
        "汇市与大宗商品",
        "今日操作参考",
    ]
    for section in required_sections:
        if section not in text:
            errors.append(f"Missing section: {section}")

    if "风险提示" not in text and "不构成投资建议" not in text:
        warnings.append("Risk warning or disclaimer missing.")

    # Fact sections: HARD-FAIL on unconverted TL, analysis tone, fluff.
    for title in FACT_SECTIONS:
        body = _extract_section(text, title)
        if not body:
            continue
        # 1) Unconverted large TL amount → must use 亿里拉
        if re.search(r"\d{1,3}(?:[.,]\d{3}){1,}\s*(?:TL|里拉)", body):
            errors.append(
                f"[{title}] unconverted large TL amount found; must use 「亿里拉」."
            )
        # 2) Analytical / causal language banned in fact sections
        for ap in FACT_ANALYSIS_PATTERNS:
            m = re.search(ap, body)
            if m:
                errors.append(
                    f"[{title}] analytical language「{m.group(0)}」banned in BHT-only fact section."
                )
                break
        # 3) Generic fluff = no real data
        for fp in FACT_FLUFF_PATTERNS:
            m = re.search(fp, body)
            if m:
                errors.append(
                    f"[{title}] generic phrase「{m.group(0)}」— use concrete BHT names/tickers."
                )
                break

    # 关键个股 must contain concrete tickers, not just prose
    stocks = _extract_section(text, "关键个股")
    if stocks:
        tickers = re.findall(r"\b[A-Z]{3,}[A-Z0-9.]*\b", stocks)
        stop = {"BIST", "TL", "TRY", "USD", "EUR", "BRENT", "WTI", "NBA", "GDP", "CPI", "PCE"}
        tickers = [t for t in tickers if t not in stop]
        if len(set(tickers)) < MIN_TICKERS_IN_STOCKS:
            errors.append(
                f"[关键个股] fewer than {MIN_TICKERS_IN_STOCKS} concrete tickers "
                f"(found {sorted(set(tickers))}); list real stock codes from BHT."
            )
        if "成交" in stocks and ("涨幅" in stocks or "跌幅" in stocks):
            if re.search(r"成交[^\n]*涨幅|成交[^\n]*跌幅", stocks):
                warnings.append("[关键个股] 成交额与涨跌幅应换行分行，不要挤同一行.")

    # 行业板块表现 must be fully translated — no raw Turkish words should leak.
    sectors_body = _extract_section(text, "行业板块表现")
    if sectors_body:
        # Match lowercase ascii runs of length >=3. Chinese chars are NOT
        # ascii so they naturally bound the match — but we must NOT use \w
        # for the lookaround because Python's re.UNICODE makes Chinese count
        # as \w, which would suppress matches at the CN/TR boundary.
        tr_hits = re.findall(r"[a-zçğıöşü]{3,}", sectors_body)
        stop_en = {"the", "and", "for", "with"}
        tr_hits = [t for t in tr_hits if t not in stop_en]
        if tr_hits:
            errors.append(
                f"[行业板块表现] untranslated Turkish words leaked: {sorted(set(tr_hits))}. "
                "Translate every sector name to Chinese."
            )

    intl = _extract_section(text, "国际新闻")
    if intl:
        intl_lines = [ln.strip() for ln in intl.splitlines() if ln.strip()]
        # No count cap or floor: write as many or as few key items as the
        # source justifies. Only enforce the per-line-one-item format.
        # Reject one giant paragraph (no line breaks between items).
        if len(intl_lines) == 1 and len(intl_lines[0]) > 120:
            errors.append("[国际新闻] must use line breaks: one news item per line.")

    for title, slots in STRUCTURED_SECTIONS.items():
        body = _extract_section(text, title)
        if not body:
            continue
        missing = [s for s in slots if s not in body]
        if missing:
            errors.append(f"[{title}] missing structured slots: {', '.join(missing)}")
        not_lined = [s for s in slots if s in body and not _slot_on_own_line(body, s)]
        if not_lined:
            errors.append(f"[{title}] slots must each start a new line: {', '.join(not_lined)}")
        if not missing and not not_lined and not _slots_are_consecutive_lines(body, slots):
            errors.append(f"[{title}] slots must be consecutive lines (换行分行，不要空行).")
        n_sent = _count_sentences(body)
        if n_sent > 6:
            errors.append(f"[{title}] too many sentences ({n_sent} > 6); keep one sentence per slot.")

    attribution = validate_no_attribution(text)
    errors.extend(attribution["errors"])

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "length": len(text),
        "attribution_hits": attribution.get("hits", []),
    }
