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

    table = text.split("## 二、个股分析", 1)[-1]
    rows = [
        ln
        for ln in table.splitlines()
        if ln.strip().startswith("|") and not re.match(r"^\|[\s:\-|]+\|$", ln.strip())
    ]
    if rows:
        bad_cols = [ln for ln in rows if len(ln.strip().strip("|").split("|")) != 4]
        if bad_cols:
            errors.append(f"个股表存在非4列行 {len(bad_cols)} 条，首条: {bad_cols[0][:60]}")
        header = rows[0].strip().strip("|")
        cells = [c.strip() for c in header.split("|")]
        if cells[:2] != ["股票名称", "定位"]:
            errors.append(f"个股表表头错误: {header[:60]}")
        # Spec §8: pure fund-flow ranking rows are not substantive judgment;
        # self-referential "不入表" rows must never appear.
        judgment_words = ("支撑", "阻力", "突破", "跌破", "均线", "通道", "业绩", "订单", "利润", "估值", "PE", "并购", "要约", "指数调整", "MSCI", "FTSE", "低估", "高估", "产能", "负债")
        flow_only = 0
        for ln in rows[1:]:
            if "不入表" in ln or "排除" in ln:
                warnings.append(f"个股表含自我说明行（应直接删除）: {ln[:60]}")
            has_flow = ("净资金流入" in ln or "净资金流出" in ln or "资金净流入" in ln or "资金净流出" in ln)
            has_judgment = any(w in ln for w in judgment_words)
            if has_flow and not has_judgment:
                flow_only += 1
        if flow_only:
            warnings.append(f"个股表有 {flow_only} 行疑似纯资金榜（无实质判断词），规范 §8 不允许单独入表.")
    else:
        warnings.append("个股表无数据行（若X大V资料缺失属预期，否则检查）")

    length = len(re.sub(r"\s", "", text))
    if length < 1200:
        errors.append(f"report too short: {length} chars (< 1200).")

    # 日报形态允许出现券商名与大V名（「券商观点」「大V分析观点」是规范要求的字段），
    # 只拦截"据XX报道/消息"式转载腔，机构观点必须内化表述。
    for m in re.finditer(r"据[^\s，。]{2,12}(?:报道|消息|透露)", text):
        errors.append(f"转载腔句式（须内化表述）: {m.group(0)}")

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings, "hotspots": len(hotspots), "stock_rows": max(0, len(rows) - 1)}
