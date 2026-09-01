# Turkey Flash News Skill

土耳其市场快讯：采集层（KAP / TCMB / BHT / RSS × 3，零 key，每 30 分钟）+ 生成层（游标 24h 窗口、规则预筛、LLM 中文快讯，完整版 + 推送简报版）。

- 用法见 `QUICKSTART.md`，完整说明见 `SKILL.md`
- 数据源端点契约见 `references/data_sources.md`
- 设计依据：2026-08-31 发布时间调研（KAP 财报法定收盘后发、TRT 18–24 时洪峰、Dünya 浅缓冲 3.4h、BHT RSS 已死）
