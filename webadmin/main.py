"""DQFB 管理后台（FastAPI）：连机器人同一个数据库，网页配置抽奖/套餐/洗数据/欢迎语。

部署要求：
- 环境变量 DATABASE_URL 指向机器人在用的同一个 Postgres
- 环境变量 ADMIN_PASSWORD 为后台登录密码（必填，否则拒绝启动）
- 本目录（webadmin）位于机器人仓库根目录下，通过父目录 import database / utils
"""
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

# 机器人代码根目录 = webadmin 的父目录
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database  # noqa: E402  （机器人同款 ORM 与配置读写，保证两边格式一致）
from utils.lottery import parse_prizes_input, parse_redeem_input  # noqa: E402

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
COOKIE_NAME = "dqfb_admin"
COOKIE_TTL = 24 * 3600  # 登录有效期 24 小时

if not ADMIN_PASSWORD:
    print("❌ 未配置 ADMIN_PASSWORD 环境变量，管理后台拒绝启动。")
    sys.exit(1)

# 确保表存在（与机器人一致；幂等）
database.init_db()

app = FastAPI(title="DQFB Admin")


# ---------------- 登录令牌（HMAC 签名 cookie，无额外依赖） ----------------

def _secret() -> bytes:
    return hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest()


def _make_token() -> str:
    expires = int(time.time()) + COOKIE_TTL
    msg = str(expires).encode()
    sig = hmac.new(_secret(), msg, hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _check_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        expires = int(exp_s)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    return hmac.compare_digest(sig, hmac.new(_secret(), exp_s.encode(), hashlib.sha256).hexdigest())


def _auth(request: Request) -> None:
    if not _check_token(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="未登录")


# ---------------- 页面 ----------------

@app.get("/")
def landing():
    page = ROOT / "website" / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"ok": True, "hint": "请在仓库根目录放置 website/index.html"})


@app.get("/admin")
def admin_page():
    return FileResponse(Path(__file__).resolve().parent / "static" / "admin.html")


# ---------------- 认证 ----------------

@app.post("/api/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求格式错误")
    if not hmac.compare_digest(str(data.get("password", "")), ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="密码错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, _make_token(), max_age=COOKIE_TTL, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------- 配置解析 ----------------

def _parse_plans(text: str) -> list[dict]:
    plans = []
    for item in (text or "").strip().split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"「{item}」格式错误，应为 天数:价格")
        try:
            days = int(parts[0].strip())
            price = float(parts[1].strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"「{item}」数值不是数字")
        if days < 1 or price <= 0:
            raise HTTPException(status_code=400, detail=f"「{item}」天数需≥1、价格需>0")
        plans.append({"days": days, "price": price})
    if not plans:
        raise HTTPException(status_code=400, detail="至少一个套餐")
    return plans


def _parse_wash_prices(text: str) -> list[dict]:
    prices = []
    for item in (text or "").strip().split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail=f"「{item}」格式错误，应为 行数:单价")
        try:
            min_rows = int(parts[0].strip())
            price = float(parts[1].strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"「{item}」数值不是数字")
        if min_rows < 0 or price <= 0:
            raise HTTPException(status_code=400, detail=f"「{item}」行数需≥0、单价需>0")
        prices.append({"min_rows": min_rows, "price_per_row": price})
    if not prices:
        raise HTTPException(status_code=400, detail="至少一个档位")
    return prices


# ---------------- 配置读取 ----------------

@app.get("/api/config")
def get_config_api(request: Request):
    _auth(request)
    with database.SessionLocal() as session:
        return {
            "lottery_prizes": json.dumps(database.get_lottery_prizes(session), ensure_ascii=False),
            "lottery_premium_prizes": json.dumps(database.get_lottery_premium_prizes(session), ensure_ascii=False),
            "lottery_redeem": json.dumps(database.get_lottery_redeem(session), ensure_ascii=False),
            "plans": json.dumps(database.get_plans(session), ensure_ascii=False),
            "wash_prices": json.dumps(database.get_wash_prices(session), ensure_ascii=False),
            "wash_price_floor": database.get_wash_price_floor(session),
            "wash_quota": database.get_wash_quota(session),
            "usdt_rate": database.get_config(session, "usdt_rate", ""),
            "welcome_text": database.get_config(session, "welcome_text", database.DEFAULT_WELCOME_TEXT),
            "lottery_notify_template": database.get_lottery_notify_template(session),
        }


@app.post("/api/config/{key}")
async def save_config(key: str, request: Request):
    _auth(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求格式错误")
    value = str(data.get("value", ""))
    with database.SessionLocal() as session:
        if key == "lottery_prizes" or key == "lottery_premium_prizes":
            prizes, err = parse_prizes_input(value)
            if err:
                raise HTTPException(status_code=400, detail=err)
            if key == "lottery_prizes":
                database.set_lottery_prizes(session, prizes)
            else:
                database.set_lottery_premium_prizes(session, prizes)
        elif key == "lottery_redeem":
            items, err = parse_redeem_input(value)
            if err:
                raise HTTPException(status_code=400, detail=err)
            database.set_lottery_redeem(session, items)
        elif key == "plans":
            database.set_plans(session, _parse_plans(value))
        elif key == "wash_prices":
            database.set_wash_prices(session, _parse_wash_prices(value))
        elif key == "wash_price_floor":
            try:
                v = float(value)
            except ValueError:
                raise HTTPException(status_code=400, detail="价格下限不是数字")
            if v <= 0:
                raise HTTPException(status_code=400, detail="价格下限需大于 0")
            database.set_wash_price_floor(session, v)
        elif key == "wash_quota":
            if not value.strip().isdigit():
                raise HTTPException(status_code=400, detail="配额需为非负整数")
            database.set_wash_quota(session, int(value))
        elif key == "usdt_rate":
            value = value.strip()
            if value:
                try:
                    float(value)
                except ValueError:
                    raise HTTPException(status_code=400, detail="汇率不是数字")
                database.set_usdt_rate(session, float(value))
            else:
                database.set_config(session, "usdt_rate", "")
        elif key == "welcome_text":
            database.set_config(session, "welcome_text", value)
            # 网页编辑不带消息实体（emoji/加粗），清掉 entities，机器人会按 HTML 渲染
            database.set_config(session, "welcome_entities", "")
        elif key == "lottery_notify_template":
            if not value.strip():
                raise HTTPException(status_code=400, detail="模板不能为空")
            database.set_lottery_notify_template(session, value)
        else:
            raise HTTPException(status_code=400, detail="未知配置项")
    return {"ok": True}


@app.post("/api/reset/{key}")
def reset_config(key: str, request: Request):
    _auth(request)
    with database.SessionLocal() as session:
        if key == "lottery_prizes":
            database.set_lottery_prizes(session, database.DEFAULT_LOTTERY_PRIZES)
        elif key == "lottery_premium_prizes":
            database.set_lottery_premium_prizes(session, database.DEFAULT_LOTTERY_PREMIUM_PRIZES)
        elif key == "lottery_redeem":
            database.set_lottery_redeem(session, database.DEFAULT_LOTTERY_REDEEM)
        elif key == "plans":
            database.set_plans(session, json.loads(database.DEFAULT_PLANS))
        elif key == "wash_prices":
            database.set_wash_prices(session, database.DEFAULT_WASH_PRICES)
        elif key == "wash_price_floor":
            database.set_wash_price_floor(session, database.DEFAULT_WASH_PRICE_FLOOR)
        elif key == "wash_quota":
            database.set_wash_quota(session, 0)
        elif key == "usdt_rate":
            database.set_config(session, "usdt_rate", "")
        elif key == "welcome_text":
            database.set_config(session, "welcome_text", "")
            database.set_config(session, "welcome_entities", "")
        elif key == "lottery_notify_template":
            database.set_lottery_notify_template(session, database.DEFAULT_LOTTERY_NOTIFY_TEMPLATE)
        else:
            raise HTTPException(status_code=400, detail="未知配置项")
    return {"ok": True}


# ---------------- 概览 / 抽奖记录 ----------------

@app.get("/api/stats")
def stats(request: Request):
    _auth(request)
    with database.SessionLocal() as session:
        total_users = session.scalar(select(func.count()).select_from(database.User)) or 0
        members = session.scalar(
            select(func.count()).select_from(database.User).where(
                database.User.membership_expires_at > database.utcnow()
            )
        ) or 0
        lottery = database.get_lottery_stats(session)
        charged, pending = database.get_washed_breakdown(session)
        records = database.get_lottery_records(session, 50)
        rec_list = [
            {
                "time": database.beijing_str(r.created_at),
                "user": r.username or str(r.user_id),
                "source": r.source,
                "prize": r.prize_label,
            }
            for r in records
        ]
    return {
        "total_users": int(total_users),
        "members": int(members),
        "lottery_total": lottery["total"],
        "lottery_today": lottery["today"],
        "washed_charged": int(charged),
        "washed_pending": int(pending),
        "records": rec_list,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
