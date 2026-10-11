# -*- coding: utf-8 -*-
"""合并 agent 分片写出的 zh JSON -> <tag>_zh.json，并对账匹配（leftover=0 才放行上云）。

用法:
  python merge_zh_parts.py --tag 20261008_2231 --parts _zh_a.json,_zh_b.json
  python merge_zh_parts.py --tag 20261008_2231 --parts "_zh_*.json" --data-dir ../tr_data

分片内可放「占位」结尾的哨兵条目防 Write 截断，合并时自动剔除；
条目格式 [time(HH:MM), source_id, key, tier, 中文标题]，与 fetch 生成的模板一致。
"""
import json, os, sys, glob, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tr_daily  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tag", required=True)
ap.add_argument("--parts", required=True, help="逗号分隔的文件名或 glob，相对 data-dir")
ap.add_argument("--data-dir", default=None)
ap.add_argument("--config", default="config.json")
args = ap.parse_args()

tr_daily.CFG = tr_daily.load_config(args.config)
DATA = args.data_dir or tr_daily.data_dir()

trans = []
total_in = 0
for pat in args.parts.split(","):
    pat = pat.strip()
    files = sorted(glob.glob(os.path.join(DATA, pat))) if any(ch in pat for ch in "*?[") \
        else [os.path.join(DATA, pat)]
    for fp in files:
        data = json.load(open(fp, encoding="utf-8"))
        n0 = len(data)
        data = [e for e in data if len(e) >= 5 and "占位" not in str(e[-1])]
        print(f"{os.path.basename(fp)}: {n0} -> {len(data)} (剔除占位 {n0 - len(data)})")
        total_in += n0
        trans.extend(data)

print("合并总条数:", len(trans))
items = json.load(open(os.path.join(DATA, f"{args.tag}_items.json"), encoding="utf-8"))
print("items:", len(items))
if len(trans) != len(items):
    print("!! 条数不一致，停止")
    sys.exit(1)

empty = [e for e in trans if not str(e[4]).strip()]
if empty:
    print("!! 空中文标题:", empty)
    sys.exit(1)

buckets, leftover = tr_daily.match(items, trans)
n = {tv: len(buckets[tv]) for tv in (0, 1, 2)}
print("匹配结果: 高{} / 中{} / 低{} / 未匹配{}".format(n[0], n[1], n[2], len(leftover)))
for it in leftover:
    print("  leftover:", it["time"], it["source_id"], it["text"][:60])
if leftover:
    sys.exit(1)
assert sum(n.values()) == len(items), "已用翻译数与条目数不一致（存在错配）"

out = os.path.join(DATA, f"{args.tag}_zh.json")
json.dump(trans, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("OK ->", out)
