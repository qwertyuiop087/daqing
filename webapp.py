"""内置网站 + 管理后台（aiohttp）：机器人一个服务同时提供网页与后台，免去单独部署。

- GET /                 介绍页（website/index.html）
- GET /admin            管理后台登录页
- POST /api/login/logout
- GET /api/config  POST /api/config/{key}  POST /api/reset/{key}  GET /api/stats
环境变量 ADMIN_PASSWORD 为后台密码；未配置时介绍页正常展示、后台提示不可用。
"""
import asyncio
import hashlib
import hmac
import math
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

import database
from config import settings
from utils import config_api

ROOT = Path(__file__).resolve().parent
COOKIE_NAME = "dqfb_admin"
COOKIE_TTL = 24 * 3600  # 登录有效期 24 小时
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")  # 后台登录用户名，默认 admin

# ---------------- 登录防护（防暴力破解） ----------------

LOGIN_FAIL_WINDOW = 600        # 失败统计窗口（秒）
LOGIN_FAIL_MAX = 15            # 单个 IP 在窗口内失败次数上限，超过则锁定该 IP 登录
LOGIN_GLOBAL_FAIL_MAX = 100    # 全局兜底：窗口内总失败次数上限（防分布式爆破）
LOGIN_LOCK_SECONDS = 900       # 锁定时长（15 分钟）
# 可选：只允许这些 IP 登录（逗号分隔，如 "1.2.3.4,5.6.7.8"；留空 = 不限）
ADMIN_IP_WHITELIST = [x.strip() for x in os.getenv("ADMIN_IP_WHITELIST", "").split(",") if x.strip()]

# 每个 IP 的失败时间戳（用于按 IP 锁定）；全局失败时间戳（用于兜底）
_ip_failures: dict[str, list[float]] = {}
_global_failures: list[float] = []

if ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 8:
    print("⚠️ 警告：ADMIN_PASSWORD 少于 8 位，容易被暴力破解，建议改成 12 位以上强密码。")

_bot = None  # 由 main.py 注入，用于给用户发到账通知（可能为 None）

# ---------------- 基础防护：限流 / 缓存 ----------------

RATE_WINDOW = 60            # 统计窗口（秒）
RATE_MAX = 90               # 普通接口：每 IP 每窗口最多请求数
LOGIN_RATE_MAX = 8          # 登录接口：每 IP 每窗口最多尝试次数
PUBLIC_CACHE_TTL = 10       # 公共接口缓存秒数

_rate_hits: dict[str, list[float]] = {}
_public_cache: dict[str, tuple[float, dict]] = {}


def _client_ip(request: web.Request) -> str:
    """取客户端 IP：只信任代理链里最后一跳（Railway 等边缘代理追加的那一段），
    客户端可以伪造 X-Forwarded-For 前面的部分，但伪造不了最后一跳。"""
    value = request.headers.get("X-Forwarded-For")
    if value:
        return value.split(",")[-1].strip() or (request.remote or "unknown")
    value = request.headers.get("X-Real-IP")
    if value:
        return value.strip()
    return request.remote or "unknown"


def _rate_ok(ip: str, limit: int) -> bool:
    now = time.time()
    hits = [t for t in _rate_hits.get(ip, []) if now - t < RATE_WINDOW]
    hits.append(now)
    _rate_hits[ip] = hits
    # 被大量不同 IP 刷时，控制字典规模
    if len(_rate_hits) > 20000:
        for key in [k for k, v in _rate_hits.items() if not v or now - v[-1] > RATE_WINDOW]:
            _rate_hits.pop(key, None)
    return len(hits) <= limit


@web.middleware
async def guard_middleware(request: web.Request, handler):
    """全站基础防护：按 IP 限流，登录接口更严格。"""
    if not _rate_ok(_client_ip(request), LOGIN_RATE_MAX if request.path == "/api/login" else RATE_MAX):
        raise web.HTTPTooManyRequests(text="请求过于频繁，请稍后再试")
    return await handler(request)


def _cached(key: str, builder):
    """公共接口短缓存：降低高频刷新对数据库的压力。"""
    now = time.time()
    hit = _public_cache.get(key)
    if hit and now - hit[0] < PUBLIC_CACHE_TTL:
        return hit[1]
    data = builder()
    _public_cache[key] = (now, data)
    return data


def _secret() -> bytes:
    return hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest()


def _make_token(username: str = "admin") -> str:
    expires = int(time.time()) + COOKIE_TTL
    user = str(username or "admin").replace(".", "_")
    msg = f"{expires}.{user}".encode()
    sig = hmac.new(_secret(), msg, hashlib.sha256).hexdigest()
    return f"{expires}.{user}.{sig}"


def _check_token(token) -> str | None:
    """校验登录令牌，返回登录用户名；无效返回 None。"""
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        exp_s, user, sig = parts
        expires = int(exp_s)
    except ValueError:
        return None
    if expires < int(time.time()):
        return None
    msg = f"{exp_s}.{user}".encode()
    if not hmac.compare_digest(sig, hmac.new(_secret(), msg, hashlib.sha256).hexdigest()):
        return None
    return user


def _auth(request: web.Request) -> str:
    """校验登录，返回登录用户名（用于审计）。"""
    if not ADMIN_PASSWORD:
        raise web.HTTPForbidden(text="未配置 ADMIN_PASSWORD，后台不可用")
    user = _check_token(request.cookies.get(COOKIE_NAME))
    if not user:
        raise web.HTTPUnauthorized(text="未登录")
    return user


# ---------------- 页面 ----------------

_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>错落雨分包机器人 · CUOLUOYU</title>
<style>
body{margin:0;background:#05060f;color:#eef0fa;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px}
h1{font-size:30px;margin-bottom:12px}
p{color:#9aa1bd;margin-bottom:26px;line-height:1.8}
a{display:inline-block;padding:12px 26px;border-radius:12px;
background:linear-gradient(120deg,#8b5cf6,#22d3ee);color:#fff;text-decoration:none;font-weight:700}
.note{margin-top:28px;font-size:12px;color:#555}
</style></head>
<body><div>
<h1>错落雨分包机器人</h1>
<p>号码数据处理 · 抽奖系统 · 会员服务</p>
<a href="https://t.me/sechou_bot">✈️ 打开机器人</a>
<p class="note">网站页面文件还未上传（缺少 website/index.html），当前为备用页</p>
</div></body></html>"""


async def index(request: web.Request):
    page = ROOT / "website" / "index.html"
    if page.exists():
        return web.FileResponse(page)
    print("⚠️ 缺少 website/index.html（代码没传全），已使用内置备用页")
    return web.Response(text=_FALLBACK_HTML, content_type="text/html")


async def admin_page(request: web.Request):
    page = ROOT / "webadmin" / "static" / "admin.html"
    if page.exists():
        return web.FileResponse(page)
    print("⚠️ 缺少 webadmin/static/admin.html（代码没传全），后台页面不可用")
    return web.Response(
        text="<h2 style='font-family:sans-serif'>⚠️ 缺少 webadmin/static/admin.html</h2>"
        "<p style='font-family:sans-serif'>请完整上传代码后再打开后台。</p>",
        content_type="text/html",
    )


async def health(request: web.Request):
    return web.Response(text="ok")


# ---------------- 认证 ----------------

async def login(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="请求格式错误")

    # 按 IP 锁定 + 全局兜底：单个 IP 窗口内失败过多就锁该 IP；
    # 全局失败次数过高（分布式爆破）才整体锁定，避免一人捣乱锁死管理员
    now = time.time()
    ip = _client_ip(request)
    ip_fails = [t for t in _ip_failures.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
    if len(ip_fails) >= LOGIN_FAIL_MAX:
        raise web.HTTPTooManyRequests(text="尝试次数过多，请 15 分钟后再试")
    _global_failures[:] = [t for t in _global_failures if now - t < LOGIN_FAIL_WINDOW]
    if len(_global_failures) >= LOGIN_GLOBAL_FAIL_MAX:
        raise web.HTTPTooManyRequests(text="尝试次数过多，请 15 分钟后再试")

    # IP 白名单（配置了才生效）
    if ADMIN_IP_WHITELIST and ip not in ADMIN_IP_WHITELIST:
        raise web.HTTPForbidden(text="当前网络不在允许登录的白名单内")

    # 用户名 + 密码双验证（用户名未配置时默认 admin）
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    ok_user = bool(ADMIN_USERNAME) and hmac.compare_digest(username, ADMIN_USERNAME)
    ok_pass = bool(ADMIN_PASSWORD) and hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        ip_fails.append(now)
        _ip_failures[ip] = ip_fails
        _global_failures.append(now)
        if len(_ip_failures) > 5000:  # 控制字典规模
            for k in [k for k, v in _ip_failures.items() if not v or now - v[-1] > LOGIN_FAIL_WINDOW]:
                _ip_failures.pop(k, None)
        await asyncio.sleep(0.5)  # 失败延时，拖慢爆破速度
        raise web.HTTPUnauthorized(text="用户名或密码错误")
    with database.SessionLocal() as session:
        database.log_audit(session, username, "login", f"后台登录成功（IP: {ip}）")
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE_NAME, _make_token(username), max_age=COOKIE_TTL, httponly=True, samesite="lax")
    return resp


async def logout(request: web.Request):
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE_NAME)
    return resp


# ---------------- 配置 API ----------------

async def get_config_api(request: web.Request):
    _auth(request)
    with database.SessionLocal() as session:
        return web.json_response(config_api.get_all_config(session))


async def save_config_api(request: web.Request):
    admin = _auth(request)
    key = request.match_info["key"]
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="请求格式错误")
    value = str(data.get("value", ""))
    with database.SessionLocal() as session:
        try:
            config_api.save_config(session, key, value)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        database.log_audit(session, admin, "config_save", f"{key} = {value[:120]}")
    return web.json_response({"ok": True})


async def reset_config_api(request: web.Request):
    admin = _auth(request)
    key = request.match_info["key"]
    with database.SessionLocal() as session:
        try:
            config_api.reset_config(session, key)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        database.log_audit(session, admin, "config_reset", f"{key}")
    return web.json_response({"ok": True})


async def stats_api(request: web.Request):
    _auth(request)
    with database.SessionLocal() as session:
        return web.json_response(config_api.get_stats(session))


async def public_feed(request: web.Request):
    """公开中奖播报（无需登录）：最近 10 条真实中奖记录，用户名打码。"""

    def build():
        with database.SessionLocal() as session:
            records = database.get_lottery_records(session, 50)
        items = []
        for r in records:
            if r.prize_type == "none" or not r.prize_label or r.prize_label == "谢谢参与":
                continue
            name = (r.username or "").strip()
            if name:
                masked = (name[:3] + "**") if len(name) > 3 else (name[:1] + "**")
            else:
                masked = "匿名用户"
            items.append(
                {
                    "user": masked,
                    "prize": r.prize_label,
                    "time": database.beijing_str(r.created_at),
                }
            )
            if len(items) >= 10:
                break
        return {"ok": True, "items": items}

    return web.json_response(_cached("feed", build))


async def public_plans(request: web.Request):
    """公开套餐列表（无需登录）：与机器人后台配置同步。"""

    def build():
        with database.SessionLocal() as session:
            plans = database.get_plans(session)
        return {"ok": True, "items": [{"days": int(p["days"]), "price": p["price"]} for p in plans]}

    return web.json_response(_cached("plans", build))


# ---------------- 成员管理 ----------------

async def members_api(request: web.Request):
    """成员列表：搜索 ID / 用户名 / 昵称。"""
    _auth(request)
    q = request.query.get("q", "")
    with database.SessionLocal() as session:
        return web.json_response({"ok": True, "items": config_api.search_members(session, q)})


async def members_adjust_api(request: web.Request):
    """调整成员天数/余额（正数增加、负数扣减），并给用户发到账通知（仅增加时）。"""
    admin = _auth(request)
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="请求格式错误")
    try:
        user_id = int(data.get("user_id", 0) or 0)
        days_delta = int(data.get("days_delta", 0) or 0)
        balance_delta = float(data.get("balance_delta", 0) or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="数值格式错误")
    # 拒绝 NaN / Infinity：JSON 解析会接受这些值，写库会永久损坏余额
    if not math.isfinite(balance_delta) or not math.isfinite(float(days_delta)):
        raise web.HTTPBadRequest(text="数值格式错误")
    if abs(days_delta) > 365000 or abs(balance_delta) > 1e9:
        raise web.HTTPBadRequest(text="调整数值超出允许范围")
    if not user_id:
        raise web.HTTPBadRequest(text="缺少用户")
    if not days_delta and not balance_delta:
        raise web.HTTPBadRequest(text="调整值不能为空")
    with database.SessionLocal() as session:
        try:
            result = config_api.adjust_member(session, user_id, days_delta, balance_delta)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        detail = "、".join(result.get("changed") or [])
        database.log_audit(session, admin, "member_adjust", f"用户 {user_id}：{detail}")
    if _bot and (days_delta > 0 or balance_delta > 0):
        parts = []
        if days_delta > 0:
            parts.append(f"会员 <b>{days_delta}</b> 天")
        if balance_delta > 0:
            parts.append(f"洗数据余额 <b>¥{balance_delta:.4f}</b>")
        if parts:
            try:
                await _bot.send_message(
                    user_id,
                    "🎉 <b>到账通知</b>\n\n管理员为你添加了：\n" + "\n".join(parts) + "\n\n感谢使用～",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    return web.json_response({"ok": True, "result": result})


async def public_site(request: web.Request):
    """公开网站配置（无需登录）：落地页读取，用于动态渲染标题/联系方式/主色等。"""

    def build():
        with database.SessionLocal() as session:
            return {"ok": True, "site": config_api.get_site_config(session)}

    return web.json_response(_cached("site", build))


async def site_api(request: web.Request):
    """后台读取网站配置。"""
    _auth(request)
    with database.SessionLocal() as session:
        return web.json_response({"ok": True, "site": config_api.get_site_config(session)})


async def site_save_api(request: web.Request):
    """后台保存网站配置。"""
    admin = _auth(request)
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="请求格式错误")
    with database.SessionLocal() as session:
        try:
            cfg = config_api.set_site_config(session, data)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        database.log_audit(session, admin, "site_save", "、".join(sorted((data or {}).keys())) or "（空）")
    _public_cache.pop("site", None)  # 立即让公开接口生效
    return web.json_response({"ok": True, "site": cfg})


# ---------------- 机器人资料（改名字 / 简介） ----------------

def _bot_token() -> str:
    return str(getattr(settings, "BOT_TOKEN", "") or "").strip()


async def bot_profile_api(request: web.Request):
    """读取机器人当前显示名称、简介与简短说明（调用 Telegram Bot API）。"""
    _auth(request)
    token = _bot_token()
    if not token:
        raise web.HTTPBadRequest(text="未配置 BOT_TOKEN")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                me = await r.json()
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMyDescription",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                desc = await r.json()
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMyShortDescription",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                short = await r.json()
    except Exception as e:
        raise web.HTTPBadRequest(text=f"读取失败：{str(e)[:100]}")
    name = (me.get("result") or {}).get("first_name", "") or ""
    description = (desc.get("result") or {}).get("description", "") or ""
    short_description = (short.get("result") or {}).get("short_description", "") or ""
    return web.json_response(
        {
            "ok": True,
            "name": name,
            "description": description,
            "short_description": short_description,
        }
    )


async def bot_profile_save_api(request: web.Request):
    """修改机器人显示名称与简介（调用 Telegram Bot API，立即生效）。"""
    admin = _auth(request)
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="请求格式错误")
    token = _bot_token()
    if not token:
        raise web.HTTPBadRequest(text="未配置 BOT_TOKEN")

    updated: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            if "name" in data:
                name = str(data.get("name", "")).strip()
                if not (1 <= len(name) <= 64):
                    raise web.HTTPBadRequest(text="名字需 1~64 个字符")
                async with session.get(
                    f"https://api.telegram.org/bot{token}/setMyName",
                    params={"name": name},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    j = await r.json()
                if not j.get("ok"):
                    raise web.HTTPBadRequest(text=f"改名失败：{j.get('description', '')}")
                updated.append("名字")
            if "description" in data:
                desc = str(data.get("description", "")).strip()
                if len(desc) > 512:
                    raise web.HTTPBadRequest(text="简介不能超过 512 个字符")
                async with session.get(
                    f"https://api.telegram.org/bot{token}/setMyDescription",
                    params={"description": desc},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    j = await r.json()
                if not j.get("ok"):
                    raise web.HTTPBadRequest(text=f"改简介失败：{j.get('description', '')}")
                updated.append("简介")
            if "short_description" in data:
                sd = str(data.get("short_description", "")).strip()
                if len(sd) > 120:
                    raise web.HTTPBadRequest(text="简短说明不能超过 120 个字符")
                async with session.get(
                    f"https://api.telegram.org/bot{token}/setMyShortDescription",
                    params={"short_description": sd},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    j = await r.json()
                if not j.get("ok"):
                    raise web.HTTPBadRequest(text=f"改简短说明失败：{j.get('description', '')}")
                updated.append("简短说明")
    except web.HTTPException:
        raise
    except Exception as e:
        raise web.HTTPBadRequest(text=f"操作失败：{str(e)[:100]}")
    with database.SessionLocal() as session:
        database.log_audit(session, admin, "bot_profile", "、".join(updated) or "（无变化）")
    return web.json_response({"ok": True, "updated": updated})


# ---------------- 操作审计 ----------------

async def audit_api(request: web.Request):
    """后台操作记录（谁在什么时候改了什么）。"""
    _auth(request)
    with database.SessionLocal() as session:
        logs = database.get_audit_logs(session, 100)
    items = [
        {
            "time": database.beijing_str(l.created_at),
            "admin": l.admin,
            "action": l.action,
            "detail": l.detail,
        }
        for l in logs
    ]
    return web.json_response({"ok": True, "items": items})


# ---------------- 洗数据账单 ----------------

async def wash_orders_api(request: web.Request):
    """洗数据流水：充值/扣费/退款，最近 100 条 + 汇总。"""
    _auth(request)
    with database.SessionLocal() as session:
        data = config_api.get_wash_orders(session, 100)
    return web.json_response({"ok": True, **data})


def build_app(bot=None) -> web.Application:
    global _bot
    if bot is not None:
        _bot = bot
    app = web.Application(client_max_size=256 * 1024, middlewares=[guard_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/health", health)
    app.router.add_get("/api/public/feed", public_feed)
    app.router.add_get("/api/public/plans", public_plans)
    app.router.add_get("/api/public/site", public_site)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/config", get_config_api)
    app.router.add_post("/api/config/{key}", save_config_api)
    app.router.add_post("/api/reset/{key}", reset_config_api)
    app.router.add_get("/api/stats", stats_api)
    app.router.add_get("/api/members", members_api)
    app.router.add_post("/api/members/adjust", members_adjust_api)
    app.router.add_get("/api/site", site_api)
    app.router.add_post("/api/site", site_save_api)
    app.router.add_get("/api/bot/profile", bot_profile_api)
    app.router.add_post("/api/bot/profile", bot_profile_save_api)
    app.router.add_get("/api/audit", audit_api)
    app.router.add_get("/api/wash/orders", wash_orders_api)
    return app
