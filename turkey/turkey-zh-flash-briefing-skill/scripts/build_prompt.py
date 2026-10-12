# -*- coding: utf-8 -*-
"""构建中文编译 prompt：把 <tag>_items.json 交给 LLM，产出五列 zh JSON。

校验规则与 references/zh-translation-spec.md 一致：
- 每条 [time(HH:MM), source_id, key, tier(0/1/2), 中文标题]，条数必须等于输入条数
- key 只能是 "" 或该条 url+text 经 ASCII 折转写后的真实子串（严禁自造/编号/缩写）
- 中文标题不允许为空
"""
from __future__ import annotations

import json

PROMPT_TMPL = """你是土耳其财经快讯的中文编译编辑。下面是 {n} 条过去16小时的土耳其财经快讯原文（土耳其语/英语）。
请逐条翻译并分级，输出严格的 JSON 数组（不要 markdown 代码块、不要任何解释文字）。

输出格式：恰好 {n} 个元素的数组，每个元素5列：
["HH:MM", "source_id", "key", tier, "中文标题"]

硬性规则：
1. 每条输入都必须有对应输出；time 与 source_id 逐字照抄输入，顺序可以乱，但 total 必须 = {n}。
2. tier 分级（重要性=对国际或土耳其股市的影响）：
   0=🔴高：宏观数据(CPI/PPI/PMI/就业)、央行/财政/货币政策表态、地缘与油价直接冲击(霍尔木兹/油轮/军事行动)、市场监管重拳(卖空限制/停牌/接管/大额罚单)
   1=🟠中：BIST个股与券商研判、资金与制度动向、重要全球宏观、大型并购/IPO、银行声明
   2=⚪低：公司披露、软新闻、娱乐体育、噪音
   事件簇只给主条目高分级，后续/转述降一档，第三条起给2。油价警报、军事袭击、央行信号、市场接管属于0级，禁止降级。
3. 中文标题：一句话说清"谁/发生了什么/关键数字"，面向中国读者；数字保留原文精度；禁止空标题。
4. key 规则：仅当同一 HH:MM 且同一 source_id 有多条时才用 key 区分，key 必须逐字符取自该条 text 或 url 的小写 ASCII 折转写（ı/İ→i、ç→c、ğ→g、ö→o、ş→s、ü→u）后的真实子串；其余一律 ""。
5. 输出示例（示意，非数据）：
[["14:02","cnbce_rss","",1,"示例标题甲"],["13:58","foreks_rss","",0,"示例标题乙"]]

待编译条目（共 {n} 条）：
{items_json}
"""


def build_prompt(items: list) -> str:
    slim = [
        {
            "time": it["time"][11:16],
            "source_id": it["source_id"],
            "text": it["text"],
            "url": it["url"],
        }
        for it in sorted(items, key=lambda x: x["time"], reverse=True)
    ]
    return PROMPT_TMPL.format(n=len(items), items_json=json.dumps(slim, ensure_ascii=False))


SUMMARY_PROMPT_TMPL = """以下是今日土耳其财经快讯的高重要性（🔴）中文标题：
{titles}

请从中挑出对国际或土耳其股市最重要的 3-6 条，输出一行中文，格式严格为：
重点消息包括：A、B、C
（顿号分隔，条目文字可直接沿用原标题或轻微压缩，不要输出其他任何内容。）"""
