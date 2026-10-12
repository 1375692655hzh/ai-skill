# -*- coding: utf-8 -*-
"""端到端每日简报生成（编排器入口，其他 turkey skill 同款模式）：

fetch 抓取 → LLM 中文编译+分级（带校验/修复回路）→ 摘要 → build 上云（三道硬闸内置）
→ 打印验收块与结果 JSON（末行，编排器解析日期用）。

用法:
  python scripts/generate_briefing.py --config config.json [--hours 16] [--dry-run] [--skip-fetch]
  python scripts/generate_briefing.py --config config.json --force   # 仅限人工明确要求

环境变量: TURKEY_STATION_API_KEY（数据站）、<llm.api_key_env>（LLM，如 MINIMAX_API_KEY）
退出码: 0=成功/跳过；非0=生成失败（编排器按 retry 策略重跑）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tr_daily  # noqa: E402
from llm_runner import generate_summary, generate_translation  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--hours", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true", help="复用数据目录里最新一档（调试用）")
    ap.add_argument("--force", action="store_true", help="绕过一天一份闸（仅限人工明确要求）")
    args = ap.parse_args()

    tr_daily.CFG = tr_daily.load_config(
        args.config if os.path.exists(args.config)
        else os.path.join(HERE, args.config)
    )
    llm_cfg = tr_daily.CFG.get("llm", {})

    # 1) 抓取（窗口闸内置；build 阶段自动挂接本档）
    if not args.skip_fetch:
        import datetime
        since = (datetime.datetime.now() - datetime.timedelta(hours=args.hours)).strftime(
            "%Y-%m-%d %H:%M")
        tr_daily.cmd_fetch(since)

    tag = tr_daily.tag_of(tr_daily.latest_tag())
    items = json.load(open(os.path.join(tr_daily.data_dir(), f"{tag}_items.json"),
                           encoding="utf-8"))
    if not items:
        print("skip: 窗口内 0 条，疑似数据站停更（详见 /v1/status）")
        return 0

    # 2) LLM 中文编译 + 分级（校验失败自动带反馈重试；三轮不过即失败退出，绝不降级发布）
    arr, info = generate_translation(items, llm_cfg)
    if arr is None:
        print("!! 生成失败：LLM 编译未通过校验（共 {} 轮）".format(info["rounds"]))
        for e in info["last_errors"]:
            print("  -", e)
        return 1
    json.dump(arr, open(os.path.join(tr_daily.data_dir(), f"{tag}_zh.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[llm] 编译通过校验（第 {info['rounds']} 轮）: {len(arr)} 条")

    # 3) 摘要：LLM 从🔴中挑 3-6 条；失败回退脚本自动摘要
    buckets, leftover = tr_daily.match(items, arr)
    tier0 = [zh for _, zh in sorted(buckets[0], key=lambda x: x[0]["time"], reverse=True)]
    summary = generate_summary(tier0, llm_cfg) or tr_daily.auto_summary(buckets)
    with open(os.path.join(tr_daily.data_dir(), f"{tag}_summary.txt"), "w",
              encoding="utf-8") as f:
        f.write(summary)

    # 4) 构建 + 上云（三道硬闸在 cmd_build 内；今天已发布且未 --force 时会被拒绝）
    since = f"{tag[:4]}-{tag[4:6]}-{tag[6:8]} {tag[9:11]}:{tag[11:13]}"
    before = set(os.listdir(tr_daily.data_dir()))
    tr_daily.cmd_build(since, summary=summary, dry_run=args.dry_run, force=args.force)
    state_p = os.path.join(tr_daily.data_dir(), "build_state.json")
    url = ""
    if os.path.exists(state_p):
        st = json.load(open(state_p, encoding="utf-8"))
        if st.get("tag") == tag:
            url = st.get("url", "")

    # 5) 验收块（人读）+ 结果 JSON（末行，编排器解析）
    n = {tv: len(buckets[tv]) for tv in (0, 1, 2)}
    print("==== 验收单 ====")
    print(f"条数: {len(items)}（高{n[0]}/中{n[1]}/低{n[2]}）")
    print(f"窗口: {since} → {max(i['time'] for i in items)}")
    print("🔴 高重要条目:")
    for zh in tier0:
        print("  -", zh)
    print("腾讯文档:", url or ("(dry-run 未上云)" if args.dry_run else "(见 build 输出)"))
    print(json.dumps({"date": tr_daily.today_str(), "tag": tag,
                      "files": [f"output/土耳其快讯_{tag[:4]}-{tag[4:6]}-{tag[6:8]}_中文简报*.md"],
                      "url": url}, ensure_ascii=False))
    _ = before  # 保留变量名以备后续增量判断
    return 0


if __name__ == "__main__":
    sys.exit(main())
