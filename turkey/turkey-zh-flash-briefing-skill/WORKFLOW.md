# 工作流（WORKFLOW）：两种跑法 + 编排器接入

本 skill 有**两种等价的生产模式**，产出规范完全一致（同一套闸门与验收单）：

## 模式 A：全自动（编排器/LLM，推荐接入 turkey-orchestrator）

一条命令端到端，编译由 LLM 完成、带三轮校验/修复回路：

```bash
export TURKEY_STATION_API_KEY="ak_live_..."
export TURKEY_BRIEFING_LLM_API_KEY="sk-..."   # 任意 OpenAI 兼容端点，见 config.llm
python scripts/generate_briefing.py --config config.json --hours 16
```

内部步骤（任一步不合格即非零退出，绝不降级发布）：

```
fetch(窗口闸内置) → LLM 编译五列 JSON
  ├─ 校验: 条数==输入 / 无空标题 / tier∈012 / key 为真实 ASCII 子串 / match leftover==0
  └─ 不过 → 带错误反馈重试，最多 3 轮 → 仍不过 exit 1
摘要(LLM 从🔴挑 3-6 条；格式不合格回退脚本自动摘要)
build(三道硬闸: 窗口闸/新鲜度闸/一天一份闸 + 空标题/空摘要拒绝)
验收单 + 末行结果 JSON（编排器解析日期与文件）
```

### cron（北京时间）

```
# 主跑
30 14 * * *  <python> scripts/generate_briefing.py --config <路径>/config.json --hours 16
# 失败重试
0  15 * * *  同上
```

### 接入 turkey-orchestrator

在 `../turkey-orchestrator/automation_config.json` 的 `tasks` 中加入（完整示例见其 `automation_config.example.json` 的 `zh_flash_briefing` 条目）：

```json
"zh_flash_briefing": {
  "enabled": true,
  "title_prefix": "土耳其财经快讯简报",
  "skill_dir": "<部署路径>/turkey-zh-flash-briefing-skill",
  "command": ["python", "scripts/generate_briefing.py", "--config", "config.json", "--hours", "16"],
  "run_cwd": "<部署路径>/turkey-zh-flash-briefing-skill",
  "output_dir": "output",
  "full_pattern": "土耳其快讯_{date}_中文简报*.md",
  "send_full_as_file": true,
  "skip_keywords": ["skip:", "0 条", "拒绝"],
  "max_retries": 2,
  "retry_delay_sec": 300
}
```

编排器会：运行 → 定位当日 output 文件 → 推飞书 → 写 `logs/run.jsonl`。本 skill 的末行 JSON 即编排器的 `today_tr` 日期来源。

## 模式 B：agent 模式（人机协作，质量上限最高）

agent 按 `SKILL.md` 的"每日七步"执行：编译/分级由 agent 逐条人工完成（关键数字更准、簇内降级判断更细），脚本只负责抓取/对账/上云。**日常人工跑或质量复核日用此模式。**

## 产出验收单（两种模式都必须出现在汇报里）

```
条数: N（高X/中Y/低Z）
窗口: YYYY-MM-DD HH:MM → YYYY-MM-DD HH:MM
🔴 高重要条目: （逐条列出）
腾讯文档: <url>
```

用户可以用验收单直接核对覆盖度——**N 与数据站窗口条数不符、或🔴清单缺明显的油价/军事/央行条目，即判定为不合格产出**。

## 双环境注意

一天一份闸按部署本地 `tr_data/build_state.json` 记账，**跨环境不互斥**。同一份简报只应在一个环境执行；多环境并行会出现同名双文档（2026-10-12 实际发生过）。
