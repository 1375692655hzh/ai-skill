# Quickstart

```bash
pip install -r requirements.txt

# 1) 先采集一轮（零 key）
python scripts/generate_flash_news.py --config config.json --ingest-only

# 2) 再生成快讯（需要 MINIMAX_API_KEY，可放本目录 .env）
export MINIMAX_API_KEY="sk-..."
python scripts/generate_flash_news.py --config config.json
```

成功后查看：

```
output/{date}_flash_news_zh.md          # 星级快讯流完整版
output/{date}_flash_news_brief_zh.md    # 推送简报版
```

## 自动化 cron（北京时间）

**用绝对 Python 路径**（本机裸 `python` 可能指向无第三方包的运行时）；主跑定在 08:31 与整半点采集错峰；重试两条必配：

```
# 采集：每 30 分钟（分钟 01/31，避开主跑）
1,31 * * * *  C:/Python311/python.exe E:/ai-skill/turkey/turkey-flash-news-skill/scripts/generate_flash_news.py --config E:/ai-skill/turkey/turkey-flash-news-skill/config.json --ingest-only

# 快讯主跑 + 失败重试
31  8 * * *  .../python.exe .../generate_flash_news.py --config .../config.json
51  8 * * *  .../python.exe .../generate_flash_news.py --config .../config.json
11  9 * * *  .../python.exe .../generate_flash_news.py --config .../config.json
```

事件日加跑（可选，独立文件名不覆盖主产出）：

```
20 15 * * 3  .../python.exe .../generate_flash_news.py --config .../config.json --force-window-hours 0.6   # CPI 日（每月3日 10:00 TRT）
15 19 * * 4  .../python.exe .../generate_flash_news.py --config .../config.json --force-window-hours 0.6   # PPK 日（14:00 TRT，见 config calendar）
```

管线自检（不调 LLM）：`python scripts/generate_flash_news.py --config config.json --no-llm --force-refresh`
源健康：`.cache/turkey-flash-news/store/state.ingest.json` 的 `fail_streaks`（连续失败 ≥6 次日志会告警）。
