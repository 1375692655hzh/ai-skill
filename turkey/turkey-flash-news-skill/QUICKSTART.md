# Quickstart

```bash
pip install -r requirements.txt

# 1) 先采集一轮（零 key）
python scripts/generate_flash_news.py --config config.json --ingest-only

# 2) 再生成快讯（需要 MINIMAX_API_KEY）
export MINIMAX_API_KEY="sk-..."
python scripts/generate_flash_news.py --config config.json
```

成功后查看：

```
output/{date}_flash_news_zh.md          # 完整版
output/{date}_flash_news_brief_zh.md    # 推送简报版
```

自动化两条 cron（北京时间）：

```
*/30 * * * *  采集   ... generate_flash_news.py --config config.json --ingest-only
30  8   * * *  主跑   ... generate_flash_news.py --config config.json
```

管线自检（不调 LLM）：`python scripts/generate_flash_news.py --config config.json --no-llm --force-refresh`
