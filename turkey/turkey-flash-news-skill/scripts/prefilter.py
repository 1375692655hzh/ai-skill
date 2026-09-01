#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rule-based pre-filter between the store and the LLM prompt.

Empirically (7d sample, 2026-08-31): ~35% of the KAP morning cluster is
mechanical noise (circuit-breaker notices, BISTECH system announcements).
Those never reach the LLM. Everything else gets a tier tag; caps per source
keep the prompt bounded in earnings season (18:00-24:00 TRT can flood).
"""
from __future__ import annotations

import re
from typing import Any

# KAP subjects that are pure system/procedural noise → dropped before the prompt.
# Based on observed subject distribution (2026-08-31, 393 disclosures/day):
# 189 circuit-breaker + 18 info forms + 11 broker warrants + 9 weekly reports
# + 5 market-making ≈ 59% of raw volume carries zero market-moving content.
NOISE_PATTERNS = [
    r"Devre Kesici",            # per-stock circuit-breaker notices
    r"BISTECH",                 # exchange system announcements
    r"Alım Satım Sistemi Duyurusu",
    r"Şirket Genel Bilgi Formu",  # routine company info form updates
    r"Varant - Sertifika",        # broker warrant/certificate issues
    r"Piyasa Yapıcılığ",          # market-making transaction reports
    r"Haftalık Rapor",            # weekly fund/trust reports
    r"Kısa Dönem Borçlanma",     # routine treasury ops
    r"Bültene ek",
]

# KAP subjects that matter most → P0 (cited first by the LLM).
P0_PATTERNS = [
    r"Özel Durum",              # material developments (ÖDA)
    r"Haber ve Söylentilere",    # press-rumor clarifications (market-moving)
    r"Yeni İş İlişkisi",         # new business relationships (contracts)
    r"Finansal Tablo",           # financial statements
    r"Finansal Rapor",
    r"Bilanço",
    r"Temettü",                  # dividends
    r"Bedelsiz|Bedelli",         # capital increases
    r"Geri Al[ıi]m",             # buybacks
    r"Birleşme|Bölünme",         # M&A / spinoffs
    r"Halka Arz",                # IPOs
    r"Tahvil|Bono",              # debt issuance
]

# Label-only P2 categories: never get individual lines in the digest; the
# template forces them into one merged summary line unless enriched by wires.
LABEL_ONLY_HINTS = (
    r"Sermaye Art[ıi]r[ıi]m",    # capital changes (routine filings)
    r"Kredi Derecelendir",       # rating actions (no figures in label)
    r"Pay Dışında Sermaye Piyasası Aracı",  # non-equity instrument ops
    r"Genel Kurul",              # shareholder-meeting procedure
    r"Bağımsız Denetim",         # auditor selection etc.
)

# Priority ordering for trimming when over cap.
TIER_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def classify_kap(item: dict[str, Any], *, noise: list[str], p0: list[str]) -> str:
    text = f"{item.get('title','')} {item.get('body','')}"
    if any(re.search(p, text, re.IGNORECASE) for p in noise):
        return "P3"
    if any(re.search(p, text, re.IGNORECASE) for p in p0):
        return "P0"
    return "P2"


def is_label_only(item: dict[str, Any]) -> bool:
    """P2 procedural filing — no substance unless enriched by wire reports."""
    text = f"{item.get('title','')} {item.get('body','')}"
    return any(re.search(p, text, re.IGNORECASE) for p in LABEL_ONLY_HINTS)


def enrich_kap_with_rss(
    kap_items: list[dict[str, Any]],
    rss_items: list[dict[str, Any]],
    *,
    max_lines_per_item: int = 2,
) -> int:
    """Cross-fill KAP label-only disclosures with wire-report substance.

    Turkish wires (Foreks/CNBC-e) transcribe KAP disclosures within minutes
    and include figures, usually referencing the company by $TICKER or name.
    Returns the number of KAP items enriched.
    """
    enriched = 0
    for kap in kap_items:
        ticker = (kap.get("ticker") or "").strip()
        company = (kap.get("company") or "").strip()
        patterns = []
        if ticker:
            # Wires reference tickers as bare codes or $CODE; word-boundary only.
            patterns.append(re.compile(rf"(?:\$|\b){re.escape(ticker)}\b", re.IGNORECASE))
        # Full company-name phrase only — single words like TÜRKİYE/ZORLU are
        # common Turkish words and produce false matches.
        if 8 <= len(company) <= 60:
            patterns.append(re.compile(rf"\b{re.escape(company)}\b", re.IGNORECASE))
        if not patterns:
            continue
        hits: list[str] = []
        for rss in rss_items:
            text = f"{rss.get('title','')} {rss.get('body','')}"
            if any(p.search(text) for p in patterns):
                hits.append(f"[{rss.get('source','')}] {rss.get('title','')}")
            if len(hits) >= max_lines_per_item:
                break
        if hits:
            kap["body"] = kap.get("body", "") + " 【电讯补充】 " + " / ".join(hits)
            kap["enriched"] = True
            enriched += 1
    return enriched


def prefilter(
    items: list[dict[str, Any]],
    *,
    noise_patterns: list[str] | None = None,
    p0_patterns: list[str] | None = None,
    kap_cap: int = 120,
    rss_cap_per_source: int = 40,
    tcmb_cap: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Returns {'kap': [...], 'rss': [...], 'tcmb': [...]} with tier tags + caps."""
    noise_patterns = noise_patterns or NOISE_PATTERNS
    p0_patterns = p0_patterns or P0_PATTERNS

    kap: list[dict[str, Any]] = []
    rss: list[dict[str, Any]] = []
    tcmb: list[dict[str, Any]] = []

    for it in items:
        src = it.get("source") or ""
        if src == "kap":
            it = dict(it)
            it["tier"] = classify_kap(it, noise=noise_patterns, p0=p0_patterns)
            if it["tier"] != "P3":
                kap.append(it)
        elif src == "tcmb_press":
            tcmb.append(dict(it, tier="P0"))
        elif src == "bht":
            rss.append(dict(it, tier="P1"))
        elif it.get("kind") == "rss":
            rss.append(dict(it, tier="P1"))

    # KAP: keep every P0; trim P2 by recency when over cap.
    if len(kap) > kap_cap:
        p0 = [x for x in kap if x["tier"] == "P0"]
        p2 = sorted(
            (x for x in kap if x["tier"] != "P0"),
            key=lambda x: x.get("ts") or "",
            reverse=True,
        )
        kap = p0 + p2[: max(0, kap_cap - len(p0))]

    # RSS: per-source recency cap (BHT items are also 'rss' bucket here).
    by_source: dict[str, list[dict[str, Any]]] = {}
    for it in rss:
        by_source.setdefault(it.get("source") or "?", []).append(it)
    trimmed: list[dict[str, Any]] = []
    for src, group in by_source.items():
        group.sort(key=lambda x: x.get("ts") or "", reverse=True)
        trimmed.extend(group[:rss_cap_per_source])
    trimmed.sort(key=lambda x: x.get("ts") or "")

    tcmb = tcmb[:tcmb_cap]
    return {"kap": kap, "rss": trimmed, "tcmb": tcmb}
