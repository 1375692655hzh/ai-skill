# Quickstart

```bash
pip install -r requirements.txt

# 0) 先确保上游素材就绪（至少 X大V全文 + Info日报/早报其一 + 前日收评）
#    各上游 skill 的 output 目录由 config.json inputs.roots 指向

# 1) 生成日报（md + docx）
python scripts/generate_daily_report.py --config config.json
```

成功后查看：

```
output/{date}_market_daily_zh.md       # Markdown
output/turkey_daily_{yyyymmdd}.docx    # Word（QC 通过）
```

调试：

```bash
python scripts/generate_daily_report.py --config config.json --no-llm   # 只定位输入+建 prompt（看 Input bundle 日志）
python scripts/generate_daily_report.py --config config.json --force-refresh
```

cron（北京时间 16:35，TRT 11:35，全部当日素材就绪）：

```
35 16 * * 1-5  python scripts/generate_daily_report.py --config config.json
```
