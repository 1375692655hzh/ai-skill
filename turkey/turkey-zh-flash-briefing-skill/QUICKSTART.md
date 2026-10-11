# Quickstart

## 前置（一次性）

1. 局域网数据站可达（默认 `http://192.168.110.214:8787`，ai-auto-gen 供数服务）
2. 设数据站 API key（二选一，**不要把 key 提交进仓库**）：

```bash
export TURKEY_STATION_API_KEY="ak_live_..."     # 推荐：环境变量
# 或编辑 config.json 的 "api_key" 字段
```

3. 腾讯文档上云：本机已配置 mcporter + tencent-docs skill 授权（见 `references/tencent-docs-upload.md`）

## 手动跑一轮

```bash
# 1) 抓取过去16小时快讯（输出待翻译清单 + zh 模板）
python scripts/tr_daily.py --config config.json fetch --hours 16

# 2) agent 逐条填写 tr_data/<tag>_zh.json（分级+中文标题，规范见 references/zh-translation-spec.md）
#    并写 tr_data/<tag>_summary.txt（"重点消息包括：A、B、C"）

# 3) 先干跑自检（不上云）
python scripts/tr_daily.py --config config.json build --hours 16 --dry-run

# 4) 正式上云（本地 md 存 output/，腾讯文档链接打印在最后）
python scripts/tr_daily.py --config config.json build --hours 16
```

三道硬闸任一触发会以 `!! 拒绝：...` 退出，照提示处理；勿用 `--force` 绕过（除非用户明确要求）。

## 自动化 cron（北京时间 14:30）

每天**只跑一次**。本 skill 设计为 agent 技能：cron 投递 SKILL.md 中"每日七步"任务（含编译步骤），由 agent 执行；纯脚本只能覆盖抓取与上云，编译必须有人/LLM 参与。

若接 `../turkey-orchestrator/`，在 `automation_config.json` 的 `tasks` 中仿照其他条目新增：

```json
"zh_flash_briefing": {
  "enabled": true,
  "title_prefix": "土耳其财经快讯简报",
  "skill_dir": "<部署路径>/turkey-zh-flash-briefing-skill",
  "command": ["python", "scripts/tr_daily.py", "--config", "config.json", "build", "--hours", "16"],
  "run_cwd": "<部署路径>/turkey-zh-flash-briefing-skill",
  "output_dir": "output",
  "full_pattern": "土耳其快讯_{date}_中文简报*.md",
  "skip_if_exists": true,
  "skip_keywords": ["拒绝", "0 条", "停更"],
  "max_retries": 1
}
```

（注意：编译步骤需在 command 之前由 agent 完成 zh 文件；纯编排器模式下请配合 LLM 步骤使用。）

## 故障速查

| 现象 | 处置 |
|------|------|
| `!! 拒绝：窗口起点 … 疑似旧数据` | 不要手传 --since；用 `--hours 16` 自算 |
| `!! 拒绝：… 数据疑似过期/停更` | `curl -H "Authorization: Bearer $KEY" <station>/../status` 查刷新轮次，向用户报告证据 |
| `!! 拒绝：今天已发布过一版` | 重复投递——回复已有链接即可 |
| `!! N 条未匹配翻译` | 按 leftover 提示修 zh 分片，`merge_zh_parts.py` 重跑 |
| 上传分块 405 | `python scripts/retry_append.py <file_id> <本地md>` |
| get_content 条数偏少 | 已知截断，以 build 输出条数 + 关键词抽查为准 |
