#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KAP (Borsa Istanbul) official disclosure fetcher.

Endpoint contract (verified 2026-08-31):
- POST https://www.kap.org.tr/tr/api/disclosure/members/byCriteria
- JSON body {"fromDate":"YYYY-MM-DD","toDate":"YYYY-MM-DD","member":"","disclosureClass":""}
- Requires Referer header; legacy GET /tr/api/disclosure/list is 404.
- publishDate format "DD.MM.YYYY HH:MM" in TRT (UTC+3). System is 7x24;
  financial reports are legally published AFTER the close (18:00-24:00 TRT).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

TRT = timezone(timedelta(hours=3))
KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (turkey-flash-news-skill/1.0)",
    "Referer": "https://www.kap.org.tr/tr/",
    "Content-Type": "application/json",
}
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})")


def _parse_publish_date(raw: str) -> Optional[datetime]:
    m = DATE_RE.match(raw or "")
    if not m:
        return None
    d, mo, y, h, mi = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=TRT).astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_kap_disclosures(
    *, from_date: str, to_date: str, timeout: int = 40, retries: int = 3
) -> list[dict[str, Any]]:
    """from_date/to_date: YYYY-MM-DD (TRT calendar). Returns store-ready items."""
    body = {"fromDate": from_date, "toDate": to_date, "member": "", "disclosureClass": ""}
    last_err = ""
    for attempt in range(max(1, retries)):
        try:
            resp = requests.post(KAP_API, json=body, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            arr = resp.json()
            if not isinstance(arr, list):
                raise RuntimeError(f"KAP returned non-array: {str(arr)[:120]}")
            break
        except Exception as exc:
            last_err = str(exc)
            if attempt + 1 < max(1, retries):
                print(f"[kap] retry {attempt + 1}: {last_err[:140]}", file=sys.stderr)
                import time

                time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"KAP request failed: {last_err[:200]}")

    items: list[dict[str, Any]] = []
    for it in arr:
        raw_date = str(it.get("publishDate") or "")
        ts = _parse_publish_date(raw_date)
        code = str(it.get("stockCodes") or "").strip()
        company = str(it.get("kapTitle") or "").strip()
        subject = str(it.get("subject") or "").strip()
        if not subject:
            continue
        from store import stable_id

        items.append(
            {
                "id": stable_id("kap", it.get("disclosureIndex") or raw_date, code, company, subject),
                "source": "kap",
                "kind": "disclosure",
                "ts": ts.isoformat() if ts else None,
                "raw_time": raw_date,
                "title": subject,
                "body": f"[{code} {company}] {subject}"
                + (f" — {it.get('summary')}" if it.get("summary") else ""),
                "ticker": code,
                "company": company,
                "url": "https://www.kap.org.tr/tr/bildirim-sorgu",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return items


if __name__ == "__main__":
    today_tr = datetime.now(TRT).date()
    data = fetch_kap_disclosures(
        from_date=(today_tr - timedelta(days=1)).isoformat(), to_date=today_tr.isoformat()
    )
    print(f"{len(data)} disclosures")
    for d in data[-5:]:
        print(" ", d["raw_time"], "|", d["body"][:90])
