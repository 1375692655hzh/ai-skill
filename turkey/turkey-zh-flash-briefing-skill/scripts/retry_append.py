# -*- coding: utf-8 -*-
"""重试向腾讯文档追加剩余分块（HTTP 405 等瞬时报错后的补发工具）。
直连 mcporter，不经 shell（Windows 上裸 'bash' 可能解析到 WSL 导致失败）。

用法: python retry_append.py <file_id> <md路径> [--config config.json]"""
import json, subprocess, os, re, sys, shutil

argv = sys.argv[1:]
cfg_path = "config.json"
if "--config" in argv:
    cfg_path = argv[argv.index("--config") + 1]
    argv = argv[:argv.index("--config")] + argv[argv.index("--config") + 2:]
if len(argv) < 2:
    raise SystemExit(__doc__)
fid, md_path = argv[0], argv[1]
md = open(md_path, encoding='utf-8').read()
body = md.split("\n", 1)[1].lstrip("\n")

limit = 12000
service = 'tencent-docs'
mcporter_path = ''
for cp in [cfg_path, os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg_path)]:
    if os.path.exists(cp):
        c = json.load(open(cp, encoding='utf-8'))
        limit = c.get('chunk_limit', limit)
        service = c.get('tencent_service', service)
        mcporter_path = c.get('mcporter_path', '')
        break

exe = mcporter_path or shutil.which('mcporter')
if not exe:
    raise SystemExit("!! 找不到 mcporter：请安装 mcporter 或在 config 设置 mcporter_path")

chunks, cur, size = [], [], 0
for ln in body.splitlines():
    cur.append(ln)
    size += len(ln) + 1
    if size >= limit:
        chunks.append("\n".join(cur))
        cur, size = [], 0
if cur:
    chunks.append("\n".join(cur))
print('chunks:', len(chunks), [len(c) for c in chunks])

for i, ch in enumerate(chunks[1:], 2):
    payload = json.dumps({"file_id": fid, "action": "INSERT_AFTER", "content": ch},
                         ensure_ascii=False)
    r = subprocess.run([exe, 'call', service, 'smartcanvas.edit', '--args', payload],
                       capture_output=True, text=True, timeout=120, errors='replace')
    out = (r.stdout or r.stderr).strip()
    print(f'chunk {i}:', out[:200])
