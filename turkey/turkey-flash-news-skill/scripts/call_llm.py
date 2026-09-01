#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic OpenAI-compatible LLM caller."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests


def call_llm(
    prompt: str,
    provider: str,
    model: str,
    api_key_env: str,
    base_url: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 2500,
) -> str:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"Environment variable {api_key_env} is not set.")

    # Provider-specific defaults
    if base_url is None:
        defaults = {
            "openai": "https://api.openai.com/v1",
            "xai": "https://api.x.ai/v1",
            "zhipu": "https://open.bigmodel.cn/api/paas/v4",
            "minimax": "https://api.minimaxi.com/v1",
        }
        base_url = defaults.get(provider, "https://api.openai.com/v1")

    url = f"{base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a Turkish market analyst writing a Chinese morning briefing."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Disable thinking for MiniMax models (they output <think> blocks that consume tokens)
    if provider == "minimax":
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Retry on transient failures: MiniMax frequently times out under load,
    # and 5xx flaps happen. Back off exponentially so a brief API hiccup
    # doesn't fail the whole report.
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=180)
            if resp.status_code >= 500 or resp.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                message = data["choices"][0].get("message", {})
                return message.get("content", "")
            raise RuntimeError(f"Unexpected LLM response: {data}")
        except (requests.Timeout, requests.ConnectionError):
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("LLM call exhausted retries without response.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: call_llm.py PROMPT_FILE")
        sys.exit(1)
    prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
    r = call_llm(
        prompt=prompt,
        provider=os.environ.get("TURKEY_MORNING_LLM_PROVIDER", "openai"),
        model=os.environ.get("TURKEY_MORNING_LLM_MODEL", "gpt-4o"),
        api_key_env=os.environ.get("TURKEY_MORNING_LLM_API_KEY_ENV", "OPENAI_API_KEY"),
        base_url=os.environ.get("TURKEY_MORNING_LLM_BASE_URL"),
    )
    print(r)
