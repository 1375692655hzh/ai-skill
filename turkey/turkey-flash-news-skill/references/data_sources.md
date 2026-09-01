# Data Sources（端点契约，2026-08-31 实测）

时区：TRT = UTC+3（无夏令时），北京 = TRT + 5。

## 一手事实层

### KAP 法定披露（主源）

- `POST https://www.kap.org.tr/tr/api/disclosure/members/byCriteria`
- body `{"fromDate":"YYYY-MM-DD","toDate":"YYYY-MM-DD","member":"","disclosureClass":""}`；必须带 `Referer: https://www.kap.org.tr/tr/`
- 旧端点 `GET /tr/api/disclosure/list` 已 404，别用
- `publishDate` 格式 `DD.MM.YYYY HH:MM`（TRT）；系统 7×24，周末也可发（量 ≤4%）
- 财报法定收盘后发：非截止日 18:00 → 次日 ~08:40；截止日 18:00 → 24:00（MKK GM1058 / SPK II-15.1）
- 日分布（7 天 1315 条实测）：18 时 225 条（全天峰）、10 时 167 条（35% 为机械噪声）、19 时后骤降

### TCMB 新闻稿 Atom

- `https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB+EN/Bottom+Menu/Other/RSS/Press+Releases`
- 坑：Content-Type 是 text/html 但 body 是合法 Atom；标题 CDATA；日期 `Aug 28, 2026, 5:23:07 PM`（TRT）
- 议息（PPK）决议固定 **14:00 TRT**，2026 剩余：9/10、10/22、12/10（多为周四）

### TCMB 每日牌价

- `https://www.tcmb.gov.tr/kurlar/today.xml`（零 key）
- 交易日 ~15:30 TRT 覆盖一期；非交易日文件不更新（Tarih 日期旧 = 正常空数据，不是故障）

## 快讯层（RSS）

| 源 | 端点 | 特征 |
|----|------|------|
| CNBC-e | `https://www.cnbce.com/rss` | ~60+/天、5 分钟级、深夜含美盘；土语 |
| Foreks | `https://www.foreks.com/rss/` | 终端级、KAP/指数变更转写快；土语 |
| Dünya | `https://www.dunya.com/rss` | **缓冲仅 ~3.4h**，必须 ≤30min 高频采集 |
| Daily Sabah | `https://www.dailysabah.com/rssfeed/business` | 英文、混旧稿需日期过滤（默认关） |
| Sabah 经济 | `https://www.sabah.com.tr/rss/ekonomi.xml` | ~10 条/天宏观日包（默认关） |
| ~~Hürriyet DN~~ | — | 已裁：旧稿混杂、时效最差 |

通用坑：pubDate RFC2822 主解 + ISO 兜底；标题 CDATA；`r.encoding = r.apparent_encoding` 防土语乱码。

## BHT 突发/要闻（HTML 解析）

- `https://www.bloomberght.com/borsa`：`SON DAKİKA` 区块取 `/sondakika` 链接、`Öne Çıkan` 区块取 `-\d+` 结尾文章链接
- **BHT RSS 已死**（2026-08-31 实测最新条目 5.7 天前），只能走页面解析
- 请求带 `Accept-Language: tr-TR,tr;q=0.9`；实现复用 `fetch_bloomberght_closing.py`

## 关键时刻表（事件日加跑用）

| 事件 | 时刻（TRT） | 北京 |
|------|------------|------|
| TÜİK CPI（每月 3 日，逢周末顺延） | 10:00 | 15:00 |
| TCMB PPK 议息决议 | 14:00 | 19:00 |
| 每日官方牌价 | ~15:30 | 20:30 |
| KAP 财报洪峰 | 18:00–24:00 | 23:00–05:00 |
