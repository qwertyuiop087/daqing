"""OKPay 商户支付接口客户端（HMAC-SHA256 协议）。

文档：https://docs.okaypay.me/doc.html
- 根地址 https://api.okaypay.me，前缀 /shop，POST 表单
- 公共字段：id / timestamp / nonce / sign
- 签名：参数去 sign、去 null 与空串（0/false 保留）、嵌套点号展开、
       键名 ASCII 升序、k=v 用 & 拼接（值不 URL 编码），
       sign = UPPER(HMAC_SHA256(base, token))

这里只用标准库（urllib），不引入新依赖。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request

BASE_URL = "https://api.okaypay.me"
PREFIX = "/shop"
TIMEOUT = 15


# ---------------- 签名 ----------------

def _flatten(out: dict, prefix: str, value) -> None:
    """把嵌套参数展开成 点号 形式；null 与空串丢弃，0/false 保留。"""
    if value is None:
        return
    if isinstance(value, bool):
        out[prefix] = "true" if value else "false"
        return
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten(out, key, v)
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(out, f"{prefix}.{i}", v)
        return
    text = str(value)
    if text == "":
        return
    out[prefix] = text


def build_base(params: dict) -> str:
    """构造签名原文（不含 sign）。"""
    flat: dict = {}
    for key, value in params.items():
        if key == "sign":
            continue
        _flatten(flat, str(key), value)
    items = sorted(flat.items(), key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def sign(params: dict, token: str) -> str:
    """计算签名（64 位大写十六进制）。"""
    base = build_base(params)
    digest = hmac.new(token.encode("utf-8"), base.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest().upper()


def verify(payload: dict, token: str) -> bool:
    """验签：恒定时间比较。payload 为响应/回调的完整 JSON。"""
    got = str((payload or {}).get("sign") or "")
    if not got:
        return False
    expect = sign(payload, token)
    return hmac.compare_digest(expect, got.upper())


def signed_params(params: dict, shop_id, token: str, now: int | None = None) -> dict:
    """补齐 id / timestamp / nonce / sign。"""
    payload = dict(params)
    payload["id"] = shop_id
    payload["timestamp"] = int(now if now is not None else time.time())
    payload["nonce"] = secrets.token_hex(8)  # 16 位，满足 8–128 位要求
    payload["sign"] = sign(payload, token)
    return payload


# ---------------- HTTP ----------------

class OkayPayError(RuntimeError):
    pass


def _post(path: str, params: dict) -> dict:
    url = BASE_URL + PREFIX + path
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception as e:
        raise OkayPayError(f"返回不是 JSON：{raw[:200]}") from e


def call(path: str, params: dict, shop_id, token: str) -> dict:
    """签名 → 请求 → 验签（成功报文必须带 sign）。"""
    payload = signed_params(params, shop_id, token)
    result = _post(path, payload)
    status = (result or {}).get("status")
    if status != "success":
        raise OkayPayError((result or {}).get("msg") or f"请求失败：{status}")
    if not verify(result, token):
        raise OkayPayError("响应验签失败")
    return result


# ---------------- 业务封装 ----------------

def pay_link(shop_id, token: str, amount: str, coin: str = "USDT",
             unique_id: str = "", name: str = "", callback_url: str = "",
             return_url: str = "") -> dict:
    """创建支付链接，返回 data（order_id / pay_url / status）。"""
    params = {"amount": str(amount), "coin": coin}
    if unique_id:
        params["unique_id"] = unique_id
    if name:
        params["name"] = name
    if callback_url:
        params["callback_url"] = callback_url
    if return_url:
        params["return_url"] = return_url
    return call("/payLink", params, shop_id, token).get("data") or {}


def check_deposit(shop_id, token: str, unique_id: str) -> dict:
    """按商户订单号查充值状态，返回 data（status 0/1）。"""
    return call("/checkDeposit", {"unique_id": unique_id}, shop_id, token).get("data") or {}


def balance(shop_id, token: str) -> dict:
    return call("/balance", {}, shop_id, token).get("data") or {}


# ---------------- 配置 ----------------

def guess_callback_url() -> str:
    """自动推断回调地址：云平台域名 + /okpay/callback。

    后台不填也能用：Railway 会注入 RAILWAY_PUBLIC_DOMAIN，
    其它平台可自己设 PUBLIC_BASE_URL。
    """
    import os

    for key in ("PUBLIC_BASE_URL", "RAILWAY_STATIC_URL", "RAILWAY_PUBLIC_DOMAIN"):
        value = (os.getenv(key) or "").strip()
        if not value:
            continue
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        return value.rstrip("/") + "/okpay/callback"
    return ""


def get_config_values() -> tuple[str, str, str]:
    """读取商户配置：优先数据库后台配置，其次环境变量。

    返回 (shop_id, token, callback_url)，未配置时返回空串。
    """
    from config import settings
    from database import SessionLocal, get_config

    shop_id = ""
    token = ""
    callback = ""
    try:
        with SessionLocal() as session:
            shop_id = (get_config(session, "okpay_shop_id", "") or "").strip()
            token = (get_config(session, "okpay_token", "") or "").strip()
            callback = (get_config(session, "okpay_callback_url", "") or "").strip()
    except Exception:
        pass
    shop_id = shop_id or str(getattr(settings, "OKPAY_SHOP_ID", "") or "").strip()
    token = token or str(getattr(settings, "OKPAY_TOKEN", "") or "").strip()
    callback = callback or str(getattr(settings, "OKPAY_CALLBACK_URL", "") or "").strip()
    callback = callback or guess_callback_url()
    return shop_id, token, callback


def is_configured() -> bool:
    shop_id, token, _ = get_config_values()
    return bool(shop_id and token)
