#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the daily-report markdown structure (pre-docx gate)."""
from __future__ import annotations

import re




LEVEL_WORDS = ("支撑", "阻力", "均线", "压力位", "支撑位", "阻力位")
DIRECTION_WORDS = ("突破", "跌破", "破位", "回踩", "企稳", "站稳", "守稳", "守住", "失守", "上看", "下看", "看涨", "看跌", "看多", "看空", "偏多", "偏空", "多头", "空头", "上行", "下行", "转弱", "转强", "走弱", "走强", "反弹", "回调", "上攻", "破坏", "承压", "催化", "止损", "止盈", "震荡", "改善", "恶化", "目标价", "买入", "卖出", "增持", "减持", "低估", "高估", "订单", "利润", "增长", "业绩", "并购", "要约", "分红", "回购")
INDEX_PREFIX_RE = re.compile(r"^(?:XU\s?\d+|BIST\s?\d*|XBANK)")
# A real LEVEL quote carries a number: "支撑 12.50". The bare word is often a
# VERB in real commentary ("对指数形成支撑", "保费数据支撑") — replay showed
# both false kills came from verb usage (K3 review 2026-10-12).
LEVEL_WITH_PRICE_RE = re.compile(r"(?:支撑|阻力|压力)[位]?\s*[：:]?\s*\d|均线[^\n]{0,12}\d")
# Fund-flow phrases whose 买入/卖出 words must not count as direction evidence
# ("外资净买入" kept a pure listing alive in replay).
FLOW_PHRASE_RE = re.compile(r"(?:净买入|净卖出|买入榜|卖出榜|净资金流入|净资金流出|资金净流入|资金净流出|买入居首|卖出居首)")


def block_is_no_view(blk: str) -> bool:
    """True if a stock block must be dropped: index row, placeholder fields,
    or levels-without-direction (user rule 2026-10-12).

    Direction evidence is judged on the OPINION LINES ONLY (first line is the
    required 名称｜定位｜决策 format — its 决策 wording is format, not thesis),
    with fund-flow phrases masked out first."""
    lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
    if not lines:
        return False
    fields = [f.strip() for f in lines[0].split("｜")]
    if len(fields) != 3 or not all(fields) or any(f in ("不适用", "无", "N/A") for f in fields):
        return True
    if INDEX_PREFIX_RE.match(fields[0].upper()):
        return True
    body = "\n".join(lines[1:]) or blk
    body = FLOW_PHRASE_RE.sub("", body)
    has_level = bool(LEVEL_WITH_PRICE_RE.search(body))
    has_dir = any(w in body for w in DIRECTION_WORDS)
    return has_level and not has_dir


def strip_no_view_blocks(text: str) -> tuple[str, list[str]]:
    """Remove no-view stock blocks from the 个股分析 section deterministically.
    Returns (new_text, removed_blocks_verbatim) — the caller MUST persist the
    removed blocks for audit: a wrong kill is otherwise unrecoverable (K3 P0).

    Also drops index/garbage blocks whose first line carries no ｜ (the
    looks_stock blind spot K3 replayed)."""
    import re as _re
    m = _re.search(r"(?m)^(## 二、个股分析[^\n]*)\n(.*?)(?=^## |\Z)", text, _re.S | _re.M)
    if not m:
        return text, []
    heading, body = m.group(1), m.group(2)
    kept: list[str] = []
    removed: list[str] = []
    for blk in _re.split(r"\n\s*\n", body):
        s = blk.strip()
        if not s:
            continue
        first = s.splitlines()[0]
        is_meta = s.startswith(("*", "-", "声明"))
        looks_stock = ("｜" in first and not is_meta) or (
            INDEX_PREFIX_RE.match(first.split()[0].upper()) if first.split() else False
        )
        if looks_stock and block_is_no_view(s):
            removed.append(s)
            continue
        kept.append(s)
    new_body = "\n\n".join(kept)
    return text[: m.start()] + heading + "\n\n" + new_body + "\n" + text[m.end():], removed


def validate(text: str, *, min_hotspots: int = 4, max_hotspots: int = 9) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if "# 土耳其股市热点日报" not in text:
        errors.append("missing report title line (# 土耳其股市热点日报｜…)")
    if "## 一、热点资讯" not in text:
        errors.append("missing section 一、热点资讯")
    if "## 二、个股分析" not in text:
        errors.append("missing section 二、个股分析")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    if re.search(r"今日关注|今日策略|大V汇总|热门股|重点推荐", text):
        warnings.append("report contains a section/phrase outside the allowed two-chapter structure")

    hot_zone = text.split("## 一、热点资讯", 1)[-1].split("## 二、个股分析", 1)[0]
    hotspots = re.findall(r"(?m)^### 热点\d+", hot_zone)
    if not (min_hotspots <= len(hotspots) <= max_hotspots):
        errors.append(f"热点条数 {len(hotspots)} 超出 {min_hotspots}-{max_hotspots}.")

    # 事件介绍必有；影响分析/券商观点有素材才写（用户规则：没有就省略，不许占位凑数）
    n_intro = len(re.findall(r"\*\*事件介绍", hot_zone))
    if n_intro < len(hotspots):
        errors.append(f"事件介绍字段缺失：{len(hotspots) - n_intro} 条热点没有事件介绍.")
    for m in re.finditer(
        r"\*\*(?:影响分析|券商观点)[：:]\*\*\s*([^*\n]{0,40})"
        r"(?:无新增|未单独点评|暂无|未见|无相关|缺乏)[^。]{0,12}(?:判断|观点|点评|内容)",
        hot_zone,
    ):
        errors.append(f"占位句凑数（无观点应整段省略）: {m.group(0)[:50]}")

    # 个股节为行式排版（聊天窗口友好）：每只股票一个块，首行「名称｜定位｜决策」
    # 恰好两个全角分隔符，其后为观点行，空行分块。docx 渲染时再转 4 列表格。
    stock_zone = text.split("## 二、个股分析", 1)[-1]
    # Drop the remainder of the HEADING line: the template asks for a suffixed
    # title (（仅保留有实质判断的标的）) which otherwise lands in the first
    # block and trips the field check (review P0-1).
    stock_zone = stock_zone.split("\n", 1)[1] if "\n" in stock_zone else ""
    stock_zone = stock_zone.split("\n## ", 1)[0]
    blocks = [
        blk.strip()
        for blk in re.split(r"\n\s*\n", stock_zone)
        if blk.strip() and not blk.strip().startswith(("*", "-", "声明", "---"))
    ]
    stock_rows = 0
    judgment_words = ("支撑", "阻力", "突破", "跌破", "均线", "通道", "业绩", "订单", "利润", "估值", "PE", "并购", "要约", "指数调整", "MSCI", "FTSE", "低估", "高估", "产能", "负债")
    bad_heads = 0
    flow_only = 0
    pure_levels = 0
    seen_tickers: list[str] = []
    INDEX_RE = re.compile(r"^(?:XU\s?\d+|BIST\s?\d*|XBANK)$")
    NESTED_HEAD_RE = re.compile(r"^[A-Z][A-Z0-9.]{1,9}(?:[\s\u3000]|$)")
    for blk in blocks:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        first = lines[0]
        # Blocks must be separated by BLANK lines: a stock head nested in a
        # non-first line means the LLM ran blocks together (review P1-1).
        # Require a TICKER-like leading token too — quoting a大V line like
        # "支撑8000｜阻力8300｜仓位五成" is legitimate detail, not a nested
        # head (K3 A2 false-positive fix).
        for nested in lines[1:]:
            if nested.count("｜") >= 2 and NESTED_HEAD_RE.match(nested):
                errors.append(
                    "个股块之间必须空行分隔（检测到块内嵌套股票首行）: "
                    + nested[:40]
                )
                break
        # chat-friendly block: first line carries 名称｜定位｜决策 (no collapse).
        fields = [f.strip() for f in first.split("｜")]
        # "不适用"-style placeholder fields mark meta/junk blocks (observed:
        # a broker bulk-sweep residue head "KREDİ KARTI DEĞİL｜不适用｜不适用").
        if len(fields) != 3 or not all(fields) or any(f in ("不适用", "无", "N/A") for f in fields):
            bad_heads += 1
            continue
        if len(lines) < 2:
            errors.append(f"个股块缺少观点行（无实质判断应整块删除）: {first[:40]}")
            continue
        norm_key = re.sub(r"\s+", "", fields[0]).upper()
        if norm_key in seen_tickers:
            errors.append(f"个股块重复：{fields[0][:40]} — 同一股票必须合并为一块.")
            continue
        # Index rows: whole-field match OR a leading index token with no
        # boundary assertion — \b does not fire between ASCII and Chinese
        # (both are \w in Unicode mode), and an earlier edit wrote a literal
        # backspace here; prefix-only matching covers "BIST指数"/"XBANK银行指数"
        # without either trap (K3 A4 + dispatch-layer finding).
        if INDEX_RE.match(norm_key) or re.match(r"^(?:XU\s?\d+|BIST\s?\d*|XBANK)", fields[0].upper()):
            errors.append(f"指数 {fields[0]} 不得进入个股分析（指数观点放热点资讯）.")
            continue
        seen_tickers.append(norm_key)
        stock_rows += 1
        if "不入表" in blk or "按规则排除" in blk:
            warnings.append(f"个股块含自我说明（应直接删除）: {first[:60]}")
        has_flow = any(w in blk for w in ("净资金流入", "净资金流出", "资金净流入", "资金净流出"))
        has_judgment = any(w in blk for w in judgment_words)
        if has_flow and not has_judgment:
            flow_only += 1
        # Pure level-quoting technical sweep = no view (user rule 2026-10-12).
        # Delegates to block_is_no_view — the ONE shared predicate. A separate
        # inline word list here drifted from the stripper's and produced a
        # guaranteed-fail loop (validate errors, strip removes nothing,
        # re-validate errors again — K3 P0 replay).
        if block_is_no_view(blk):
            pure_levels += 1
            continue
    if bad_heads:
        errors.append(f"个股块首行缺少「名称｜定位｜决策」三字段分隔: {bad_heads} 块.")
    if pure_levels:
        errors.append(
            f"个股块有 {pure_levels} 块为纯技术位罗列（支撑/阻力位无方向条件判断＝无观点，须整块删除）."
        )
    if flow_only:
        warnings.append(f"个股块有 {flow_only} 个疑似纯资金榜（无实质判断词），规范 §8 不允许单独成块.")
    if not stock_rows:
        warnings.append("个股节无数据块（若X大V资料缺失属预期，否则检查）")

    length = len(re.sub(r"\s", "", text))
    if length < 1200:
        errors.append(f"report too short: {length} chars (< 1200).")

    # 日报形态允许出现券商名与大V名（「券商观点」「大V分析观点」是规范要求的字段），
    # 只拦截"据XX报道/消息"式转载腔，机构观点必须内化表述。
    for m in re.finditer(r"据[^\s，。]{2,12}(?:报道|消息|透露)", text):
        errors.append(f"转载腔句式（须内化表述）: {m.group(0)}")

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings, "hotspots": len(hotspots), "stock_rows": stock_rows}
