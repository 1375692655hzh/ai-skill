#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate close-of-day report format and forbid source attribution."""
from __future__ import annotations

import re

from sanitize_sources import validate_no_attribution


FACT_SECTIONS = (
    "大盘概况",
    "关键个股异动",
    "行业板块表现",
    "汇市与大宗商品",
)

# Structured concise slots (not char-count caps); each slot must start its own line
STRUCTURED_SECTIONS = {
    "核心信号与逻辑": ("驱动：", "技术：", "资金情绪："),
    "后市策略参考": ("仓位：", "点位：", "回避："),
}

# Hard ban: analysis / causal language inside the four BHT-only fact sections
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

# Required concrete tickers in 关键个股异动 — at least N uppercase codes (length>=4)
MIN_TICKERS_IN_STOCKS = 3


def _extract_section(text: str, title: str) -> str:
    """Return body of 【title】 until next 【...】 or 风险提示."""
    pat = rf"【{re.escape(title)}】\s*(.*?)(?=\n【|\n风险提示|$)"
    m = re.search(pat, text, re.S)
    return (m.group(1) if m else "").strip()


def _count_sentences(body: str) -> int:
    parts = [p.strip() for p in re.split(r"[。！？]", body) if p.strip()]
    return len(parts)


def _slot_on_own_line(body: str, slot: str) -> bool:
    """True if some non-empty line starts with the slot label."""
    for line in body.splitlines():
        if line.strip().startswith(slot):
            return True
    return False


def _slots_are_consecutive_lines(body: str, slots: tuple[str, ...]) -> bool:
    """Slots should be on consecutive lines (换行), not separated by blank lines."""
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


def _num_core(fp: str) -> str | None:
    """Normalize a numeric token for float-equivalence comparison.

    Strips '%' and leading '-', returns a canonical decimal string so that
    '13458.10' and '13458.1' compare equal. Returns None if not a plain number.
    """
    bare = fp.rstrip("%")
    if bare.startswith("-"):
        bare = bare[1:]
    if not re.fullmatch(r"\d+(?:\.\d+)?", bare):
        return None
    try:
        return f"{float(bare):.10g}"
    except ValueError:
        return None


def _fp_in_source(fp: str, src_fps: set[str]) -> bool:
    """True if fingerprint token appears in source with %/sign/trailing-zero tolerance."""
    if fp in src_fps:
        return True
    variants = {fp}
    bare = fp.rstrip("%")
    if bare != fp:
        variants.add(bare)
    if fp.startswith("-"):
        variants.add(fp[1:])
        variants.add(fp[1:].rstrip("%"))
    else:
        variants.add("-" + fp)
        variants.add("-" + bare)
    if any(v in src_fps for v in variants):
        return True
    # Float-equivalence: 13458.10 ↔ 13458.1
    core = _num_core(fp)
    if core is None:
        return False
    for s in src_fps:
        sc = _num_core(s)
        if sc is not None and sc == core:
            return True
    return False


def _extract_numbers(s: str) -> set[str]:
    """Fingerprint all numeric tokens in source text for provenance check.

    Captures percentages (X.XX%), index closes / FX / commodity prices with
    optional decimal, 亿里拉 amounts. Uses non-overlapping leftmost matches so
    a price like 47.40 is recorded as one token, not split into 47 and 40.

    IMPORTANT: do NOT use \\b as the boundary — in Python 3's default re.UNICODE
    mode, Chinese characters count as word chars (\\w matches 报), so \\b does
    NOT fire between a Chinese prefix and a digit. That caused '报47.40' to be
    fingerprinted as just '40' (the \\b only matched between '.' and '4'),
    producing false-positive "number not in source" errors for correct LLM
    output. Use lookarounds anchored on digits/dots instead, which are
    Unicode-agnostic.
    """
    fps: set[str] = set()
    # Percentages first (greedy, with optional leading -): -2.48% / 1.26%
    # Record BOTH the full "-2.48%" and the bare "-2.48" so an output that
    # writes the number without the trailing % still matches the source.
    for m in re.finditer(r"-?[\d.,]+%", s):
        token = m.group(0).replace(",", ".").replace(" ", "")
        fps.add(token)
        fps.add(token.rstrip("%"))
        s = s[: m.start()] + " " * (m.end() - m.start()) + s[m.end() :]
    # 亿里拉 amounts: 179.41亿里拉
    # Record BOTH the full "179.41亿里拉" and the bare "179.41" so an output
    # that drops the unit (or abbreviates it, e.g. "亿拉") still matches.
    for m in re.finditer(r"[\d.]+亿里拉", s):
        full = m.group(0)
        fps.add(full)
        fps.add(full.replace("亿里拉", ""))
        s = s[: m.start()] + " " * (m.end() - m.start()) + s[m.end() :]
    # Prices / closes with optional decimal: 13515.54 / 47.40 / 4016 / 63996
    # Consume the whole numeric run so "47.40" is one token, not "47" + "40".
    # Allow 1-digit integer part so percentage-like cells stored as "0,57" or
    # "9,40" without a % sign (common in tabular sources) are not skipped.
    # Lookarounds (not \b) so Chinese prefixes don't split the number.
    for m in re.finditer(r"(?<![\d.])\d{1,5}(?:[.,]\d{1,4})?(?![\d.])", s):
        fps.add(m.group(0).replace(",", "."))
    return fps


def validate(text: str, *, source_facts: str | None = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(text) < 300:
        errors.append("Output too short (< 300 chars).")
    if len(text) > 6000:
        warnings.append("Output longer than expected (> 6000 chars).")

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

    # Hard ban: clock stamps (esp. BHT 18:30) anywhere in finished report
    if re.search(r"\b18:30\b", text) or "18：30" in text:
        errors.append("Found forbidden clock stamp 18:30 (FX/commodities must not include time).")
    if re.search(r"\d{1,2}:\d{2}", text):
        # Allow ISO dates like 2026-07-27 only; ban HH:MM
        if re.search(r"(?<!\d{4}-\d{2}-\d{2}T)\b\d{1,2}:\d{2}\b", text):
            errors.append("Found clock time HH:MM; remove time stamps from FX/commodities and body.")

    required_sections = [
        "核心结论",
        "大盘概况",
        "关键个股异动",
        "行业板块表现",
        "汇市与大宗商品",
        "核心信号与逻辑",
        "后市策略参考",
    ]
    for section in required_sections:
        if section not in text:
            errors.append(f"Missing section: {section}")

    if "风险提示" not in text and "不构成投资建议" not in text:
        warnings.append("Risk warning or disclaimer missing.")

    # Fact sections: HARD-FAIL on unconverted TL, analysis tone, fluff, missing tickers.
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

    # 关键个股异动 must contain concrete tickers, not just prose
    stocks = _extract_section(text, "关键个股异动")
    if stocks:
        tickers = re.findall(r"\b[A-Z]{3,}[A-Z0-9.]*\b", stocks)
        # Filter out common non-ticker all-caps words
        stop = {"BIST", "TL", "TRY", "USD", "EUR", "BRENT", "WTI", "NBA", "GDP", "CPI", "PCE"}
        tickers = [t for t in tickers if t not in stop]
        if len(set(tickers)) < MIN_TICKERS_IN_STOCKS:
            errors.append(
                f"[关键个股异动] fewer than {MIN_TICKERS_IN_STOCKS} concrete tickers "
                f"(found {sorted(set(tickers))}); list real stock codes from BHT."
            )
        if "成交" in stocks and ("涨幅" in stocks or "跌幅" in stocks):
            # ok if blank line OR just next line; fail only if same line
            if re.search(r"成交[^\n]*涨幅|成交[^\n]*跌幅", stocks):
                warnings.append("[关键个股异动] 成交额与涨跌幅应换行分行，不要挤同一行.")

    # 行业板块表现 must be fully translated — no raw Turkish words should leak.
    sectors_body = _extract_section(text, "行业板块表现")
    if sectors_body:
        tr_hits = re.findall(r"[a-zçğıöşü]{3,}", sectors_body)
        stop_en = {"the", "and", "for", "with"}
        tr_hits = [t for t in tr_hits if t not in stop_en]
        if tr_hits:
            errors.append(
                f"[行业板块表现] untranslated Turkish words leaked: {sorted(set(tr_hits))}. "
                "Translate every sector name to Chinese."
            )

    # 大盘概况 must contain a concrete BIST 100 close (5-digit number like 13xxx or 14xxx)
    overview = _extract_section(text, "大盘概况")
    if overview and not re.search(r"1[3-5]\s?\d{3}([.,]\d{1,2})?", overview):
        errors.append(
            "[大盘概况] missing concrete BIST100 close figure (e.g. 13687.86); do not narrate."
        )

    # Opinion sections: require slot labels each on its own line
    for title, slots in STRUCTURED_SECTIONS.items():
        body = _extract_section(text, title)
        if not body:
            continue
        missing = [s for s in slots if s not in body]
        if missing:
            errors.append(f"[{title}] missing structured slots: {', '.join(missing)}")
        not_lined = [s for s in slots if s in body and not _slot_on_own_line(body, s)]
        if not_lined:
            errors.append(
                f"[{title}] slots must each start a new line: {', '.join(not_lined)}"
            )
        if not missing and not not_lined and not _slots_are_consecutive_lines(body, slots):
            errors.append(
                f"[{title}] slots must be consecutive lines (换行分行，不要空行)."
            )
        n_sent = _count_sentences(body)
        # 3 slots ≈ 3 sentences; allow slight overflow, reject long essays
        if n_sent > 6:
            errors.append(
                f"[{title}] too many sentences ({n_sent} > 6); keep one sentence per slot."
            )
        elif n_sent < 3:
            warnings.append(f"[{title}] fewer than 3 sentences; each slot should be one sentence.")

    # Data-provenance check: numbers in the four fact sections must appear in the
    # BHT source fingerprint. Catches LLM-fabricated percentages/prices/tickers.
    if source_facts:
        src_fps = _extract_numbers(source_facts)
        # Tolerate formatting differences: also store comma/dot-stripped forms.
        extra = set()
        for fp in list(src_fps):
            extra.add(fp.replace(",", ""))
            extra.add(fp.replace(".", ""))
        src_fps |= extra
        # Common non-data numbers to ignore: years, day counts, section ordinals
        ignore = {str(y) for y in range(2020, 2031)} | {"1", "2", "3", "100"}
        for title in FACT_SECTIONS:
            body = _extract_section(text, title)
            if not body:
                continue
            out_fps = _extract_numbers(body) - ignore
            # Membership with % / sign / trailing-zero transparency.
            # e.g. source "13458.1" matches output "13458.10"; source "-1.53%"
            # matches output "下跌1.53%" (bare 1.53).
            suspicious = []
            for fp in sorted(out_fps):
                if _fp_in_source(fp, src_fps):
                    continue
                suspicious.append(fp)
            if suspicious:
                errors.append(
                    f"[{title}] numbers not found in BHT source: {suspicious[:8]}. "
                    "Only reuse figures that appear in the BHT fact card; do not fabricate."
                )

    attribution = validate_no_attribution(text)
    errors.extend(attribution["errors"])

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "length": len(text),
        "attribution_hits": attribution.get("hits", []),
    }
