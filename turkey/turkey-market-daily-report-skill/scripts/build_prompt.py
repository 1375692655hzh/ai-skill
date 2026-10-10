#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble the daily-report prompt from the located input bundle."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any


SOURCE_LABELS = {
    "info_daily": "当日 Info 每日市场分析",
    "x_influencer": "当日 X 大V全文",
    "aa_morning": "当日 AA 通讯社晨报",
    "morning_briefing": "当日开盘前早报",
    "info_technical": "当日 Info 技术分析",
    "close_report": "前一交易日收评",
    "paraborsa": "前一交易日 Paraborsa 券商评论",
}


def _bundle_line(hit: dict[str, Any]) -> str:
    fb = "（注意：当日文件缺失，此为前一日文件回退）" if hit.get("fallback") else ""
    return f"- {SOURCE_LABELS.get(hit['key'], hit['key'])}: {hit['path'].name} {fb}".rstrip()


def build_prompt(
    *,
    template_path: Path,
    target: date,
    bundle: dict[str, Any],
) -> str:
    tpl = template_path.read_text(encoding="utf-8")
    date_cn = f"{target.year}年{target.month}月{target.day}日"
    prompt = tpl.replace("{date_cn}", date_cn)

    lines = ["\n\n---\n\n# 资料范围", ""]
    for key in ("info_daily", "x_influencer", "aa_morning", "morning_briefing", "info_technical"):
        hit = bundle["same_day"].get(key)
        lines.append(_bundle_line(hit) if hit else f"- {SOURCE_LABELS[key]}: 缺失")
    for key in ("close_report", "paraborsa"):
        hit = bundle["prev_day"].get(key)
        label = f"{SOURCE_LABELS[key]}（{bundle['prev_trading_day'].isoformat()}）"
        lines.append(_bundle_line({**hit, "key": key}) if hit else f"- {label}: 缺失")
    x_hit = bundle["same_day"].get("x_influencer")
    if x_hit:
        n_posts = x_hit["text"].count("### ")
        lines.append(f"- X 窗口统计：全文约 {n_posts} 个帖子条目")
    prompt += "\n".join(lines)

    sections = []
    title_map = [
        ("close_report", "## 素材一：前一交易日收评"),
        ("paraborsa", "## 素材二：前一交易日 Paraborsa 券商评论"),
        ("info_daily", "## 素材三：当日 Info 每日市场分析"),
        ("morning_briefing", "## 素材四：当日开盘前早报"),
        ("info_technical", "## 素材五：当日 Info 技术分析"),
        ("aa_morning", "## 素材六：当日 AA 通讯社晨报"),
        ("x_influencer", "## 素材七：当日 X 大V全文（含原文与译文；土语原文可直接内化）"),
    ]
    for key, title in title_map:
        hit = bundle["same_day"].get(key) or bundle["prev_day"].get(key)
        if not hit:
            continue
        sections.append(f"{title}（{hit['path'].name}）\n\n{hit['text']}")
    prompt += "\n\n---\n\n# 输入素材（仅供内化）\n\n" + "\n\n".join(sections)
    return prompt
