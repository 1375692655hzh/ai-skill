# -*- coding: utf-8 -*-
"""LLM 生成 + 校验 + 带反馈修复（适配中文编译五列 JSON 输出）。"""
from __future__ import annotations

import json
import re
from typing import Callable

from call_llm import call_llm


def _parse_json_array(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        raise ValueError("输出中找不到 JSON 数组")
    return json.loads(m.group(0))


def make_validator(items: list) -> Callable[[str], dict]:
    """返回校验函数：条数、空标题、tier、key 真实子串、match leftover 全查。"""
    from tr_daily import match, norm  # 延迟导入：复用主管线的折叠表与匹配器

    idx = {}
    for it in items:
        hm = it["time"][11:16]
        idx.setdefault((hm, it["source_id"]), []).append(
            norm(it["url"] + " " + it["text"])
        )

    def validate(output: str) -> dict:
        errors, warnings = [], []
        try:
            arr = _parse_json_array(output)
        except Exception as exc:
            return {"ok": False, "errors": [f"JSON 解析失败: {exc}"], "warnings": []}
        if not isinstance(arr, list):
            return {"ok": False, "errors": ["输出不是 JSON 数组"], "warnings": []}
        if len(arr) != len(items):
            errors.append(f"条数不符: 输出 {len(arr)} != 输入 {len(items)}")
        for e in arr:
            if not (isinstance(e, list) and len(e) == 5):
                errors.append(f"条目格式错误(须为5列): {str(e)[:80]}")
                continue
            t, src, key, tv, zh = e
            if not str(zh).strip():
                errors.append(f"空中文标题: {t} {src}")
            if str(tv) not in ("0", "1", "2"):
                errors.append(f"tier 非法(须0/1/2): {t} {src} -> {tv}")
            bucket = idx.get((str(t), str(src)))
            if bucket is None:
                errors.append(f"time/source_id 在输入中不存在: {t} {src}")
            elif str(key):
                tn = norm(str(key))
                if not any(tn in body for body in bucket):
                    errors.append(f"key 不是真实子串: {t} {src} key={key!r}")
        if errors:
            return {"ok": False, "errors": errors[:20], "warnings": []}

        buckets, leftover = match(items, arr)
        if leftover:
            errors.append(f"{len(leftover)} 条未匹配（time/source/key 组合对不上）:")
            for it in leftover[:10]:
                errors.append(f"  {it['time']} {it['source_id']} {it['text'][:50]}")
        if errors:
            return {"ok": False, "errors": errors[:20], "warnings": []}
        return {"ok": True, "errors": [], "warnings": [], "arr": arr, "buckets": buckets}

    return validate


def generate_translation(items: list, llm_cfg: dict, max_rounds: int = 3) -> tuple[list | None, dict]:
    """生成五列 zh 数组；失败时带校验反馈重试。返回 (arr|None, info)。"""
    from build_prompt import build_prompt

    prompt = build_prompt(items)
    validate_fn = make_validator(items)
    info = {"rounds": 0, "last_errors": []}
    for round_no in range(1, max_rounds + 1):
        info["rounds"] = round_no
        try:
            output = call_llm(
                prompt=prompt,
                provider=llm_cfg.get("provider", "openai"),
                model=llm_cfg.get("model", "gpt-4o"),
                api_key_env=llm_cfg.get("api_key") or llm_cfg.get("api_key_env", "OPENAI_API_KEY"),
                base_url=llm_cfg.get("base_url"),
                temperature=llm_cfg.get("temperature", 0.2),
                max_tokens=llm_cfg.get("max_tokens", 8000),
            )
        except Exception as exc:
            info["last_errors"] = [f"LLM 调用失败: {exc}"]
            return None, info
        v = validate_fn(output)
        if v["ok"]:
            info["last_errors"] = []
            return v["arr"], info
        info["last_errors"] = v["errors"]
        prompt = (
            build_prompt(items)
            + "\n\n【重要修订】你上一版输出存在以下问题，请严格修正后重新输出完整 JSON 数组：\n- "
            + "\n- ".join(v["errors"])
        )
    return None, info


def generate_summary(tier0_titles: list, llm_cfg: dict) -> str | None:
    """从🔴标题中挑 3-6 条生成'重点消息包括：…'；格式不合格返回 None（调用方回退自动摘要）。"""
    if not tier0_titles:
        return None
    from build_prompt import SUMMARY_PROMPT_TMPL

    prompt = SUMMARY_PROMPT_TMPL.format(titles="\n".join("- " + t for t in tier0_titles))
    try:
        out = call_llm(
            prompt=prompt,
            provider=llm_cfg.get("provider", "openai"),
            model=llm_cfg.get("model", "gpt-4o"),
            api_key_env=llm_cfg.get("api_key") or llm_cfg.get("api_key_env", "OPENAI_API_KEY"),
            base_url=llm_cfg.get("base_url"),
            temperature=0.1,
            max_tokens=500,
        )
    except Exception:
        return None
    out = out.strip()
    m = re.match(r"^重点消息包括：.+", out, flags=re.S)
    if not m or "、" not in out:
        return None
    return out.strip().splitlines()[0].strip()
