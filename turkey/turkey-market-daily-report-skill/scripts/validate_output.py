#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the daily-report markdown structure (pre-docx gate)."""
from __future__ import annotations

import re




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
    # 两个全角分隔符，其后为观点行，空行分块。docx 渲染时再转 4 列表格。
    stock_zone = text.split("## 二、个股分析", 1)[-1]
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
    seen_tickers: list[str] = []
    for blk in blocks:
        first = blk.splitlines()[0].strip()
        # chat-friendly block: first line carries 名称｜定位｜决策 (no collapse —
        # collapsing merged detail lines into the field check and let 2-field
        # heads pass when the body happened to contain ｜).
        fields = [f.strip() for f in first.split("｜")]
        if len(fields) < 3:
            bad_heads += 1
            continue
        ticker = fields[0].split()[0].upper() if fields[0].split() else ""
        if ticker in seen_tickers:
            errors.append(f"个股块重复：{fields[0][:40]} — 同一股票必须合并为一块.")
            continue
        if ticker in ("XU100", "BIST", "XU030", "XBANK"):
            errors.append(f"指数 {ticker} 不得进入个股分析（指数观点放热点资讯）.")
            continue
        seen_tickers.append(ticker)
        stock_rows += 1
        if "不入表" in blk or "按规则排除" in blk:
            warnings.append(f"个股块含自我说明（应直接删除）: {first[:60]}")
        has_flow = any(w in blk for w in ("净资金流入", "净资金流出", "资金净流入", "资金净流出"))
        has_judgment = any(w in blk for w in judgment_words)
        if has_flow and not has_judgment:
            flow_only += 1
    if bad_heads:
        errors.append(f"个股块首行缺少「名称｜定位｜决策」三字段分隔: {bad_heads} 块.")
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
