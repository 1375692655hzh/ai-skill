#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble the flash-news prompt from prefiltered fact cards."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BJ_TZ = timezone(timedelta(hours=8))


def _fmt_ts(iso_ts: str, tz=BJ_TZ) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return "??"


def _kap_card(items: list[dict[str, Any]]) -> str:
    if not items:
        return "（本窗口无 KAP 公告素材）"
    lines = []
    for it in items:
        tier = it.get("tier") or "P2"
        mark = "★" if tier == "P0" else " "
        body = it.get("body", "")
        # Wire-enriched entries carry the substance the LLM needs for details.
        limit = 400 if "【电讯补充】" in body else 220
        lines.append(f"{mark} {_fmt_ts(it.get('ts') or '')} | {body[:limit]}")
    return "\n".join(lines)


def _rss_card(items: list[dict[str, Any]]) -> str:
    if not items:
        return "（本窗口无快讯素材）"
    lines = []
    for it in items:
        src = str(it.get("source") or "").upper()
        # body = title + summary[:300]; the summary is what feeds 详情 — keep it.
        lines.append(f"- [{src}] {_fmt_ts(it.get('ts') or '')} | {it.get('body','')[:320]}")
    return "\n".join(lines)


def _tcmb_card(items: list[dict[str, Any]], fx: dict[str, Any]) -> str:
    parts = []
    if items:
        for it in items:
            parts.append(f"- {_fmt_ts(it.get('ts') or '')} | {it.get('title','')[:160]}")
    else:
        parts.append("（本窗口无央行新闻稿）")
    if fx.get("ok"):
        rates = fx.get("rates") or {}
        bits = [f"{code} 买 {v.get('buy')} / 卖 {v.get('sell')}" for code, v in rates.items()]
        parts.append(f"- 官方牌价（{fx.get('date')}，交易日约15:30更新）：{'；'.join(bits)} 兑 TRY")
    else:
        parts.append("- 官方牌价：本窗口未获取（非交易日属正常）")
    return "\n".join(parts)


def _calendar_card(events: list[dict[str, Any]]) -> str:
    if not events:
        return "（日历暂无条目——【明日关注】只能依据素材中明确提到的日程，禁止编造日期）"
    return "\n".join(f"- {e.get('date', '')} {e.get('event', '')}".rstrip() for e in events)


def build_prompt(
    *,
    template_path: Path,
    date_label: str,
    window_start: datetime,
    window_end: datetime,
    kap_items: list[dict[str, Any]],
    rss_items: list[dict[str, Any]],
    tcmb_items: list[dict[str, Any]],
    fx_snapshot: dict[str, Any],
    stats: dict[str, Any],
    calendar_events: list[dict[str, Any]] | None = None,
) -> str:
    tpl = template_path.read_text(encoding="utf-8")
    window_line = (
        f"北京时间 {window_start.astimezone(BJ_TZ).strftime('%m-%d %H:%M')} 至 "
        f"{window_end.astimezone(BJ_TZ).strftime('%m-%d %H:%M')}"
        f"（约 {round((window_end - window_start).total_seconds() / 3600, 1)} 小时）"
    )
    prompt = tpl.replace("{date}", date_label).replace("{window_line}", window_line)
    prompt += (
        f"\n\n---\n\n# 输入素材（已脱敏，仅供内化，不得在成品中标注出处）\n\n"
        f"统计：KAP {stats.get('kap_total', 0)} 条（过滤系统噪声后，★=高优先级）、"
        f"快讯 {stats.get('rss_total', 0)} 条、央行 {stats.get('tcmb_total', 0)} 条。\n\n"
        f"## KAP 法定披露\n{_kap_card(kap_items)}\n\n"
        f"## 央行与官方数据\n{_tcmb_card(tcmb_items, fx_snapshot)}\n\n"
        f"## 市场快讯（BHT 突发 + RSS）\n{_rss_card(rss_items)}\n\n"
        f"## 事件日历卡（未来数日已核实日程）\n{_calendar_card(calendar_events or [])}\n"
    )
    return prompt
