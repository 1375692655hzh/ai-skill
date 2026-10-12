# 土耳其报告自动化编排层 (turkey-orchestrator)

7 个土耳其 skill 的统一调度器：运行 skill 生成报告 → 推送飞书群 → 记录运行日志。仅依赖 Python 3.12+ 标准库。

## 文件

| 文件 | 说明 |
|---|---|
| `runner.py` | 编排器核心：调 skill → 定位当日输出 → 推送 → 写 `logs/run.jsonl` |
| `push_wecom.py` | 推送通道：飞书应用消息（文本+文件）、企微 webhook/appchat、告警 |
| `automation_config.example.json` | 配置模板。复制为 `automation_config.json` 并填入真实凭据（此文件含密钥，绝不入仓） |
| `README.md` | 本文件 |

## 核心设计（与 skill 层配合的硬约束）

1. **执行即推**：自动化一旦触发就必须实际生成并推送，不允许任何调度器/脚本级幂等锁。指纹去重只拦截"已有产出且素材无变化"的后续轮次。
2. **禁回退**：`locate_outputs` 只接受 `today_tr` 当天的文件，找不到即判 `gen_failed` + 告警，绝不回退到旧日期文件（历史上造成过 12 次错发旧稿）。
3. **失败隔 5 分钟重跑**：全部任务 `max_retries=2, retry_delay_sec=300`；外层自动化 prompt 再兜一层"失败后 sleep 300 重跑，最多 3 轮"。
4. **素材指纹去重**（close_report）：指纹 = BHT article_id/url、Paraborsa url、Info daily/technical uuid，不含实时行情。补跑时素材无变化 → `ok + dedup=source_fingerprint_unchanged` 短路。
5. **BHT 兜底与二次校验**（close_report）：BHT 缺失时 skill 产出 `*_fallback_*.md` 临时版并写 `_bht_verify_pending.json` 标记；runner 推送临时版，**检测到 pending 标记即带 `--verify-bht` 重跑 skill**（BHT 已发布 → 强刷缓存生成正式版；仍缺失 → skill 原样退出不烧 LLM，走指纹去重）。
6. **today_tr 解析**：从 stdout 后往前扫，先 JSON（morning/close）后纯文本日期，免疫 warning 行。
7. **扩展名校验**：启动时对比 output 最新文件后缀与 `full_pattern` 期望，不一致 fail-fast + 告警。

## 运行

```bash
python runner.py --task close_report --config automation_config.json
```

任务名：`morning_briefing` / `close_report` / `info_daily` / `info_technical` / `aa_morning_briefing` / `paraborsa_all` / `x_influencer` / `zh_flash_briefing`

退出码 0 = `ok` / `ok_fallback` / `skipped_holiday` / `skipped_exists`。

## 日志

- `logs/run.jsonl`：每次执行一行 JSON（status/time/today_tr/files/elapsed）。
- 状态语义：`ok` 正常；`ok_fallback` 临时版已推、正式版待 BHT 校验；`gen_failed`/`push_failed` 需人工介入（先查日志区分"已恢复推送过"与"仍缺产出"）。
