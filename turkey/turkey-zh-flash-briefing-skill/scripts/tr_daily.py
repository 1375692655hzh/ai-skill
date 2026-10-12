# -*- coding: utf-8 -*-
"""
tr_daily.py — 土耳其财经快讯每日简报管线（局域网数据站 → 中文编译 → 本地 md + 腾讯文档）

用法:
  python tr_daily.py --config config.json fetch --hours 16
  python tr_daily.py --config config.json build --hours 16 [--summary ...] [--dry-run] [--force]

三道硬闸（拒绝即退出、不上云；每日任务一律用 --hours 让脚本自算窗口）:
  1) 窗口闸: --since 起点必须是今天或昨天，更旧直接拒绝（杜绝旧数据套今天的标题）
  2) 新鲜度闸: 窗口内最新条目时间必须是今天，否则视为数据站停更/过期，拒绝
  3) 一天一份闸: <data_dir>/build_state.json 记录当日已发布版本，同日重复构建拒绝（--force 显式绕过）
  另: 中文标题为空、重点消息摘要为空，也一律拒绝上云

agent 工作流（编译/摘要/验证/汇报）见 SKILL.md。
"""
import json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request

DEF_CFG = {
    "station_base": "http://192.168.110.214:8787/v1/items",
    "api_key": "",
    "api_key_env": "TURKEY_STATION_API_KEY",
    "markets": "土耳其",
    "kinds": "flash",
    "data_dir": "tr_data",
    "doc_title_prefix": "土耳其财经快讯简报",
    "tencent_service": "tencent-docs",
    "chunk_limit": 12000,
    "request_timeout": 20,
    "mcporter_path": "",
}


def load_config(path):
    cfg = dict(DEF_CFG)
    if path and os.path.exists(path):
        cfg.update(json.load(open(path, encoding="utf-8")))
    return cfg


CFG = None


def c(key):
    return CFG[key]


def api_key():
    env = c("api_key_env")
    return os.environ.get(env) or c("api_key") or ""


def norm(s):
    return s.lower().translate(TBL)


TBL = str.maketrans({"ı": "i", "İ": "i", "\u0307": "", "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g",
                     "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u"})

# 关键词初筛表（当日新主题需 agent 人工复核改级）
HIGH = ["global headwinds threaten", "carry trade", "konut analizi", "piyasaya bakis",
        "borsa gun acilis", "yukari adim kurali", "net para girisi", "hurmuz",
        "enflasyon", "tuik", "merkez bankasi", "faiz"]
MED = ["borsa", "banka", "halka arz", "sermaye", "goldman sachs", "jpmorgan", "ubs",
       "savunma", "asels", "tupras", "petrol", "dogalgaz", "ecb", "boj", "fed"]


def auto_tier(text):
    t = norm(text)
    if any(k in t for k in HIGH):
        return 0
    if any(k in t for k in MED):
        return 1
    return 2


def fetch(params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{c('station_base')}?{qs}",
                                 headers={"Authorization": f"Bearer {api_key()}"})
    return json.load(urllib.request.urlopen(req, timeout=c("request_timeout")))


def fetch_items(since_dt):
    cursor, out = "", []
    while True:
        p = {"markets": c("markets"), "kinds": c("kinds"),
             "since": since_dt, "limit": 1000, "dedup": 1}
        if cursor:
            p["cursor"] = cursor
        d = fetch(p)
        out.extend(d["items"])
        cursor = d.get("next_cursor", "")
        if not cursor:
            break
    return out


def write_zh_template(items):
    """生成待填写的中文映射模板：agent 按 (time, source, key) 逐条补 tier 与中文标题。"""
    return [[it["time"][11:16], it["source_id"], "", str(auto_tier(it["text"])), ""]
            for it in sorted(items, key=lambda x: x["time"], reverse=True)]


def match(items, trans):
    used = [False] * len(trans)
    buckets = {0: [], 1: [], 2: []}
    leftover = []
    for it in items:
        hm = it["time"][11:16]
        tn = norm(it["url"] + " " + it["text"])
        hit = None
        for i, (t, src, key, tv, zh) in enumerate(trans):
            if used[i] or t != hm or src != it["source_id"]:
                continue
            if not key or norm(key) in tn:
                hit = i
                break
        if hit is None:
            leftover.append(it)
        else:
            used[hit] = True
            buckets[int(trans[hit][3])].append((it, trans[hit][4]))
    return buckets, leftover


SRC = {"dunya_rss": "Dünya", "cnbce_rss": "CNBC-e", "foreks_rss": "Foreks",
       "twitter_kol_flash": "X/KOL", "sabah_rss": "Sabah",
       "dailysabah_rss": "Daily Sabah", "hurriyet_rss": "Hürriyet"}


def build_md(date, items, buckets, since, summary_text=None):
    n = {tv: len(buckets[tv]) for tv in (0, 1, 2)}
    from collections import Counter
    srcs = Counter(i["source_id"] for i in items)
    src_line = " ｜ ".join(f"{SRC.get(k, k)} {v}" for k, v in srcs.most_common())
    title = {0: "🔴 高重要性", 1: "🟠 中等重要性", 2: "⚪ 低重要性"}
    sub = {0: "—— 宏观数据/财政货币政策/地缘油价，直接影响里拉与BIST定价",
           1: "—— 银行与个股研判、资金与政策动向、重要全球背景",
           2: "—— 公司披露/软新闻/噪音信息"}
    hi = max((i["time"] for i in items), default="?")[11:16]
    out = [f"# {c('doc_title_prefix')} · {date}", "",
           f"**时间窗口**：北京时间 {since}–{hi}　|　**共 {len(items)} 条**"
           f"（高 {n[0]} / 中 {n[1]} / 低 {n[2]}），同级内按时间倒序",
           "**数据来源**：局域网数据站 ai-auto-gen 供数服务"
           f"（markets={c('markets')}，kinds={c('kinds')}，dedup=1）",
           f"**来源分布**：{src_line}",
           (f"**重点消息**：{summary_text}" if summary_text else ""),
           "**说明**：标题为中文编译，点击“原文”查看土耳其语/英语原始报道"]
    for tv in (0, 1, 2):
        out += ["", f"## {title[tv]}（{n[tv]}条）{sub[tv]}", ""]
        for it, zh in sorted(buckets[tv], key=lambda x: x[0]["time"], reverse=True):
            hm = it["time"][11:16]
            src = SRC.get(it["source_id"], it["source_id"])
            out.append(f"- **{hm}**〔{src}〕{zh}｜[原文]({it['url']})")
    return "\n".join(out), n


def tag_of(since_dt):
    return since_dt.replace("-", "").replace(" ", "_").replace(":", "")


def today_str():
    import datetime
    return datetime.date.today().isoformat()


def check_window(since_dt):
    """硬闸1：窗口起点必须落在今天或昨天，否则视为旧数据，直接拒绝。"""
    import datetime
    d = datetime.date.fromisoformat(since_dt[:10])
    age = (datetime.date.today() - d).days
    if age > 1 or age < 0:
        raise SystemExit(f"!! 拒绝：窗口起点 {since_dt} 距今 {age} 天（今天 {today_str()}），疑似旧数据。"
                         "每日任务请用 --hours 16 让脚本自算窗口，勿手传旧 --since。")


def latest_tag():
    """数据目录里最新一次 fetch 的数据档（build --hours 用它，避免时钟漂移错档）。"""
    import glob
    files = glob.glob(os.path.join(data_dir(), "*_items.json"))
    if not files:
        raise SystemExit("!! 拒绝：数据目录没有 *_items.json，请先执行 fetch")
    t = max(os.path.basename(f).replace("_items.json", "") for f in files)
    return f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}"


def data_dir():
    p = c("data_dir")
    return p if os.path.isabs(p) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", p)


def cmd_fetch(since_dt):
    check_window(since_dt)
    os.makedirs(data_dir(), exist_ok=True)
    tag = tag_of(since_dt)
    items = fetch_items(since_dt)
    json.dump(items, open(os.path.join(data_dir(), f"{tag}_items.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    json.dump(write_zh_template(items),
              open(os.path.join(data_dir(), f"{tag}_zh.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"tag={tag} since={since_dt} items={len(items)}")
    for it in sorted(items, key=lambda x: x["time"], reverse=True):
        auto = auto_tier(it["text"])
        txt = it["text"].replace("\n", " ")[:100]
        print(f"[AUTO{auto}][{it['time'][11:16]}] {it['source_id']} | {txt}")
    print("-- 请编辑", os.path.join(data_dir(), f"{tag}_zh.json"),
          "填写分级(第4列)与中文标题(第5列)")


def auto_summary(buckets, limit=4):
    tops = sorted(buckets.get(0, []), key=lambda x: x[0]["time"], reverse=True)[:limit]
    return ("重点消息包括：" + "、".join(zh for _, zh in tops)) if tops else ""


def cmd_build(since_dt, summary=None, dry_run=False, force=False):
    check_window(since_dt)
    tag = tag_of(since_dt)
    date, since = since_dt[:10], since_dt[11:16]
    suffix = "" if since == "14:00" else f"_{since.replace(':', '')}"
    prod_date = today_str()
    state_p = os.path.join(data_dir(), "build_state.json")
    state = json.load(open(state_p, encoding="utf-8")) if os.path.exists(state_p) else {}
    if not dry_run and not force and state.get("prod_date") == prod_date:
        raise SystemExit(f"!! 拒绝：今天（{prod_date}）已发布过一版（tag={state.get('tag')}，"
                         f"{state.get('url')}）。重复投递已忽略；确需重建加 --force")
    items = json.load(open(os.path.join(data_dir(), f"{tag}_items.json"), encoding="utf-8"))
    trans = json.load(open(os.path.join(data_dir(), f"{tag}_zh.json"), encoding="utf-8"))
    if not items:
        raise SystemExit("!! 拒绝：窗口内 0 条，疑似数据站停更；先查 /v1/status 并人工确认，不产出空文档")
    buckets, leftover = match(items, trans)
    if leftover:
        print(f"!! {len(leftover)} 条未匹配翻译:",
              *[f"  {i['time']} {i['source_id']}" for i in leftover], sep="\n")
        sys.exit(1)
    empty_zh = sum(1 for tv in (0, 1, 2) for _, zh in buckets[tv] if not zh.strip())
    if empty_zh:
        raise SystemExit(f"!! 拒绝：{empty_zh} 条中文标题为空（会产出残缺摘要），请先补齐 {tag}_zh.json")
    newest = max(it["time"] for it in items)
    if newest[:10] != prod_date:
        raise SystemExit(f"!! 拒绝：窗口内最新条目时间 {newest} 不是今天（{prod_date}），数据疑似过期/停更；"
                         "先查数据站 /v1/status，确认无误后再 --force")
    if not summary:
        sp = os.path.join(data_dir(), f"{tag}_summary.txt")
        if os.path.exists(sp):
            summary = open(sp, encoding="utf-8").read().strip()
    if not summary:
        summary = auto_summary(buckets)
    if not summary.strip().strip("、，。;；. "):
        raise SystemExit("!! 拒绝：重点消息摘要为空，禁止上云")
    md, n = build_md(prod_date, items, buckets, since, summary)
    out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/output"
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"土耳其快讯_{date}_中文简报{suffix}.md")
    open(md_path, "w", encoding="utf-8").write(md)
    body = md.split("\n", 1)[1].lstrip("\n")
    doc_title = f"{c('doc_title_prefix')} · {prod_date}"

    if dry_run:
        print(f"[dry-run] local md: {md_path} (高{n[0]}/中{n[1]}/低{n[2]}) | summary: {summary[:80]}")
        return

    def tdoc_call(tool, args):
        """直连 mcporter（不经 shell）：分块 ≤12KB 远小于各平台单参数上限。
        不写死 bash —— Windows 上裸 'bash' 可能被 PATH 解析成 WSL bash 导致失败。"""
        exe = c("mcporter_path") or shutil.which("mcporter")
        if not exe:
            raise SystemExit("!! 找不到 mcporter：请安装 mcporter 或在 config 设置 mcporter_path")
        payload = json.dumps(args, ensure_ascii=False)
        r = subprocess.run([exe, "call", c("tencent_service"), tool, "--args", payload],
                           capture_output=True, text=True, timeout=120, errors="replace")
        out = (r.stdout or r.stderr).strip()
        try:
            return json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        except Exception:
            return {"error": out[:300]}

    def chunk_lines(text, limit=None):
        limit = limit or c("chunk_limit")
        chunks, cur, size = [], [], 0
        for ln in text.splitlines():
            cur.append(ln)
            size += len(ln) + 1
            if size >= limit:
                chunks.append("\n".join(cur))
                cur, size = [], 0
        if cur:
            chunks.append("\n".join(cur))
        return chunks

    chunks = chunk_lines(body)
    d = tdoc_call("create_smartcanvas_by_mdx", {"title": doc_title, "mdx": chunks[0]})
    fid, url = d.get("file_id", ""), d.get("url", "")
    print(f"created: {url or d}")
    for i, ch in enumerate(chunks[1:], 2):
        d2 = tdoc_call("smartcanvas.edit",
                       {"file_id": fid, "action": "INSERT_AFTER", "content": ch})
        err = d2.get("error", "")
        print(f"append chunk {i}/{len(chunks)}: {'ok' if not err else err[:200]}")
    print(f"local md: {md_path} (高{n[0]}/中{n[1]}/低{n[2]})")
    print(f"file_id: {fid} | url: {url}")
    import datetime
    json.dump({"prod_date": prod_date, "tag": tag, "since": since_dt, "file_id": fid,
               "url": url, "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
              open(state_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"state -> {state_p}")


def main():
    global CFG
    a = sys.argv[1:]
    cfg_path = "config.json"
    if "--config" in a:
        cfg_path = a[a.index("--config") + 1]
        a = a[:a.index("--config")] + a[a.index("--config") + 2:]
    CFG = load_config(cfg_path)
    if not os.path.isabs(cfg_path) and not os.path.exists(cfg_path):
        CFG = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg_path))
    cmd = a[0] if a else ""
    rest = a[1:]

    def opt(name):
        return rest[rest.index(name) + 1] if name in rest else None

    since = opt("--since")
    if not since and opt("--hours"):
        if cmd == "build":
            since = latest_tag()
            print(f"[build] 挂接最新数据档 since={since}")
        else:
            import datetime
            since = (datetime.datetime.now() - datetime.timedelta(
                hours=int(opt("--hours")))).strftime("%Y-%m-%d %H:%M")
    if not since:
        d0 = rest[0] if rest else today_str()
        t0 = rest[1] if len(rest) > 1 and ":" in rest[1] else "14:00"
        since = f"{d0} {t0}"
    if cmd == "fetch":
        cmd_fetch(since)
    elif cmd == "build":
        cmd_build(since, summary=opt("--summary"), dry_run="--dry-run" in rest,
                  force="--force" in rest)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
