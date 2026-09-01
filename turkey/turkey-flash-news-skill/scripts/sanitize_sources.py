#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strip source/platform attribution from LLM inputs and validate final copy."""
from __future__ import annotations

import re
from typing import Iterable

FORBIDDEN_ATTRIBUTION_PATTERNS: tuple[str, ...] = (
    r"Destek\s*Yat[ıi]r[ıi]m",
    r"Bizim\s*Yat[ıi]r[ıi]m",
    r"Bulls\s*Yat[ıi]r[ıi]m",
    r"İnfo\s*Yat[ıi]r[ıi]m",
    r"Info\s*Yat[ıi]r[ıi]m",
    r"Integral\s*Yat[ıi]r[ıi]m",
    r"Paraborsa",
    r"ParaBorsa",
    r"BloombergHT",
    r"Bloomberg\s*HT",
    r"Foreks",
    r"Info\s*Yat[ıi]r[ıi]m\s*Menkul",
    r"Kaynak\s*:",
    r"Günlük\s*Bülten",
    r"Teknik\s*Bülten",
    r"Borsa\s*Yorumu",
    r"JPMorgan",
    r"Morgan\s*Stanley",
    r"Goldman\s*Sachs",
    r"Commerzbank",
    r"Deutsche\s*Bank",
    r"Jefferies",
    r"BBVA",
    r"Citi(?:'|’)?",
    r"MUFG",
    r"Fitch",
    r"Moody(?:'|’)?s",
)

_FORBIDDEN_RE = re.compile(
    "|".join(f"(?:{p})" for p in FORBIDDEN_ATTRIBUTION_PATTERNS),
    re.IGNORECASE,
)

_PROMPT_LABEL_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"##\s*数据源一[^\n]*\n", "## 核心收盘数据\n"),
    (r"##\s*数据源二[^\n]*\n", "## 市场观点与情绪\n"),
    (r"##\s*数据源三[^\n]*\n", "## 技术与公告参考\n"),
    (r"【BloombergHT[^\】]*】", "【收盘数据】"),
    (r"\n【BloombergHT 突发新闻】", "\n【盘中突发】"),
    (r"\n【BloombergHT 重点新闻】", "\n【重点资讯】"),
    (r"标题：[^\n]*\n", ""),
    (r"URL：[^\n]+\n", ""),
    (r"Kaynak\s*:[^\n]+\n", ""),
    (r"BloombergHT[^\n]*为核心来源[^\n]*\n", "正文不得出现平台、券商、研究机构、网站或报告名称。\n"),
    (r"券商观点：[^\n]+\n", "观点冲突时择优采纳，但正文不得写出机构名称。\n"),
    (r"分析师观点", "市场观点"),
)

_BODY_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"【\s*(?:BloombergHT[^\】]*|详细收盘)\s*】", "【收盘数据】"),
    (r"Borsa Yorumu\s*/\s*[^–\-(\n]+", ""),
    (r"Kaynak\s*:\s*[^\n]+", ""),
    (r"İnfo Yatırım Menkul Değerler A\.Ş\.[^\n]*", ""),
    (r"Bu mesaj size\s+KREA\.Digital\s+aracılığı ile gönderilmiştir\.", ""),
)

_ATTRIBUTION_RULE = (
    "- 正文绝对禁止出现平台名、券商名、研究机构名、网站名、报告名、栏目名；"
    "禁止写「某某指出/认为/提示/警告/维持/分析称」等带来源归属的句式。"
    "统一改写为「市场认为」「技术面显示」「盘面上」「策略上建议」等中性表述。\n"
    "- 素材里即便出现机构名称，也只能吸收观点和数据，输出中不得保留这些名称。\n"
)


def _apply_replacements(text: str, rules: Iterable[tuple[str, str]]) -> str:
    out = text
    for pattern, repl in rules:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE | re.MULTILINE)
    return out


def sanitize_material(text: str) -> str:
    if not text:
        return ""
    cleaned = _apply_replacements(text, _BODY_REPLACEMENTS)
    cleaned = _FORBIDDEN_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def sanitize_prompt(prompt: str) -> str:
    if not prompt:
        return ""
    cleaned = _apply_replacements(prompt, _PROMPT_LABEL_REPLACEMENTS)
    if _ATTRIBUTION_RULE not in cleaned:
        cleaned = cleaned.replace("## 数据要求", f"## 数据要求\n{_ATTRIBUTION_RULE}", 1)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


_ATTRIBUTION_VERBS = ("指出", "认为", "提示", "警告", "分析称", "报告显示")
_GENERIC_MARKET_PHRASES = (
    "机构调仓",
    "机构资金",
    "部分机构",
    "大资金",
    "主力资金",
)


def find_forbidden_attributions(text: str) -> list[str]:
    if not text:
        return []
    hits: list[str] = []
    for m in _FORBIDDEN_RE.finditer(text):
        frag = m.group(0).strip()
        if frag and frag not in hits:
            hits.append(frag)
    verb_pattern = "|".join(re.escape(v) for v in _ATTRIBUTION_VERBS)
    for m in re.finditer(rf"[^。；\n]{{0,16}}(?:{verb_pattern})", text):
        prefix = m.group(0)
        if any(ok in prefix for ok in _GENERIC_MARKET_PHRASES):
            continue
        if any(k in prefix for k in ("券商", "研报", "报告", "分析师", "平台")):
            if prefix not in hits:
                hits.append(prefix)
    return hits


def validate_no_attribution(text: str) -> dict:
    hits = find_forbidden_attributions(text)
    return {
        "ok": len(hits) == 0,
        "errors": [f"输出包含来源归属: {h}" for h in hits],
        "hits": hits,
    }
