#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群消息推送模块（多渠道）。

支持渠道：
  企业微信 — webhook(群机器人) / appchat(应用消息)
  飞书     — webhook(自定义群机器人)

特性：结构化日志、失败自动重试与退避、text/markdown 消息、超长截断。
仅依赖 Python 标准库，无需额外安装。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# 配置 & 日志
# ---------------------------------------------------------------------------

def load_config(config_path) -> dict:
    return json.loads(Path(config_path).read_text(encoding="utf-8"))


def setup_logging(log_dir, level: str = "INFO") -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wecom_push")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        fh = logging.FileHandler(log_dir / "push.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# HTTP 工具（标准库，无第三方依赖）
# ---------------------------------------------------------------------------

def _post_json(url: str, body: dict, timeout: int = 10, headers: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {exc.read().decode('utf-8', 'ignore')[:300]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


def _get_json(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {exc.read().decode('utf-8', 'ignore')[:300]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


# ---------------------------------------------------------------------------
# 企业微信 API
# ---------------------------------------------------------------------------

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={cid}&corpsecret={cs}"
APPCHAT_URL = "https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"

# 飞书自定义群机器人 webhook
FEISHU_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/{token}"
# 飞书应用消息（IM 发消息到 chat_id）
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"

# 简单的 access_token 内存缓存，避免每次推送都换取
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def fit_text(content: str, total_max: int = 2048) -> str:
    """截断文本以适配企业微信 text 消息字节上限（官方上限 2048 字节，含截断提示）。"""
    suffix = "\n...（内容过长已截断，完整版见早报文件）"
    encoded = content.encode("utf-8")
    if len(encoded) <= total_max:
        return content
    budget = total_max - len(suffix.encode("utf-8"))
    if budget < 100:
        budget = 100
    out = encoded[:budget].decode("utf-8", errors="ignore")
    # 回退到最后一个换行，避免截断在句子中间
    if "\n" in out:
        out = out.rsplit("\n", 1)[0]
    return out + suffix


def _build_body(msg_type: str, content: str) -> dict:
    if msg_type == "markdown":
        return {"msgtype": "markdown", "markdown": {"content": content}}
    return {"msgtype": "text", "text": {"content": content}}


def send_webhook(webhook_key: str, content: str, msg_type: str = "text") -> dict:
    url = WEBHOOK_URL.format(key=urllib.parse.quote(webhook_key, safe=""))
    return _post_json(url, _build_body(msg_type, content))


def get_access_token(corp_id: str, corp_secret: str) -> str:
    cache_key = corp_id
    cached = _TOKEN_CACHE.get(cache_key)
    # 提前 5 分钟过期
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    url = TOKEN_URL.format(
        cid=urllib.parse.quote(corp_id, safe=""),
        cs=urllib.parse.quote(corp_secret, safe=""),
    )
    resp = _get_json(url)
    if resp.get("errcode") not in (0, None):
        raise RuntimeError(f"gettoken 失败: {resp}")
    token = resp["access_token"]
    _TOKEN_CACHE[cache_key] = (token, time.time() + resp.get("expires_in", 7200))
    return token


def send_appchat(corp_id: str, corp_secret: str, chatid: str,
                 content: str, msg_type: str = "text") -> dict:
    token = get_access_token(corp_id, corp_secret)
    url = APPCHAT_URL.format(token=urllib.parse.quote(token, safe=""))
    body = _build_body(msg_type, content)
    body["chatid"] = chatid
    if msg_type == "text":
        body["safe"] = 0
    return _post_json(url, body)


def send_feishu(webhook_url: str, content: str, msg_type: str = "text") -> dict:
    """飞书自定义群机器人。webhook_url 为完整地址或仅 token。"""
    if webhook_url.startswith("http"):
        url = webhook_url
    else:
        url = FEISHU_WEBHOOK_URL.format(token=webhook_url)
    if msg_type == "markdown":
        # 飞书 markdown 通过 post 富文本更稳妥，这里用 text 渲染换行
        body = {"msg_type": "text", "content": {"text": content}}
    else:
        body = {"msg_type": "text", "content": {"text": content}}
    return _post_json(url, body)


def get_feishu_token(app_id: str, app_secret: str) -> str:
    """获取飞书应用 tenant_access_token（带缓存）。"""
    cache_key = "feishu:" + app_id
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    resp = _post_json(FEISHU_TOKEN_URL, {"app_id": app_id, "app_secret": app_secret})
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"飞书获取 token 失败: {resp}")
    token = resp["tenant_access_token"]
    _TOKEN_CACHE[cache_key] = (token, time.time() + resp.get("expire", 7200))
    return token


def send_feishu_app(app_id: str, app_secret: str, chat_id: str,
                    content: str, msg_type: str = "text") -> dict:
    """飞书应用消息：通过 IM 接口发消息到群会话 chat_id。"""
    token = get_feishu_token(app_id, app_secret)
    # 飞书 IM content 字段是 JSON 字符串
    inner = {"text": content} if msg_type != "markdown" else {"text": content}
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps(inner, ensure_ascii=False),
    }
    return _post_json(FEISHU_MSG_URL, body, headers={"Authorization": f"Bearer {token}"})


# 飞书文件上传与文件消息
FEISHU_FILE_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/files"


def _post_multipart(url: str, fields, files, headers: dict | None = None, timeout: int = 60) -> dict:
    """multipart/form-data POST。files = [(name, filename, content_type, bytes)]。"""
    boundary = "----WorkBuddy" + secrets.token_hex(8)
    body = bytearray()
    for name, value in fields:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
        body += (str(value) + "\r\n").encode("utf-8")
    for name, filename, ctype, data in files:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n".encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=bytes(body), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {exc.read().decode('utf-8','ignore')[:300]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


def upload_feishu_file(app_id: str, app_secret: str, file_path, file_name: str | None = None) -> str:
    """上传文件到飞书，返回 file_key。"""
    token = get_feishu_token(app_id, app_secret)
    p = Path(file_path)
    fname = file_name or p.name
    data = p.read_bytes()
    ctype = "text/plain" if fname.lower().endswith((".txt", ".md")) else "application/octet-stream"
    resp = _post_multipart(
        FEISHU_FILE_UPLOAD_URL,
        fields=[("file_type", "stream"), ("file_name", fname)],
        files=[("file", fname, ctype, data)],
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"飞书上传文件失败: {resp}")
    return resp["data"]["file_key"]


def send_feishu_file(app_id: str, app_secret: str, chat_id: str,
                     file_path, file_name: str | None = None) -> dict:
    """上传文件并发送文件消息到飞书群。"""
    token = get_feishu_token(app_id, app_secret)
    file_key = upload_feishu_file(app_id, app_secret, file_path, file_name)
    body = {
        "receive_id": chat_id,
        "msg_type": "file",
        "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
    }
    return _post_json(FEISHU_MSG_URL, body, headers={"Authorization": f"Bearer {token}"})


def send_file_to_group(group_cfg: dict, file_path, file_name: str | None = None) -> dict:
    """向群发送文件（当前支持 feishu_app）。"""
    mode = group_cfg.get("mode", "appchat")
    if mode == "feishu_app":
        app_id = group_cfg.get("app_id") or os.environ.get("FEISHU_APP_ID", "")
        app_secret = group_cfg.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")
        chat_id = group_cfg.get("chat_id") or group_cfg.get("id", "")
        if not (app_id and app_secret and chat_id):
            raise ValueError("feishu_app 模式缺少 app_id/app_secret/chat_id")
        resp = send_feishu_file(app_id, app_secret, chat_id, file_path, file_name)
        code = resp.get("code", 0)
        if code not in (0, None):
            raise RuntimeError(f"飞书返回错误: {resp}")
        return resp
    raise NotImplementedError(f"send_file 暂不支持 mode={mode}（仅支持 feishu_app）")


def send_to_group(group_cfg: dict, content: str, msg_type: str | None = None) -> dict:
    """根据 group_cfg.mode 分发到对应渠道。凭据可来自配置或环境变量。"""
    mode = group_cfg.get("mode", "appchat")
    msg_type = msg_type or group_cfg.get("msg_type", "text")
    if mode == "feishu_app":
        app_id = group_cfg.get("app_id") or os.environ.get("FEISHU_APP_ID", "")
        app_secret = group_cfg.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")
        chat_id = group_cfg.get("chat_id") or group_cfg.get("id", "")
        if not (app_id and app_secret and chat_id):
            raise ValueError("feishu_app 模式缺少 app_id/app_secret/chat_id")
        resp = send_feishu_app(app_id, app_secret, chat_id, content, msg_type)
        code = resp.get("code", 0)
        if code not in (0, None):
            raise RuntimeError(f"飞书返回错误: {resp}")
        return resp
    if mode == "feishu":
        url = group_cfg.get("webhook_url") or os.environ.get("FEISHU_WEBHOOK_URL", "")
        if not url:
            raise ValueError("feishu 模式缺少 webhook_url（配置或 FEISHU_WEBHOOK_URL）")
        resp = send_feishu(url, content, msg_type)
        # 飞书成功返回 {"StatusCode":0} 或 {"code":0,...}，非 0 视为失败
        code = resp.get("StatusCode", resp.get("code", 0))
        if code not in (0, None):
            raise RuntimeError(f"飞书返回错误: {resp}")
        return resp
    if mode == "webhook":
        key = group_cfg.get("webhook_key") or os.environ.get("WECOM_WEBHOOK_KEY", "")
        if not key:
            raise ValueError("webhook 模式缺少 webhook_key（配置或 WECOM_WEBHOOK_KEY）")
        resp = send_webhook(key, content, msg_type)
    else:  # appchat
        corp_id = group_cfg.get("corp_id") or os.environ.get("WECOM_CORP_ID", "")
        corp_secret = group_cfg.get("corp_secret") or os.environ.get("WECOM_CORP_SECRET", "")
        chatid = group_cfg.get("chatid") or group_cfg.get("id", "")
        if not (corp_id and corp_secret and chatid):
            raise ValueError("appchat 模式缺少 corp_id/corp_secret/chatid")
        resp = send_appchat(corp_id, corp_secret, chatid, content, msg_type)
    if resp.get("errcode") not in (0, None):
        raise RuntimeError(f"企业微信返回错误: {resp}")
    return resp


def send_with_retry(group_cfg: dict, content: str, logger: logging.Logger,
                    retries: int = 3, delay: int = 10,
                    msg_type: str | None = None) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = send_to_group(group_cfg, content, msg_type)
            gid = group_cfg.get("id") or group_cfg.get("chat_id") or group_cfg.get("mode")
            logger.info(f"推送成功 (第{attempt}次尝试) group={gid}")
            return resp
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning(f"推送失败 (第{attempt}/{retries}次): {exc}")
            if attempt < retries:
                time.sleep(delay)
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# CLI: python push_wecom.py --group turkey_morning --file <path>
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="群消息推送（飞书/企业微信）")
    ap.add_argument("--config", default="automation_config.json")
    ap.add_argument("--group", required=True, help="config 中 groups 的 key")
    ap.add_argument("--file", help="读取该文件内容推送")
    ap.add_argument("--text", help="直接推送该文本")
    ap.add_argument("--msg-type", default=None, choices=["text", "markdown"])
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_cfg = cfg.get("logging", {})
    logger = setup_logging(log_cfg.get("log_dir", "logs"), log_cfg.get("level", "INFO"))

    group = (cfg.get("groups") or cfg.get("wecom", {}).get("groups", {})).get(args.group)
    if not group:
        logger.error(f"未找到群配置: {args.group}")
        return 2

    content = args.text
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    if not content:
        logger.error("无推送内容（请提供 --file 或 --text）")
        return 2

    try:
        send_with_retry(group, content, logger, retries=3, delay=10, msg_type=args.msg_type)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error(f"最终推送失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
