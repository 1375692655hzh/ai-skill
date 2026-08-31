---
name: turkey-x-influencer-report-skill
description: Fetches X/Twitter influencer posts from TR yesterday 00:00 to now via keyless FxTwitter v2 (primary) with xAI x_search fallback after BIST open, writes original+Chinese translation full file and an LLM-curated investment digest with background and analysis. Use when the user asks for Twitter/X big-V views, influencer market monitor, or post-open social sentiment for Turkey.
version: 1.2.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [turkey, bist, twitter, x, influencers, xai, translation, curated-digest]
    related_skills: [turkey-morning-report-skill, turkey-info-daily-report-skill]
---

# Turkey X Influencer Report Skill

## Overview

Standalone skill (independent of Turkey-investment / reference Hermes monitor) that:

1. Runs on **Turkish trading days** only (weekend + fixed national holidays skipped)
2. After market open, fetches watchlist posts via **FxTwitter v2**（免 key 主源；失败账号回落 **xAI Responses API `x_search`**）
3. Window: **Europe/Istanbul yesterday 00:00 → now** (paginated; soft caps apply)
4. Writes **full file**: original text verbatim + Chinese translation (+ media URLs)
5. Writes **curated file**: LLM selects valuable investment hotspots, with background + investment analysis (Turkey/BIST/TRY first)

## 开箱即用

复制整个 `turkey-x-influencer-report-skill` 目录即可。

| 必需 | 说明 |
|------|------|
| Python 3.9+ | `pip install -r requirements.txt` |
| `XAI_API_KEY` | **降级源用（可选）**：FxTwitter 主源免 key；需 xai 兜底时填 OpenRouter 的 `sk-or-v1-...`（https://openrouter.ai/keys） |
| LLM API Key | 默认 `MINIMAX_API_KEY`（翻译 + 精选） |
| accounts.yaml | 从 `accounts.example.yaml` 复制；可增删 |

```bash
cd turkey-x-influencer-report-skill
pip install -r requirements.txt
copy accounts.example.yaml accounts.yaml
copy .env.example .env
# 编辑 .env 填入 XAI_API_KEY（OpenRouter）与 MINIMAX_API_KEY
python scripts/generate_x_influencer_report.py --config config.json
```

## Recommended Schedule（开盘后）

BIST 开盘 TR 10:00 = 北京 **15:00**。

| 项目 | 值 |
|------|-----|
| **Cron 首发 (北京)** | **15:30** |
| 重试 | 15:50、16:10 |
| 周末 / 固定假日 | 跳过 |
| 抓取窗口 | **TR 昨天 00:00 → 现在** |

```
15:30, 15:50, 16:10  → 交易日
```

## Outputs

| 文件 | 说明 |
|------|------|
| `output/{date}_x_influencer_full_zh.md` | 全量原文 + 译文 |
| `output/{date}_x_influencer_curated_zh.md` | 精选热点 + 背景 + 投资分析 |
| `.cache/.../posts_{date}.json` | 抓取缓存 |

## Run Flow

1. Resolve TR trading day (fixed holidays auto-generated; Jun+ includes next year)
2. Load accounts → per handle call FxTwitter `/2/profile/<handle>/statuses`（`count`/`since`/`with_replies`/`groupthreads`，`cursor.bottom` 分页；`since` 仅作 204 快路径，窗口在本地过滤）
3. 失败账号回落 xAI `x_search` (`from:handle since:YYYY-MM-DD`)，按时间切片分页；stop at `max_pages` / `per_account_limit`
4. Keep posts in window (TR yesterday 00:00 → now)
5. Translate each post (finance terms preserved)
6. Write full report
7. LLM curated digest → write curated report

## Configuration

| Key | Purpose |
|-----|---------|
| `accounts_path` | Watchlist YAML (relative or absolute) |
| `fetch.provider` | `fxtwitter`（默认，免 key）或 `xai` |
| `fetch.fallback_provider` | 主源失败账号的兜底 provider（如 `xai`；无 key 自动跳过） |
| `fetch.fxtwitter_base_url` / `_count` / `_with_replies` / `_groupthreads` | FxTwitter v2 端点与翻页参数 |
| `fetch.window` | `yesterday_start_to_now` |
| `fetch.timezone` | Default `Europe/Istanbul` |
| `fetch.xai_model` | Default `grok-4-1-fast-non-reasoning` |
| `fetch.api_key_env` | Default `XAI_API_KEY` |
| `fetch.per_account_limit` | Max posts kept per account (default 50) |
| `fetch.max_pages_per_account` | Max x_search pages per account (default 5) |
| `fetch.account_delay_seconds` | Pause between accounts (default 1) |
| `fetch.tiers` | e.g. `["core"]` to limit cost |
| `fetch.min_priority` | `high` / `medium` / `low` |
| `translate.enabled` | Per-post Chinese translation |
| `curated.enabled` | LLM curated digest |
| `llm` | Provider / model / API key env |
| `holidays` | Optional extra closed dates |

## 账号清单（可拓展 / 可删减）

编辑 `accounts.yaml`（或 `accounts.example.yaml` 复制后的文件）即可，**无需改代码**：

| 操作 | 做法 |
|------|------|
| 新增 | 在 `accounts:` 下追加 `{handle, name?, homepage?, priority?, notes?, enabled: true}` |
| 删除 | 删掉该账号整块 YAML |
| 临时关闭 | 设 `enabled: false` |
| 换文件 | `config.json` → `accounts_path` 指向其他 yaml |

解析规则：只抓 `enabled: true`（缺省视为 true）且有非空 `handle` 的条目。

## Auth

Load order for env vars (`.env`):

1. `~/.hermes/x-influencer-market-monitor/.env`
2. Skill directory `.env`

Fetch 默认走 FxTwitter（免 key）。仅当配置 `fetch.fallback_provider: "xai"` 且希望兜底生效时需要 `XAI_API_KEY` = **OpenRouter** `sk-or-v1-...`；无该 key 时自动跳过兜底，只记录失败账号。 Required for translate/curated: `MINIMAX_API_KEY`. Do not commit `.env`.

**No longer required:** Twitter cookies / twitter-cli / opencli / kovar keys.

## Example

```bash
python scripts/generate_x_influencer_report.py --config config.json
python scripts/generate_x_influencer_report.py --config config.json --force-refresh
python scripts/generate_x_influencer_report.py --config config.json --no-curated
python scripts/generate_x_influencer_report.py --config config.json --no-translate --no-curated
```

## Diff vs reference `x-influencer-market-monitor`

- Two outputs (full + curated analysis), not one MD
- Window: **TR yesterday 00:00 → now** via **FxTwitter v2 主源 + xAI 兜底** (not twitter-cli)
- Pagination with soft caps; may still truncate
- Turkish trading-day calendar + post-open schedule
- Self-contained pack for colleagues
