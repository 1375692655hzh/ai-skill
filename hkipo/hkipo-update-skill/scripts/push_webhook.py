#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Webhook push adapters: Bark / 企业微信群机器人 / Server酱.

All channels accept a plain-text message + optional title and POST once.
Configuration: config.push.{channel}.{key_env|webhook_env|...} from config.json.
Secrets are read from environment variables (never stored in config).
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


@dataclass
class PushResult:
    ok: bool
    channel: str
    detail: str = ""
    dry_run: bool = False


def _resolve_env(name: Optional[str]) -> str:
    if not name:
        return ""
    return os.environ.get(name) or ""


def push_bark(
    text: str,
    cfg: dict,
    *,
    title: str = "HK IPO",
) -> PushResult:
    """Bark push. cfg: { base_url, key_env, sound, group }"""
    key = _resolve_env(cfg.get("key_env"))
    if not key:
        return PushResult(False, "bark", "BARK_KEY env not set")
    base = (cfg.get("base_url") or "https://api.day.app").rstrip("/")
    url = f"{base}/{key}"
    body = {
        "title": title,
        "body": text,
        "sound": cfg.get("sound", "alert"),
        "group": cfg.get("group", "HKIPO"),
    }
    try:
        r = requests.post(url, json=body, timeout=15)
        r.raise_for_status()
        return PushResult(True, "bark", f"{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return PushResult(False, "bark", str(exc))


def push_wx_work(text: str, cfg: dict, *, title: str = "HK IPO") -> PushResult:
    """企业微信群机器人 (markdown-limited). cfg: { webhook_env, mentioned_mobile_list }"""
    webhook = _resolve_env(cfg.get("webhook_env"))
    if not webhook:
        return PushResult(False, "wx_work", "WECOM_WEBHOOK env not set")
    # wx_work supports markdown message type
    md = f"## {title}\n\n{text}" if title else text
    body = {
        "msgtype": "markdown",
        "markdown": {"content": md, "mentioned_mobile_list": cfg.get("mentioned_mobile_list") or []},
    }
    try:
        r = requests.post(webhook, json=body, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode") not in (0, None):
            return PushResult(False, "wx_work", f"errcode={data.get('errcode')} {data.get('errmsg')}")
        return PushResult(True, "wx_work", f"{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return PushResult(False, "wx_work", str(exc))


def push_serverchan(text: str, cfg: dict, *, title: str = "HK IPO") -> PushResult:
    """Server酱 Turbo. cfg: { key_env, channel }"""
    key = _resolve_env(cfg.get("key_env"))
    if not key:
        return PushResult(False, "serverchan", "SC_KEY env not set")
    url = f"https://sctapi.ftqq.com/{key}.send"
    body = {
        "title": title[:32],
        "desp": text,
        "channel": cfg.get("channel", "wechat"),
    }
    try:
        r = requests.post(url, json=body, timeout=15)
        r.raise_for_status()
        return PushResult(True, "serverchan", f"{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return PushResult(False, "serverchan", str(exc))


# ---------------------------------------------------------------------------
# Lark/Feishu 应用消息 (IM API) + 文件上传
# ---------------------------------------------------------------------------

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
FEISHU_FILE_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/files"

_feishu_token_cache: dict[str, tuple[str, float]] = {}


def _feishu_get_token(app_id: str, app_secret: str) -> str:
    cache_key = f"feishu:{app_id}"
    cached = _feishu_token_cache.get(cache_key)
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    r = requests.post(FEISHU_TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") not in (0, None):
        raise RuntimeError(f"feishu get token failed: {data}")
    token = data["tenant_access_token"]
    _feishu_token_cache[cache_key] = (token, time.time() + data.get("expire", 7200))
    return token


def _feishu_post_file(app_id: str, app_secret: str, file_path: Path, file_name: Optional[str] = None) -> str:
    token = _feishu_get_token(app_id, app_secret)
    fname = file_name or file_path.name
    ctype = "text/plain" if fname.lower().endswith((".txt", ".md")) else "application/octet-stream"
    boundary = "----WorkBuddy" + secrets.token_hex(8)
    data = file_path.read_bytes()
    body = bytearray()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_type\"\r\n\r\nstream\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_name\"\r\n\r\n{fname}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n".encode()
    body += f"Content-Type: {ctype}\r\n\r\n".encode()
    body += data
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    r = requests.post(FEISHU_FILE_UPLOAD_URL, data=bytes(body), headers=headers, timeout=60)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"feishu upload file failed: {resp}")
    return resp["data"]["file_key"]


def push_feishu_app(text: str, cfg: dict, *, title: str = "HK IPO") -> PushResult:
    """飞书应用消息：通过 IM 接口发到群 chat_id.
    cfg 支持 { app_id, app_secret, chat_id } 或 { app_id_env, app_secret_env, chat_id }。
    """
    app_id = cfg.get("app_id") or _resolve_env(cfg.get("app_id_env"))
    app_secret = cfg.get("app_secret") or _resolve_env(cfg.get("app_secret_env"))
    chat_id = cfg.get("chat_id") or ""
    if not app_id:
        return PushResult(False, "feishu_app", "FEISHU_APP_ID env not set")
    if not app_secret:
        return PushResult(False, "feishu_app", "FEISHU_APP_SECRET env not set")
    if not chat_id:
        return PushResult(False, "feishu_app", "chat_id not set")
    try:
        token = _feishu_get_token(app_id, app_secret)
        content = json.dumps({"text": text}, ensure_ascii=False)
        body = {"receive_id": chat_id, "msg_type": "text", "content": content}
        r = requests.post(FEISHU_MSG_URL, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0, None):
            return PushResult(False, "feishu_app", f"code={data.get('code')} {data.get('msg')}")
        return PushResult(True, "feishu_app", f"{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return PushResult(False, "feishu_app", str(exc))


def push_feishu_app_file(file_path: Path, cfg: dict, *, file_name: Optional[str] = None) -> PushResult:
    """上传文件到飞书并发送文件消息到群 chat_id.
    cfg 支持 { app_id, app_secret, chat_id } 或 { app_id_env, app_secret_env, chat_id }。
    """
    app_id = cfg.get("app_id") or _resolve_env(cfg.get("app_id_env"))
    app_secret = cfg.get("app_secret") or _resolve_env(cfg.get("app_secret_env"))
    chat_id = cfg.get("chat_id") or ""
    if not app_id:
        return PushResult(False, "feishu_app", "FEISHU_APP_ID env not set")
    if not app_secret:
        return PushResult(False, "feishu_app", "FEISHU_APP_SECRET env not set")
    if not chat_id:
        return PushResult(False, "feishu_app", "chat_id not set")
    try:
        token = _feishu_get_token(app_id, app_secret)
        file_key = _feishu_post_file(app_id, app_secret, file_path, file_name)
        content = json.dumps({"file_key": file_key}, ensure_ascii=False)
        body = {"receive_id": chat_id, "msg_type": "file", "content": content}
        r = requests.post(FEISHU_MSG_URL, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0, None):
            return PushResult(False, "feishu_app", f"code={data.get('code')} {data.get('msg')}")
        return PushResult(True, "feishu_app", f"{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return PushResult(False, "feishu_app", str(exc))


PUSHERS = {
    "bark": push_bark,
    "wx_work": push_wx_work,
    "serverchan": push_serverchan,
    "feishu_app": push_feishu_app,
}


def push(
    text: str,
    push_cfg: dict,
    *,
    title: str = "HK IPO",
    log_path: Optional[Path] = None,
) -> PushResult:
    """Dispatch to the configured channel; honor dry_run."""
    channel = push_cfg.get("channel", "bark")
    dry_run = bool(push_cfg.get("dry_run", False))
    if dry_run:
        msg = f"[dry_run] {channel}\n--- title={title} ---\n{text}"
        print(msg)
        _log(log_path, msg)
        return PushResult(True, channel, "dry_run", dry_run=True)

    pusher = PUSHERS.get(channel)
    if not pusher:
        msg = f"unknown push channel: {channel}"
        print(msg, file=sys.stderr)
        _log(log_path, msg)
        return PushResult(False, channel, msg)

    res = pusher(text, push_cfg.get(channel) or {}, title=title)
    _log(log_path, f"[{res.channel}] ok={res.ok} {res.detail}\n---\n{text}")
    if not res.ok:
        print(f"[push FAIL {res.channel}] {res.detail}", file=sys.stderr)
    else:
        print(f"[push OK {res.channel}] {res.detail}")
    return res


def _log(log_path: Optional[Path], text: str) -> None:
    if not log_path:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"\n==== {datetime.now().isoformat(timespec='seconds')} ====\n")
            f.write(text)
            f.write("\n")
    except OSError:
        pass
