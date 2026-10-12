#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Paraborsa all-in report output."""
from __future__ import annotations

import re
import unicodedata


def _norm_tr_token(token: str) -> str:
    """Fold Turkish letters to ASCII so whitelist matching is spelling-proof
    (İ→i, ş→s, ğ→g, ç→c, ö→o, ü→u；ı 无组合符可剥且 lower 不变，双侧同走
    本函数所以自洽。Python 的 str.lower 对 İ 会产生组合变音符，故先 NFKD
    再去组合符)."""
    decomposed = unicodedata.normalize("NFKD", token)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


_TR_CHAR_RE = re.compile(r"[ğşıİçÇöÖüÜĞŞâîûÂÎÛ]")

# 中文总结里合法保留的土语词：券商名/后缀（对齐上方 per-broker wall 正则的
# 38 家清单）+ 常被原文引用的 BIST 权重股公司名与宏观缩写（模板要求用
# ticker，但模型偶用专名，防止 ≥3 阈值误杀整报）。全部存归一化形式；
# 含空格的短语永不匹配单 token，勿加。
_TR_KEEP = {
    _norm_tr_token(t)
    for t in (
        "Yatırım", "İnfo", "İntegral", "İş", "İcbc", "Ahlatcı", "Şeker",
        "Ünlü", "Osmanlı", "Vakıf", "Ziraat", "Halk", "Garanti", "Gedik",
        "Koç", "Türkiye", "İstanbul", "BİST", "İMKB", "TÜİK", "TÜSİAD", "MÜSİAD",
        "Tüpraş", "Sabancı", "Türkcell", "Ereğli", "Karabük", "İşbank", "BİM",
        "TÜFE", "ÜFE",
    )
}


def _turkish_leak_words(text: str) -> list[str]:
    """Words carrying Turkish-specific letters outside the keep-list — the
    signature of the model copying source-language sentences instead of
    translating them (tickers are ASCII and never counted)."""
    words = re.findall(r"[A-Za-zğşıİçÇöÖüÜĞŞâîûÂÎÛ][A-Za-zğşıİçÇöÖüÜĞŞâîûÂÎÛ'’-]{1,}", text)
    leaks: list[str] = []
    for w in words:
        if not _TR_CHAR_RE.search(w):
            continue
        parts = [p for p in re.split(r"['’-]", w) if p]
        if all(_norm_tr_token(p) in _TR_KEEP for p in parts):
            continue
        leaks.append(w)
    return leaks


def _extract_summary_macro(text: str) -> str:
    """Body of the 宏观与市场共识 narrative paragraph (before 个股覆盖汇总)."""
    m = re.search(
        r"宏观与市场共识\s*(.*?)(?=\n个股覆盖汇总|\n\| 标的|\n券商观点速览|\n【|$)",
        text,
        re.S,
    )
    return (m.group(1) if m else "").strip()


# Fluff patterns that indicate the macro narrative is padding, not synthesis.
MACRO_FLUFF_PATTERNS = [
    r"呈现出[^。]*?格局",
    r"权重分化",
    r"整体承压",
    r"高低(切换|轮动)",
    r"结构性轮动",
    r"系统性撤退",
    r"主要标的",
    r"权重蓝筹",
    r"中小盘题材",
    r"周期与成长板块",
    r"防御属性",
]


def validate_summary(text: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(text) < 300:
        errors.append("Summary too short (< 300 chars).")
    if "【综合总结】" not in text and "综合总结" not in text:
        errors.append("Missing summary section header.")
    if "个股覆盖汇总" not in text and "| 标的 |" not in text:
        warnings.append("Summary may be missing ticker table.")
    if "券商观点速览" not in text:
        errors.append("Missing section: 券商观点速览")
    else:
        for slot in ("技术位共识：", "宏观与事件：", "资金与标的："):
            if slot not in text:
                errors.append(f"[券商观点速览] missing structured slot: {slot}")
        # Reject old per-broker wall (many "Name：" lines after 速览)
        m = re.search(r"券商观点速览\s*(.*?)(?=\n分歧点|\n【|$)", text, re.S)
        body = (m.group(1) if m else "").strip()
        # Reject per-broker wall. Threshold >=3 (not 5): even a 3-broker wall
        # means the model listed brokers instead of synthesizing consensus.
        # Broker list covers all 38 BROKER_SLUGS plus common display-name variants
        # (TR chars İ/Ş/ı handled case-insensitively by re.I).
        brokerish = re.findall(
            r"(?m)^(?:A1 Capital|Ahlatcı|Ahlatci|Alnus|Anadolu|Acar|Ata|Allbatross|"
            r"Bizim|Bulls|BTC|Bitci|Deniz|Destek|Colendi|"
            r"Garanti|Gedik|Global|Halk|HSBC|ICBC|"
            r"İnfo|Info|İntegral|Integral|İş|Is|Meksa|Marbas|"
            r"NCM|Nurol|Osmanlı|Osmanli|Oyak|Phillip|QNB|"
            r"Şeker|Seker|Sentiment|Tacirler|Turkish|Unlu|Ünlü|Unlu|"
            r"Vakıf|Vakif|Yatırım Finansman|Yatirim Finansman|Ziraat)[^:\n]{0,20}：",
            body,
            re.IGNORECASE,
        )
        if len(brokerish) >= 3:
            errors.append(
                "[券商观点速览] looks like per-broker wall "
                f"({len(brokerish)} broker-named lines); synthesize into "
                "技术位共识/宏观与事件/资金与标的 only."
            )

    # Ticker table: reject single-broker wall (each row should ideally cite ≥2 brokers;
    # a table dominated by single-broker rows means no real cross-broker synthesis).
    if "| 标的 |" in text or "个股覆盖汇总" in text:
        rows = re.findall(r"\|\s*([A-Z]{3,}[A-Z0-9.]*)\s*\|\s*([^|]+)\|", text)
        if rows:
            single_broker_rows = 0
            for _ticker, brokers_cell in rows:
                # Count broker mentions by comma /、/and separators
                parts = re.split(r"[,，、/]|和|and", brokers_cell)
                parts = [p.strip() for p in parts if p.strip()]
                if len(parts) <= 1:
                    single_broker_rows += 1
            if len(rows) >= 6 and single_broker_rows / len(rows) > 0.6:
                # Downgrade to warning: on low-volatility / thin-coverage days
                # each broker only lists its own contribution stocks, so a
                # single-broker table is the honest synthesis, not model
                # laziness. Blocking here killed the whole 2026-09-03 report.
                warnings.append(
                    f"[个股覆盖汇总] {single_broker_rows}/{len(rows)} rows cite only one broker; "
                    "prefer tickers mentioned by ≥2 brokers."
                )

    # 宏观与市场共识 narrative must not be padding fluff
    macro = _extract_summary_macro(text)
    if macro:
        for fp in MACRO_FLUFF_PATTERNS:
            m = re.search(fp, macro)
            if m:
                errors.append(
                    f"[宏观与市场共识] generic phrase「{m.group(0)}」— synthesize concrete levels/events."
                )
                break

    if "分歧点" not in text:
        warnings.append("Summary may be missing 分歧点.")

    # Source-language leak: 素材是土语原文，模型容易整句复述而非翻译。
    # ≥3 个白名单外土语词 = 硬错误（llm_runner 触发中文重写）；零星 1-2 个
    # 可能是未收录的专有名词，降级 warning 避免误杀。
    leaks = _turkish_leak_words(text)
    if leaks:
        samples = sorted(set(leaks))[:6]
        distinct = {_norm_tr_token(w) for w in leaks}
        if len(distinct) >= 3:
            errors.append(
                f"Turkish text leaked ({len(distinct)} distinct words, "
                f"e.g. {', '.join(samples)}) — rewrite fully in Simplified "
                "Chinese; only tickers/broker names/numbers may stay in original form."
            )
        else:
            warnings.append(f"Possible Turkish words kept: {', '.join(samples)}")

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings, "length": len(text)}


def validate_report(text: str, *, min_articles: int = 1) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(text) < 500:
        errors.append("Output too short (< 500 chars).")
    # Section headers carry a date suffix, e.g. 【综合总结 — 2026-07-27（周一）】
    if not re.search(r"【综合总结[^】]*】", text):
        errors.append("Missing section: 【综合总结】")
    if not re.search(r"【拼接内容[^】]*】", text):
        errors.append("Missing section: 【拼接内容】")
    if "个股覆盖汇总" not in text and "标的" not in text:
        warnings.append("Summary may be missing ticker coverage table.")
    if text.count("--- 第") < min_articles and "未抓到正文" not in text:
        warnings.append("Concat section may have fewer articles than expected.")

    # Re-check summary synthesis rules on the full report
    sub = validate_summary(text)
    for e in sub["errors"]:
        if (
            e not in errors
            and "Summary too short" not in e
            and "summary section header" not in e
            # 拼接内容翻译失败时保留土语原文是设计内兜底，只在 summary 生成
            # 路径上硬拦，全文复检不因拼接段的土语杀整份报告
            and not e.startswith("Turkish text leaked")
        ):
            errors.append(e)
    warnings.extend([w for w in sub["warnings"] if w not in warnings])

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings, "length": len(text)}


def validate(text: str, *, min_articles: int = 1) -> dict:
    return validate_report(text, min_articles=min_articles)
