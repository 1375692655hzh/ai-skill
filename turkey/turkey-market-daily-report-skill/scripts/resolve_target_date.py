#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve the target date for the orchestrator (per-skill contract file).

turkey-orchestrator/runner.py runs this standalone before each run to anchor
today_tr (skip-if-exists + run-log). Output: single YYYY-MM-DD line on stdout.

Semantics must stay identical to the generator's own resolution in
resolve_inputs.py: the hotspots report is dated by Turkey "today" with NO
weekend/holiday rollback. Do NOT copy the close-report variant here — it
rolls back to the previous business day, which would make skip-if-exists
look for the wrong output filename on weekends/holidays.
"""
from __future__ import annotations

import sys
from pathlib import Path

# 与其余 7 份自包含契约脚本不同，本文件复用 resolve_inputs 保证语义同源；
# PYTHONSAFEPATH=1 会拿掉 sys.path[0]，这里自卫以免编排器预解析静默失效
sys.path.insert(0, str(Path(__file__).resolve().parent))

from resolve_inputs import resolve_target_date as _resolve  # noqa: E402


def main() -> int:
    forced = sys.argv[1] if len(sys.argv) > 1 else None
    print(_resolve({"target_date": forced or "auto"}).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
