"""大力钱包商户 API 客户端。"""
import base64
import hashlib
import hmac
import json
import time
import uuid

import requests

PREFIX = "dali"
BASE_URL = "https://api.dali.network"


class DaliClient:
    def __init__(self, merchant_id: str, secret: str):
        self.base_url = BASE_URL
        self.merchant_id = str(merchant_id)
        self.secret = secret.encode()

    def _hmac(self, msg: str) -> str:
        return base64.b64encode(
            hmac.new(self.secret, msg.encode(), hashlib.sha256).digest()
        ).decode()

    def call(self, path: str, payload: dict) -> dict:
        body_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode()
        ).decode()
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        canonical = f"{ts}\n{nonce}\nPOST\n{path}\n{body_b64}"
        headers = {
            f"{PREFIX}-id": self.merchant_id,
            f"{PREFIX}-timestamp": str(ts),
            f"{PREFIX}-nonce": nonce,
            f"{PREFIX}-sign": self._hmac(canonical),
            "Content-Type": "text/plain",
        }
        r = requests.post(self.base_url + path, data=body_b64, headers=headers, timeout=15)
        resp = r.json()
        if resp.get("code") != 10000:
            return {"ok": False, "raw": resp}
        resp_sign = r.headers.get(f"{PREFIX}-sign", "")
        if not hmac.compare_digest(self._hmac(resp["data"]), resp_sign):
            return {"ok": False, "error": "response sign mismatch"}
        return {"ok": True, "data": json.loads(base64.b64decode(resp["data"]))}

    def verify_callback(self, data_b64: str, sign: str) -> bool:
        """验证回调签名。"""
        return hmac.compare_digest(self._hmac(data_b64), sign)


def get_client() -> DaliClient | None:
    """从环境变量读取凭证，创建客户端。未配置返回 None。"""
    import os
    merchant_id = os.getenv("DALI_MERCHANT_ID", "")
    secret = os.getenv("DALI_SECRET", "")
    if not merchant_id or not secret:
        return None
    return DaliClient(merchant_id, secret)
