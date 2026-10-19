#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时探测指定 TR 日期的 BHT 收盘综述（供 runner 素材指纹预检用）。

指纹去重曾只读 skill 落盘缓存——23:45 未发文时缓存 article_id=None，
00:10 补跑即使文章已上线也判定"素材无变化"而跳过（2026-10-19 事故）。
本探针直接调 _scan_list_page（内置双抓合并防 CDN 漏卡），输出 JSON。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bht_closing_fetcher import _scan_list_page  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="TR date YYYY-MM-DD")
    args = ap.parse_args()
    hit, status, _anchors = _scan_list_page(date.fromisoformat(args.date))
    out = {
        "status": status,
        "article_id": (hit or {}).get("article_id"),
        "url": (hit or {}).get("url"),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
