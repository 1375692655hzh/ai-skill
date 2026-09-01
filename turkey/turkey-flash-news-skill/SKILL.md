---
name: turkey-flash-news-skill
description: Ingests Turkey flash sources every 30 minutes (KAP official disclosures, TCMB press + FX fixing, CNBC-e/Foreks/Dünya/Daily Sabah/Sabah RSS, BHT breaking headlines) into a local store, then generates a Chinese flash-news digest over a cursor-based 24h window - unified importance-ranked item stream (star + brief + detail per item) with LLM selection, wire-report enrichment, and a grounded event calendar. Use when the user asks for 土耳其快讯, flash digest, KAP 公告汇总, or daily Turkey event stream.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [turkey, bist, kap, tcmb, flash-news, digest, cron]
    related_skills: [turkey-morning-report-skill, turkey-close-report-skill]
---

# Turkey Flash News Skill

## Overview

两段式土耳其快讯技能（2026-08-31 三方调研定型 + 09-01 产品迭代定型）：

1. **采集层（ingest，无 LLM、零 key）**：每 30 分钟把 9 路源追加进本地 JSONL 库（id 去重幂等）
2. **生成层（digest，LLM）**：游标窗口 = 取「上次成功−5min」与「now−24h」的**更早者**为起点（周日跳过自动放宽；停机超 72h 截断防旧闻重放），排除已交付 id → 规则预筛 → 电讯回填 → 事件日历注入 → LLM 产出「星级快讯流」

**成品形态（v1.1 定稿）**：一条按重要性降序的快讯流，每条 = 完整显示单元：
```
【★★★】一句简介（含关键数字）。
详情 1–3 句（发生了什么、关键数字、背景/影响，来自 RSS 摘要与电讯补充）。
```
8–12 个单元，例行 KAP 公告合并为最后一条低星级单元；文末【明日关注】（基于事件日历卡）+ 风险提示。另产推送简报版（五字段）。

设计依据（实测）：KAP 财报法定收盘后发（TRT 18–24 时洪峰）；早峰 35% 是熔断/系统通知噪声（预筛直接丢）；周末 ≤4%；Dünya RSS 缓冲仅 3.4h → 必须高频采集；BHT RSS 已死走页面解析。

## Recommended Schedule

| 任务 | 北京时间 | 说明 |
|------|---------|------|
| **采集 cron** | **每 30 分钟全天** | `--ingest-only`，零成本；Dünya 缓冲 3.4h，最大间隔别超 3h |
| **快讯主跑** | **08:31**（与整半点采集错峰） | 吃完 TRT 18:00–24:00 财报洪峰 + 隔夜美盘 |
| 主跑重试 | 08:50、09:10 | 生成失败时（失败不清检查点，自动重收同窗口） |
| 周日 | 跳过 digest（采集照跑） | `skip_sunday: true`，周一窗口自动放宽 |
| CPI 日加跑（可选） | 15:20 | `--force-window-hours 0.6` 短窗 |
| PPK 日加跑（可选） | 19:15 | 同上 |

**不要跑的点**：14:30（已有早报）、00:10（已有收盘报告）、TRT 18:00–18:45（KAP 洪峰刚开始）。

## Data Sources（9 路，全部实测在线）

| 层 | 源 | 内容形态 |
|----|------|---------|
| 一手 | **KAP** byCriteria API | 类别标签+副标题（无正文）；主源 |
| 一手 | **TCMB 新闻稿** Atom | 标题 |
| 一手 | **TCMB 牌价** today.xml | 生成时取数做牌价单元 |
| 电线 | **CNBC-e / Foreks / Dünya / Daily Sabah / Sabah** RSS | 标题+摘要（~200 字符，100% 覆盖） |
| 电线 | **BHT 突发+要闻**（页面解析） | 标题 |

已裁：Hürriyet。备而未用：MKK 网关（公告正文，需注册 key + 土耳其 IP）。

## Pipeline（生成层六步）

1. **窗口**：起点 = min(last_digest−5min, now−window_hours)，上限 72h（防长期停机后旧闻重放）；`--force-refresh` 忽略 delivered 真重出当日；`--force-window-hours` 严格短窗 [now−h, now] 且输出独立 `_event` 文件名（不覆盖主产出）
2. **预筛**：KAP 噪声拦截（熔断/信息表/券商权证/周报/做市/BISTECH，实测日丢 ~235/392）→ 分层 P0★49/P2；KAP 封顶 120（P0 全保+P2 留新），RSS 每源封顶 40
3. **电讯回填**：ticker 边界/完整公司名匹配，把 RSS 电讯数字补进 KAP 条目（【电讯补充】）；纯标签条目标记 label-only
4. **日历注入**：config `calendar` 中今天±3 天的条目进「事件日历卡」，【明日关注】只准基于日历卡与素材明确日程
5. **LLM**：模板产出星级快讯流；结构错自动重写一次；署名/星标问题重写后仍存则带警告放行（不废稿）
6. **落盘+检查点**：确定性排版（条目间空行）→ 写完整版+简报版 → 窗口内全部条目标 delivered、游标前移

## Outputs

| 文件 | 说明 |
|------|------|
| `output/{date}_flash_news_zh.md` | 星级快讯流（900–2000 字）+【明日关注】+ 风险提示 |
| `output/{date}_flash_news_brief_zh.md` | 推送简报版（【头条】【公告】【数据】【关注】【风险】五字段，200–400 字） |
| `.cache/.../store/store.jsonl` | 全量素材库（append-only） |
| `.cache/.../store/state.json` | 游标/已交付/各源健康 |
| `.cache/.../prompt_{date}.txt` | 生成 prompt（调试） |

## Configuration

| Key | Purpose |
|-----|---------|
| `window_hours` | 窗口下限（默认 24；游标更早自动放宽） |
| `skip_sunday` | 周日跳过 digest |
| `min_items_for_digest` | 素材低于此数不生成 |
| `sources.*` | 9 路源开关；RSS 池加源即加条目 |
| `prefilter.*` | 噪声模式/封顶 |
| `calendar` | 事件日历 `[{date, event}]`，已录 2026 CPI/PPK 共 5 条，手动维护 |
| `llm` / `brief` | MiniMax-M3；`.env` 自动加载（`MINIMAX_API_KEY`） |

## Common Pitfalls

1. **采集间隔别超 3h**（Dünya 滚动缓冲）
2. **不要绕过 store 直连 RSS 生成**
3. **检查点只在成功后前移**；失败重试自动重收同窗口
4. **KAP 洪峰在 TRT 18–24 时**，主跑别放傍晚
5. **牌价日期可能是上一交易日**（非交易日不更新，非故障）
6. `--force-refresh` 现在会忽略已交付集合（真重出当日）

## Verification Checklist

- [ ] `--ingest-only` 两次连跑，第二次 `+0 new`
- [ ] `--no-llm` 产出三张素材卡 + 事件日历卡
- [ ] prompt 中熔断/信息表/权证计数为 0
- [ ] 成品：每单元星级+简介+详情、**降序（校验器硬查）**、例行公告合并为末条、无署名
- [ ] 成功后 `state.json` 游标前移；周日跳过后周一窗口 ≥48h
- [ ] `--force-refresh` 重出当日成功
