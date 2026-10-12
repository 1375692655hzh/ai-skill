---
name: turkey-market-daily-report-skill
description: Aggregates upstream Turkey skill outputs (same-day Info daily analysis, X influencer full feed, AA morning briefing + previous-trading-day close report and Paraborsa broker comments) into the two-chapter《土耳其股市热点日报》- hotspot news (5-7 items, three fixed fields) + stock analysis table (4 columns, substantive-judgment-only), rendered to a QC'd Word file. Use when the user asks for 热点分析日报 / 土耳其股市热点日报 / turkey daily hotspots report.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [turkey, bist, daily-report, hotspots, docx, aggregate]
    related_skills: [turkey-info-daily-report-skill, turkey-x-influencer-report-skill, turkey-aa-morning-briefing-skill, turkey-close-report-skill, turkey-paraborsa-all-report-skill]
---

# Turkey Market Daily Report Skill（热点分析日报）

## Overview

聚合型技能：不自己抓数据，读取**上游技能的 output 产物**生成当日《土耳其股市热点日报》（Word）。

| 输入 | 日期口径 | 来源技能 |
|------|---------|---------|
| Info 每日市场分析 `{date}_info_daily_report_zh.md` | 当日 T（可回退） | turkey-info-daily-report-skill |
| X 大V全文 `{date}_x_influencer_full_zh.md` | 当日 T（可回退） | turkey-x-influencer-report-skill |
| AA 晨报 `{date}_aa_morning_briefing_zh.md` | 当日 T（可回退，可选） | turkey-aa-morning-briefing-skill |
| 早报 `{date}_daily_briefing_zh.md` | 当日 T（可回退；Info 缺失时的等效锚点） | turkey-morning-report-skill |
| 收评 `{date}_close_report_zh.md` | **严格前一交易日 P** | turkey-close-report-skill |
| Paraborsa `{date}_paraborsa_all_report_zh.md` | **严格 P** | turkey-paraborsa-all-report-skill |

**编辑规范（筛选/口径/排除项/归因/风格的唯一权威）**：`references/colleague_workflow_spec.md`（同事工作流原 spec，18 节）。生成模板 `templates/generate_template.txt` 是其执行浓缩。

## 核心规则速记（全文以 spec 为准）

1. 只有两个主章节：一、热点资讯（5–7 条 × 事件介绍/影响分析/券商观点三字段）；二、个股分析（4 列表格：股票名称/定位/投资决策/大V分析观点）
2. **扫描全部大V全部帖子，绝不按热门度筛股**；只有"实质判断"（技术位/基本面逻辑/资金筹码结构/事件影响/交易逻辑/多维估值）才入表
3. **单纯资金流入流出榜、ticker+图片、纯截图、基金持仓等 15 类排除项不得入表**；被排除的直接不写，不许写"按规则不入表"的自我说明行
4. 前一交易日 Paraborsa 只作背景（必须写"前一交易日券商观点"）；大盘点位优先级：当天 Info > 早报 > 前日 Paraborsa > 大V
5. 调查/操纵/违法类内容必须归因式表达；投资决策列用克制用词（关注/观察/等待…）
6. X 缺失 → 拒绝生成虚假个股表；Info 与早报至少其一（同事锚定顺序 info_daily→daily_briefing）

## Run Flow

1. `resolve_inputs`：按 T（TR 今天）与 P（前一交易日，周一→周五跳假日）定位 6 路输入；当日文件缺失自动回退前一日并标注
2. `build_prompt`：资料范围说明 + 七段素材（脱敏拼接，X 上限 150k 字符其余 60k）
3. LLM（MiniMax-M3）按模板生成 → `validate_output`（章节/热点条数/三字段/表格 4 列/表头/资金榜行/转载腔）
4. `make_docx`：A4、Noto Sans CJK SC、居中标题、4 列表格加粗表头跨页重复 → 程序化 QC（结构/表格/标题居中）→ 落盘

## Outputs

| 文件 | 说明 |
|------|------|
| `output/{date}_market_daily_zh.md` | 日报 Markdown |
| `output/turkey_daily_{yyyymmdd}.docx` | 最终 Word（QC 通过才交付） |
| `.cache/.../prompt_{date}.txt` | 生成 prompt（调试） |

## Schedule（推荐）

**北京 16:35（TRT 11:35）**：当日 X（15:30 生成）、AA（14:10 兜底后）、Info 就绪，前日收评/Paraborsa 隔夜已备——全部当日素材齐活。同事的 TRT 06:00 早班模式也可（北京 11:00），届时 AA 当日未出会自动回退前一日并在资料范围注明。周一/假日后首跑，回退窗口 4 天自动覆盖。

编排器契约：`scripts/resolve_target_date.py` 供 turkey-orchestrator 预解析 today_tr（skip-if-exists / run-log 锚点）；语义与 `resolve_inputs.resolve_target_date` 一致（TR 今日、不回退周末），勿替换成收评版（回退语义会查错文件名）。

## Example

```bash
pip install -r requirements.txt
python scripts/generate_daily_report.py --config config.json                 # 全流程
python scripts/generate_daily_report.py --config config.json --no-llm        # 只定位输入+建 prompt
python scripts/generate_daily_report.py --config config.json --md-only       # 跳过 docx
python scripts/generate_daily_report.py --config config.json --force-refresh # 重做当日
```

## Common Pitfalls

1. **输入没跑就跑日报** —— 核心输入缺失时 exit 2 并指出缺哪路；先跑上游
2. **上游产物在别的机器** —— config `inputs.roots` 指向各 skill 的 output 目录，跨机部署改路径或同步产物
3. **个股表出现纯资金榜行** —— 校验器 warning 提示；持续出现就在上游 X 报告源头治理
4. **Word 视觉质检** —— 脚本 QC 是结构级；交付前的渲染 PNG 逐页检查是 agent 手动步骤（用 docx 技能渲染流程），spec §15 要求"禁止在没有实际渲染检查时声称已渲染检查"
5. **Info 源不稳** —— infoyatirim 曾出现连接失败；缺失时早报为等效锚点，不阻塞

## Verification Checklist

- [ ] 资料范围段落如实标注使用文件与回退情况
- [ ] 热点 5–7 条、每条三字段齐全
- [ ] 个股表 4 列、表头正确、无纯资金榜行、无自我说明行
- [ ] Paraborsa 观点标注"前一交易日"
- [ ] docx QC passed（标题居中/表格 4 列/章节齐）
- [ ] 渲染 PNG 逐页检查通过（交付前）
