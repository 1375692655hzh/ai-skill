# turkey-zh-flash-briefing-skill

土耳其每日中文快讯简报：局域网数据站（ai-auto-gen）过去 16 小时土耳其快讯 → 逐条中文编译 + 三级重要性分级 → 本地 md 归档 + 腾讯文档智能文档上云。每日 14:30（北京时间）一份，文首含"重点消息"摘要段。

## 与其他 turkey skill 的关系

- `turkey-flash-news-skill` 面向**土耳其本地源直采**（KAP/TCMB/RSS）产出星级快讯流；本 skill 面向**局域网数据站的 aggregator 流**产出**给中国读者的人译简报**（腾讯文档交付），两者互补
- 上云依赖 mcporter(tencent-docs)；推送飞书/企微可接 `../turkey-orchestrator/`

## 全自动模式

`scripts/generate_briefing.py` 端到端：LLM 中文编译（三轮校验/修复回路，校验含条数/空标题/key 真实子串/match leftover）→ 摘要 → 上云 → 打印验收单。可接 `../turkey-orchestrator/` 统一调度（任务名 `zh_flash_briefing`）。详见 `WORKFLOW.md`。

## 亮点：三道硬闸

参数错就拒绝执行，不产出烂文档（全部来自真实事故，详见 `references/pipeline-and-gates.md`）：

1. **窗口闸**——旧 `--since` 直接拒绝（杜绝旧闻套新标题）
2. **新鲜度闸**——数据站停更时报告证据而非产出空文档
3. **一天一份闸**——重复 cron 投递直接回已有链接（build_state.json）

## 目录

见 `SKILL.md` 的 Files 一节。部署：复制本目录即可；`config.json` 的 api_key 留空走环境变量 `TURKEY_STATION_API_KEY`（勿提交真实 key）。

## 快速上手

见 `QUICKSTART.md`。编译规范与分级标准见 `references/zh-translation-spec.md`。
