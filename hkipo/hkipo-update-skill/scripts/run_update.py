#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Main daily refresh entry (07:30 HKT).

Pipeline:
  1. Resolve HK target date
  2. Run 3 collectors in parallel (best-effort, degrade silently)
  3. Merge → UPSERT companies
  4. HKEX prospectus PDF parser fills missing dates
  5. compute_events → UPSERT events
  6. Compute daily_diff vs prior snapshot
  7. Write output/{date}/daily_digest.md

Exit codes: 0 success, 1 hard failure (no source succeeded), 2 config error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# Allow running both as `python scripts/run_update.py` (cwd = skill root) and
# as `python run_update.py` from within scripts/.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from collectors import (  # noqa: E402
    aastocks_ipo,
    hkex_appindex,
    hkex_new_listings,
    hkex_prospectus_pdf,
    tiger_ipo,
)
from lib import compute_events, db, normalize  # noqa: E402
from resolve_target_date import resolve_target_date  # noqa: E402
from runtime_utils import configure_stdio, resolve_skill_paths, set_env  # noqa: E402


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def render_digest(
    target_iso: str,
    new_filings: list,
    new_hearings: list,
    new_offers: list,
    new_listings: list,
    in_offer: list,
    coming_soon: list,
    sources_status: dict,
) -> str:
    L = [f"# 港股新股每日变动 — {target_iso}\n"]

    def _bullet(items, label):
        L.append(f"## {label}")
        if not items:
            L.append("- 无\n")
            return
        for it in items:
            name = it.get("name_zh") or it.get("name_en") or "—"
            code = it.get("stock_code") or ""
            extra = ""
            if it.get("offer_price_min") is not None:
                lo, hi = it.get("offer_price_min"), it.get("offer_price_max") or it.get("offer_price_min")
                extra += f" 招股价 {lo}–{hi} 港元"
            if it.get("listing_date"):
                extra += f" 上市 {it['listing_date']}"
            L.append(f"- {name}（{code}）{extra}".rstrip())
        L.append("")

    _bullet(new_filings, "新递表")
    _bullet(new_hearings, "新聆讯（PHIP）")
    _bullet(new_offers, "新招股")
    _bullet(new_listings, "新上市")

    L.append("## 招股中")
    if in_offer:
        L.append("| 公司 | 代码 | 招股价 | 每手 | 截止 | 上市日 |")
        L.append("|---|---|---|---|---|---|")
        for c in in_offer:
            lo = c.get("offer_price_min")
            hi = c.get("offer_price_max") or lo
            price = f"{lo}–{hi}" if lo else "—"
            L.append(
                f"| {c.get('name_zh') or '—'} | {c.get('stock_code') or ''} | {price} | "
                f"{c.get('board_lot') or '—'} | {c.get('cash_close_date') or '—'} | "
                f"{c.get('listing_date') or '—'} |"
            )
    else:
        L.append("（无）")
    L.append("")

    L.append("## 即将上市（7 天内）")
    if coming_soon:
        for c in coming_soon:
            L.append(
                f"- {c.get('name_zh') or '—'}（{c.get('stock_code') or ''}） "
                f"上市 {c.get('listing_date') or '—'} / 暗盘 {c.get('grey_date') or '—'} 16:15–18:30"
            )
    else:
        L.append("（无）")
    L.append("")

    L.append("## 数据源状态")
    for k, v in sources_status.items():
        L.append(f"- {k}: {v}")
    return "\n".join(L) + "\n"


def run(config_path: Path, force_date: str | None = None) -> int:
    configure_stdio()
    config = load_config(config_path)
    skill_dir, workdir, data_dir, output_dir, db_path = resolve_skill_paths(config_path, config)
    set_env(skill_dir, data_dir, output_dir)

    holidays = config.get("holidays") or []
    target = resolve_target_date(force_date, holidays)
    target_iso = target.isoformat()
    snapshot_dir = data_dir / target_iso
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    print(f"== hkipo update target={target_iso}")
    print(f"   skill={skill_dir}")
    print(f"   data={data_dir}")
    print(f"   db={db_path}")

    sources_cfg = config.get("sources") or {}
    sources_status: dict = {}

    # 1. Collect from 5 sources (HKEX new_listings is the primary in_offer trigger)
    hkex_rows, st1 = hkex_appindex.collect(sources_cfg.get("hkex_appindex") or {}, snapshot_dir)
    new_listing_rows, st1b = hkex_new_listings.collect(
        sources_cfg.get("hkex_new_listings") or {}, snapshot_dir
    )
    tiger_rows, st2 = tiger_ipo.collect(sources_cfg.get("tiger_json") or {}, snapshot_dir)
    aastocks_rows, st3 = aastocks_ipo.collect(sources_cfg.get("aastocks") or {}, snapshot_dir)
    sources_status.update(st1)
    sources_status.update(st1b)
    sources_status.update(st2)
    sources_status.update(st3)

    if all(v.startswith(("degraded", "disabled")) for v in sources_status.values()):
        print(f"[ABORT] all sources degraded: {sources_status}", file=sys.stderr)
        return 1

    # 2. Normalize each source
    hkex_norm = [normalize.normalize_hkex_appindex(r) for r in hkex_rows]
    new_listing_norm = [normalize.normalize_hkex_new_listing(r) for r in new_listing_rows]
    aastocks_norm = [normalize.normalize_aastocks(r) for r in aastocks_rows]
    tiger_norm = [normalize.normalize_tiger(r) for r in tiger_rows]

    # 3. Merge by stock_code (later overrides earlier; see normalize.merge_companies
    # docstring for the precedence contract)
    merged = normalize.merge_companies(
        hkex_norm,         # baseline: 递表/聆讯/已上市
        new_listing_norm,  # in_offer trigger (招股主判定)
        aastocks_norm,     # cross-check
        tiger_norm,        # 字段补充：股价/每手/市值/截止时间
    )

    # 4. PDF parser fills missing dates for in-offer / hearing companies
    pdf_cfg = sources_cfg.get("hkex_pdf") or {}
    pdf_dates, st4 = hkex_prospectus_pdf.collect(
        pdf_cfg, snapshot_dir, list(merged.values())
    )
    sources_status.update(st4)
    for code, dates in pdf_dates.items():
        if code in merged:
            merged[code].update({k: v for k, v in dates.items() if v})

    # 5. Open DB, capture prior state for diff, then UPSERT companies + events
    with db.connect(db_path) as conn:
        prior_status = {
            row["stock_code"]: (row["status"], row["listing_date"])
            for row in conn.execute("SELECT stock_code, status, listing_date FROM companies")
        }

        for code, comp in merged.items():
            db.upsert_company(conn, comp)
            n = compute_events.compute_and_upsert(
                conn,
                comp,
                extra_holidays=holidays,
                margin_offset_days=int(
                    (sources_cfg.get("tiger_json") or {}).get("margin_offset_days", 1)
                ),
            )
            if n:
                print(f"   {code}: {n} events upserted")

        # 6. Compute daily_diff: new filings / hearings / offers / listings
        all_now = db.list_companies(conn)
        new_filings = []
        new_hearings = []
        new_offers = []
        new_listings = []
        for c in all_now:
            code = c["stock_code"]
            prev = prior_status.get(code)
            cur_status = c.get("status")
            if prev is None and cur_status == normalize.STATUS_FILED:
                new_filings.append(c)
            elif prev and prev[0] != normalize.STATUS_HEARING and cur_status == normalize.STATUS_HEARING:
                new_hearings.append(c)
            elif prev and prev[0] != normalize.STATUS_IN_OFFER and cur_status == normalize.STATUS_IN_OFFER:
                new_offers.append(c)
            elif prev and prev[0] != normalize.STATUS_LISTED and cur_status == normalize.STATUS_LISTED:
                new_listings.append(c)
            elif prev is None and cur_status == normalize.STATUS_IN_OFFER:
                new_offers.append(c)

        in_offer = [c for c in all_now if c.get("status") == normalize.STATUS_IN_OFFER]
        today_plus7 = date.fromisoformat(target_iso).toordinal() + 7
        coming_soon = sorted(
            [
                c for c in all_now
                if c.get("listing_date")
                and target_iso <= c["listing_date"] <= date.fromordinal(today_plus7).isoformat()
            ],
            key=lambda c: c.get("listing_date") or "",
        )

        db.upsert_daily_diff(
            conn, target_iso,
            new_filings, new_hearings, new_offers, new_listings,
            sources_status,
        )

    # 7. Write daily_digest.md
    out_dir = output_dir / target_iso
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = render_digest(
        target_iso, new_filings, new_hearings, new_offers, new_listings,
        in_offer, coming_soon, sources_status,
    )
    digest_path = out_dir / "daily_digest.md"
    digest_path.write_text(digest, encoding="utf-8")
    # 生成 brief 版供 runner 以文本形式发送到飞书群
    brief_text = digest[:3000].strip()
    if len(digest) > 3000:
        brief_text += "\n\n...（内容较长，完整版见文件）"
    brief_path = out_dir / "daily_digest_brief.md"
    brief_path.write_text(brief_text, encoding="utf-8")
    print(f"✅ digest -> {digest_path}")
    print(f"✅ brief  -> {brief_path}")
    print(f"   new_filings={len(new_filings)} new_hearings={len(new_hearings)} "
          f"new_offers={len(new_offers)} new_listings={len(new_listings)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="HK IPO daily refresh")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--force-date", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()
    cfg = Path(args.config)
    if not cfg.is_file():
        alt = HERE.parent / "config.json"
        ex = HERE.parent / "config.example.json"
        if alt.is_file():
            cfg = alt
        elif ex.is_file():
            print("config.json 不存在, 请先: Copy-Item config.example.json config.json", file=sys.stderr)
            sys.exit(2)
        else:
            print(f"config not found: {args.config}", file=sys.stderr)
            sys.exit(2)
    rc = run(cfg, force_date=args.force_date)
    sys.exit(rc)


if __name__ == "__main__":
    main()
