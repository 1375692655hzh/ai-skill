---
name: turkey-zh-flash-briefing-skill
description: 每日14:30抓取局域网数据站(ai-auto-gen)过去16小时土耳其快讯(markets=土耳其, kinds=flash, dedup=1)，逐条中文编译+三级重要性分级，生成本地md归档并分块上云腾讯文档（智能文档），文首含"重点消息"摘要段。内置三道硬闸（窗口日期闸/数据新鲜度闸/一天一份闸）拒绝旧数据或重复建版。Use when the user asks for 土耳其快讯简报, 土耳其每日简报, 每日土耳其快讯, or the 14:30 cron fires.
version: 1.0.0
author: 1375692655hzh
license: MIT
metadata:
  hermes:
    tags: [turkey, bist, flash-news, daily-briefing, chinese, tencent-docs, cron]
    related_skills: [turkey-flash-news-skill, turkey-morning-report-skill]
---

# Turkey Chinese Flash Briefing Skill

## Overview

面向中文读者的**每日土耳其财经快讯简报**（产品定型于 2026-10，三闸加固版）：

1. **抓取**：局域网数据站 `GET /v1/items?markets=土耳其&kinds=flash&dedup=1`，窗口=过去 16 小时（脚本自算，勿手传旧 since）
2. **编译**：agent 逐条阅读土耳其语/英语原文 → 中文一句话标题 + 三级重要性分级（🔴宏观/央行/地缘油价冲击 🟠个股与资金动向/重要全球宏观 ⚪软新闻/披露/噪音），同级别按时间倒序
3. **上云**：本地 md 归档 + 经 mcporter(tencent-docs) 分块写入腾讯文档智能文档，文首含"重点消息包括：A、B、C"摘要段

**三道硬闸（2026-10 加固，事故驱动）**：参数错就拒绝执行，不产出烂文档——
1. **窗口闸**：`--since` 起点距今超过 1 天或为未来 → 拒绝（杜绝旧数据套今天的标题）
2. **新鲜度闸**：窗口内最新条目不是今天 → 拒绝（数据站停更/过期时报告证据而非产出空文档）
3. **一天一份闸**：`tr_data/build_state.json` 记录当日已发布版本，同日重复构建拒绝（重复 cron 投递直接回已有链接；`--force` 仅限用户当条消息明确要求）

## Recommended Schedule

| 任务 | 北京时间 | 说明 |
|------|---------|------|
| 每日主跑 | **14:30**（cron `30 14 * * *`） | 覆盖昨晚 22:30 → 今日 14:30 的完整交易日+隔夜美盘 |
| 重试 | 15:00 | 主跑失败时（脚本拒绝会自动退出，修复后重跑） |

每天**只跑一次**；同一天的重复投递由一天一份闸拦截，直接回复已有文档链接即可。

## Pipeline（每日七步）

1. **锚定**：先跑 `date` 确认今天真实日期，全程以此为锚——**禁止沿用上下文残留的旧日期或旧 since**（2026-09-21 旧闻套新标题事故的根因）
2. **抓取**：`python scripts/tr_daily.py fetch --hours 16` → `tr_data/<tag>_items.json` + `_zh.json` 模板；items=0 或新鲜度闸拒绝 → 查 `/v1/status` 向用户报告证据，不产出空文档
3. **编译**：逐条填写 `_zh.json`：`[time, source_id, key, tier, 中文标题]`，全部条目必须填完（key 规则与分级标准见 `references/zh-translation-spec.md`）
4. **摘要**：从高重要性条目挑 3-6 条写 `_summary.txt`，格式"重点消息包括：A、B、C"
5. **构建**：`python scripts/tr_daily.py build --hours 16`（自动挂接最新数据档，防时钟漂移错档）；遇"!! 拒绝"照提示处理，**禁止 --force 绕过**
6. **验证**：`mcporter call tencent-docs get_content`——三区齐全 + 原文链接条数=items 数 + 文末最旧条目关键词命中
7. **汇报**：文档链接 + 条数统计（高X/中Y/低Z）+ 摘要段；标题由脚本定为"土耳其财经快讯简报 · 产出日期"，不加窗口后缀

## Data Source

局域网数据站（ai-auto-gen 项目供数服务，默认 `http://192.168.110.214:8787`）：

```
GET /v1/items?markets=土耳其&kinds=flash&since=YYYY-MM-DD HH:MM&limit=1000&cursor=..&dedup=1
GET /v1/status    # 刷新轮次、源健康、条目总数（停更诊断用）
Header: Authorization: Bearer <api_key>
```

key 通过环境变量（`config.json` 的 `api_key_env`，默认 `TURKEY_STATION_API_KEY`）或 config 的 `api_key` 字段提供，**不要提交进公开仓库**。

## Tencent Docs 上云

依赖外部 mcporter CLI + tencent-docs skill 授权（一次性配置，详见 `references/tencent-docs-upload.md`）：
- 建档 `create_smartcanvas_by_mdx`，分块追加 `smartcanvas.edit INSERT_AFTER`（~12000 字符/块，防参数超长）
- 追加遇 HTTP 405 瞬时限流 → `python scripts/retry_append.py <file_id> <本地md路径>` 补发
- get_content 纯文本提取会截断大文档 → 完整性以 build 输出条数 + 关键词抽查为准，不能只凭它否定

## Known Pitfalls（都真实咬过人）

- **中文标题为空 / 摘要为空** → 脚本拒绝上云（会产生"、、、"式残缺摘要），补齐 zh 文件再跑
- **来源写串**（cnbce/foreks/dunya 互换）与**时间±几分钟笔误** → 逐条对账脚本报 leftover，照提示修，禁止手改 JSON 硬塞
- **成组条目漏 key**：同一时刻同一来源多条时必须用真实子串 key 区分，否则张冠李戴
- **Write 工具写大 JSON 会截断** → 分片写 + `scripts/merge_zh_parts.py` 合并校验
- **build 用当前时钟重算窗口会与 fetch 数据档错位** → 已改为自动挂接最新 `*_items.json` 档，勿绕过

## 两种生产模式

- **模式 A（全自动，推荐接入编排器）**：`scripts/generate_briefing.py` 一条命令端到端——编译由 LLM 完成（三轮校验/修复回路：条数/空标题/key 真实子串/leftover 全查，不过即失败退出，绝不降级发布）。工作流、cron 与编排器任务示例见 **`WORKFLOW.md`**。
- **模式 B（agent，质量上限最高）**：本文件"每日七步"，编译/分级由 agent 逐条人工完成。日常人工跑或质量复核日用此模式。

两种模式产出同一份验收单（条数/窗口/🔴清单/文档链接）——**条数与数据站窗口不符、或🔴缺明显的油价/军事/央行条目，即不合格产出**。

## Files

```
SKILL.md                     本文件（agent 工作流与铁律）
WORKFLOW.md                  工作流：两种模式 / cron / 编排器接入 / 验收单
config.json                  运行配置（两个 api_key 留空，走环境变量）
config.example.json          配置模板
requirements.txt             LLM 阶段需 requests；其余纯标准库
QUICKSTART.md                部署与 cron 快速上手
README.md                    概览
scripts/generate_briefing.py 端到端入口（fetch → LLM 编译 → 摘要 → build → 验收单）
scripts/tr_daily.py          管线：fetch / build（含三闸与上云）
scripts/build_prompt.py      中文编译/摘要 prompt 构造
scripts/llm_runner.py        LLM 生成 + 校验 + 带反馈修复回路
scripts/call_llm.py          通用 OpenAI 兼容调用器（与 flash-news skill 同源）
scripts/retry_append.py      上传分块 405 补发
scripts/merge_zh_parts.py    编译分片合并 + 对账匹配（leftover=0 才放行）
references/zh-translation-spec.md   分级标准 / key 规则 / 常见编译事故
references/tencent-docs-upload.md   腾讯文档上云与验证细节
references/pipeline-and-gates.md    三道硬闸的设计依据与事故清单
```
