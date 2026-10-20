#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic OpenAI-compatible LLM caller."""
from __future__ import annotations

import os
import re
from typing import Optional

import requests


def _resolve_api_key(api_key_or_env: str) -> str:
    if not api_key_or_env:
        return ""
    match = re.match(r"^\$\{(.+)\}$", api_key_or_env)
    if match:
        env_name = match.group(1)
        value = os.environ.get(env_name)
        if not value:
            raise RuntimeError(f"Environment variable {env_name} is not set.")
        return value
    # Bare env var name (e.g. "MINIMAX_API_KEY") — look up before treating as literal key
    env_value = os.environ.get(api_key_or_env)
    if env_value:
        return env_value
    return api_key_or_env


def _resolve_base_url(base_url_or_env: Optional[str], provider: str) -> str:
    if base_url_or_env:
        match = re.match(r"^\$\{(.+)\}$", base_url_or_env)
        if match:
            value = os.environ.get(match.group(1))
            if value:
                return value
            raise RuntimeError(f"Environment variable {match.group(1)} is not set.")
        return base_url_or_env
    defaults = {
        "openai": "https://api.openai.com/v1",
        "xai": "https://api.x.ai/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "minimax": "https://api.minimaxi.com/v1",
    }
    return defaults.get(provider, "https://api.openai.com/v1")


def call_llm(
    prompt: str,
    provider: str,
    model: str,
    api_key_env: str,
    base_url: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2500,
    system_message: str = "You are a Turkish market analyst writing Chinese investment notes.",
    timeout: int = 180,
) -> str:
    api_key = _resolve_api_key(api_key_env)
    if not api_key:
        raise RuntimeError("API key is not configured (checked env and config).")

    base_url = _resolve_base_url(base_url, provider)
    url = f"{base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if provider == "minimax" and "M3.1" not in model:
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "choices" in data and data["choices"]:
        message = data["choices"][0].get("message", {})
        content = message.get("content", "") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content
    raise RuntimeError(f"Unexpected LLM response: {data}")
