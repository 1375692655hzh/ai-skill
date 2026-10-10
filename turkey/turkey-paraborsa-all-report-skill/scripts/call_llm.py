#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic OpenAI-compatible LLM caller.

Uses ``curl`` via subprocess for the actual HTTP call.  Experience showed that
``requests`` from this sandbox gets intermittent 401 / DNS errors from
api.minimaxi.com, while ``curl`` succeeds with the same key and payload.  Curl
also handles the local HTTP proxy more reliably.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def _resolve_api_key(api_key_or_env: str) -> str:
    if not api_key_or_env:
        return ""
    match = re.match(r"^\$\{(.+)\}$", api_key_or_env)
    if match:
        env_name = match.group(1)
        value = (os.environ.get(env_name) or "").strip()
        if not value:
            raise RuntimeError(f"Environment variable {env_name} is not set.")
        return value
    # Bare env var name (e.g. "MINIMAX_API_KEY") — look up before treating as literal key
    env_value = os.environ.get(api_key_or_env)
    if env_value:
        return env_value.strip()
    return api_key_or_env.strip()


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
        "minimax": "https://api.minimaxi.com/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    }
    return defaults.get(provider, "https://api.openai.com/v1")


def _which_curl() -> str:
    for candidate in ("curl", "C:\\Windows\\System32\\curl.exe"):
        try:
            subprocess.run([candidate, "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return candidate
        except Exception:
            continue
    raise RuntimeError("curl executable not found; required for LLM calls.")


def call_llm(
    prompt: str,
    provider: str,
    model: str,
    api_key_env: str,
    base_url: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 12000,
    system_message: str = "You are a Turkish market analyst writing a Chinese broker commentary synthesis report.",
) -> str:
    api_key = _resolve_api_key(api_key_env)
    if not api_key:
        raise RuntimeError("API key is not configured.")

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
    if provider == "minimax":
        payload["thinking"] = {"type": "disabled"}

    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    curl = _which_curl()
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
        f.write(payload_bytes)
        payload_path = f.name

    # Retry with exponential backoff: the curl swap (2026-10-10) restored
    # sandbox reliability but silently dropped the 3-attempt retry the
    # requests version had — one transient 5xx/timeout killed the report.
    import time as _time

    result = None
    last_err = ""
    try:
        cmd = [
            curl,
            "-sS",  # silent but show errors
            "-L",
            "-X", "POST",
            url,
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {api_key}",
            "--data-binary", f"@{payload_path}",
            "-m", "180",
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        ]
        # Honour explicit proxy settings if present, but do not force one.
        env = os.environ.copy()
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=200,
                    env=env,
                )
                if result.returncode == 0:
                    break
                last_err = result.stderr.decode("utf-8", errors="ignore")[:300]
            except subprocess.TimeoutExpired as exc:
                last_err = f"timeout after {exc.timeout}s"
                result = None
            if attempt < 2:
                _time.sleep(2 ** attempt * 2)
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    if result is None or result.returncode != 0:
        detail = last_err or (result.stderr.decode("utf-8", errors="ignore")[:500] if result else "no response")
        raise RuntimeError(f"curl LLM request failed after retries: {detail}")

    raw = result.stdout.decode("utf-8", errors="ignore")
    if not raw.strip():
        raise RuntimeError("curl LLM request returned empty body")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"curl LLM returned non-JSON: {raw[:500]}... ({exc})")

    if data.get("error"):
        raise RuntimeError(f"LLM API error: {data['error']}")
    if data.get("choices"):
        return data["choices"][0].get("message", {}).get("content", "")
    raise RuntimeError(f"Unexpected LLM response: {raw[:500]}")
