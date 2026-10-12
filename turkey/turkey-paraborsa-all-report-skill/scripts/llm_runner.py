#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared LLM generation with validation and attribution retry."""
from __future__ import annotations

from typing import Callable

from call_llm import call_llm


def generate_with_validation(
    prompt: str,
    llm_cfg: dict,
    validate_fn: Callable[[str], dict],
    *,
    fix_attribution: bool = True,
) -> tuple[str | None, dict]:
    api_key_env = llm_cfg.get("api_key") or llm_cfg.get("api_key_env", "OPENAI_API_KEY")
    provider = llm_cfg.get("provider", "openai")
    model = llm_cfg.get("model", "gpt-4o")
    base_url = llm_cfg.get("base_url")
    temperature = llm_cfg.get("temperature", 0.4)
    max_tokens = llm_cfg.get("max_tokens", 2500)

    try:
        output = call_llm(
            prompt=prompt,
            provider=provider,
            model=model,
            api_key_env=api_key_env,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return None, {"ok": False, "errors": [str(exc)], "warnings": []}

    validation = validate_fn(output)
    if not validation["ok"] and fix_attribution and validation.get("attribution_hits"):
        fix_prompt = (
            prompt
            + "\n\n【重要修订】你上一版草稿仍出现了来源署名。"
            "请完全重写：正文不得出现任何机构/平台/网站/报告名称。"
        )
        try:
            output = call_llm(
                prompt=fix_prompt,
                provider=provider,
                model=model,
                api_key_env=api_key_env,
                base_url=base_url,
                temperature=max(0.2, temperature - 0.1),
                max_tokens=max_tokens,
            )
            validation = validate_fn(output)
        except Exception as exc:
            return None, {"ok": False, "errors": [str(exc)], "warnings": []}

    length_errors = [e for e in validation.get("errors", []) if "too short" in e or "too long" in e]
    structure_errors = [
        e
        for e in validation.get("errors", [])
        if "券商观点速览" in e or "structured slot" in e or "per-broker wall" in e
    ]
    turkish_errors = [
        e for e in validation.get("errors", []) if e.startswith("Turkish text leaked")
    ]
    if not validation["ok"] and (length_errors or structure_errors or turkish_errors):
        lang_rule = (
            "全篇必须使用简体中文——素材是土耳其语原文，必须翻译改写，"
            "严禁照抄原文词句；只有股票代码、券商名、通用缩写（TCMB/S&P/CPI 等）"
            "和数字可以保留原文。"
            if turkish_errors
            else ""
        )
        fix_prompt = (
            prompt
            + "\n\n【重要修订】上一版不合格："
            + "；".join(structure_errors + length_errors + turkish_errors)
            + "。请重写【综合总结】："
            "券商观点速览必须且只能写成三行——"
            "技术位共识：……\\n宏观与事件：……\\n资金与标的：……；"
            "禁止 A1 Capital：/Destek：这类逐家罗列；完整原文不在速览里。"
            + lang_rule
        )
        try:
            output = call_llm(
                prompt=fix_prompt,
                provider=provider,
                model=model,
                api_key_env=api_key_env,
                base_url=base_url,
                temperature=max(0.2, temperature - 0.05),
                max_tokens=max_tokens,
            )
            validation = validate_fn(output)
        except Exception as exc:
            return None, {"ok": False, "errors": [str(exc)], "warnings": []}

    return output, validation
