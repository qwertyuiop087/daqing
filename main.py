"""机器人入口：支持 Webhook（Railway 部署推荐）与长轮询（本地调试）两种模式。

内置功能（本文件自带，不依赖额外新模块，避免部署漏文件导致崩溃）：
- 支付自动到账轮询：每 1 秒扫一次待支付订单，已支付自动入账，5 分钟未支付作废；
- 网站/管理后台（可选）：webapp.py 存在则同进程提供 /（介绍页）与 /admin（后台）。
"""
import logging
import os
import secrets

import asyncio
import time
from datetime import timedelta

from aiohttp import web
from sqlalchemy import select

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from config import settings
from database import (
    PaymentOrder,
    SessionLocal,
    User,
    WashOrder,
    add_user_days,
    add_wash_balance,
    get_usdt_rate_async,
    get_wash_balance,
    init_db,
    utcnow,
)
from handlers import register_all_routers
from utils.dali_client import get_client
from utils.pay_credit import credit_order

logging.basicConfig(level=logging.INFO)


# ---------------- Telegram 连接加固 ----------------
# 问题现象（2026-11 排查）：
#   1) 普通 API 调用偶发挂满 HTTP_TIMEOUT 才失败，报 "Request timeout error"；
#   2) 用户上传的文件必定「下载失败：HTTP Client says - Request timeout error」。
# 根因有两层：
#   a) aiogram 的 Bot.download_file 默认 timeout=30 秒，而且是「整个下载」的总时长
#      （aiohttp ClientTimeout(total=30)）。20MB 文件要求 5.3Mbps 稳定带宽，
#      链路一抖就必然失败 —— 这是 (2) 的直接原因。
#   b) Railway 的 NAT 会回收空闲 TCP，aiohttp 连接池却还拿着死连接复用；
#      另外容器只有 IPv4 出口，DNS 若返回 AAAA，aiohttp 会先连 IPv6 黑洞。
import socket as _socket

import aiohttp as _aiohttp
from aiogram.client.session.aiohttp import AiohttpSession as _AiohttpSession


class TgSession(_AiohttpSession):
    """强制 IPv4、禁用连接复用（绕开 NAT 死连接 / IPv6 黑洞）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        init = getattr(self, "_connector_init", None)
        if isinstance(init, dict):  # aiogram 3.2x 的连接池参数，直接改它
            init.update(limit=16, force_close=True, family=_socket.AF_INET)


class TgBot(Bot):
    """文件下载：超时 30 秒 → 120 秒，失败重试一次，并把字节数写进日志。"""

    async def download_file(
        self,
        file_path,
        destination=None,
        timeout: int = 120,
        chunk_size: int = 65536,
        seek: bool = True,
    ):
        last_exc: Exception | None = None
        for attempt in (1, 2):
            t0 = time.perf_counter()
            try:
                result = await super().download_file(
                    file_path,
                    destination=destination,
                    timeout=timeout,
                    chunk_size=chunk_size,
                    seek=seek,
                )
                logging.info(
                    "⬇️ 文件下载完成 %.1fs（第 %d 次尝试）",
                    time.perf_counter() - t0,
                    attempt,
                )
                return result
            except Exception as e:  # noqa: BLE001
                last_exc = e
                size = -1
                try:
                    if isinstance(destination, (str, os.PathLike)):
                        size = os.path.getsize(destination)
                except OSError:
                    pass
                # 关键诊断信息：失败时已写入多少字节
                #   0 字节      → 连接根本没建立起来（死连接 / IPv6 黑洞）
                #   几 MB       → 带宽不够，属于链路慢
                logging.warning(
                    "⬇️ 文件下载失败 %.1fs（第 %d 次），已写入 %s 字节：%s",
                    time.perf_counter() - t0,
                    attempt,
                    size,
                    e,
                )
                if attempt == 1:
                    await asyncio.sleep(1)
        raise last_exc

# ---------------- 支付自动到账（内联在本文件，避免依赖单独模块） ----------------

AUTO_CREDIT_INTERVAL = 3           # 扫描间隔（秒）：1 秒一次等于每天 8.6 万次查询，没必要
ORDER_TIMEOUT_SECONDS = 24 * 3600  # 链上确认/渠道延迟很常见，5 分钟就判死会让用户付钱拿不到货     # 订单 5 分钟内未支付视为无效
ORDER_CHECK_EVERY = 5              # 同一订单最多每 5 秒查一次支付状态
AUTO_CREDIT_MAX_ORDERS = 50        # 每轮最多处理订单数

_last_checked: dict[str, float] = {}


async def _query_paid(order) -> tuple[bool, float, str]:
    """查单：返回 (是否已支付, USDT 金额, txid)。按订单渠道自动选择。"""
    if (order.coin or "").lower().startswith("okpay"):
        from utils import okaypay_client as okpay

        shop_id, token, _cb = okpay.get_config_values()
        if not (shop_id and token):
            return False, 0.0, ""
        try:
            data = await asyncio.to_thread(okpay.check_deposit, shop_id, token, order.order_id)
        except Exception as e:
            logging.warning("⚠️ OKPay 查单失败 %s：%s", order.order_id, e)
            return False, 0.0, ""
        if str(data.get("status", "0")) != "1":
            return False, 0.0, ""
        try:
            amount = float(data.get("amount") or order.amount or 0)
        except Exception:
            amount = float(order.amount or 0)
        return True, amount, str(data.get("order_id") or "")

    client = get_client()
    if not client:
        return False, 0.0, ""
    try:
        result = await asyncio.to_thread(
            client.call, "/merchant/checkDeposit", {"userOrder": order.order_id}
        )
    except Exception:
        return False, 0.0, ""
    if not result.get("ok"):
        return False, 0.0, ""
    data = result.get("data") or {}
    if data.get("statusText") != "paid":
        return False, 0.0, ""
    try:
        amount = float(data.get("amount") or 0)
    except Exception:
        amount = 0.0
    if amount <= 0:
        # 渠道没给金额：回退到订单金额，避免"付了钱按 0 入账"
        try:
            amount = float(order.amount or 0)
        except Exception:
            amount = 0.0
    return True, amount, data.get("txid", "") or ""


async def _sweep_payments(bot) -> None:
    now = time.time()
    timeout_cutoff = utcnow() - timedelta(seconds=ORDER_TIMEOUT_SECONDS)

    with SessionLocal() as session:
        orders = list(
            session.scalars(
                select(PaymentOrder)
                .where(PaymentOrder.status == "pending")
                .order_by(PaymentOrder.id.asc())  # 最旧的先处理，避免大量订单时饿死
                .limit(AUTO_CREDIT_MAX_ORDERS)
            )
        )
    for order in orders:
        expired = order.created_at is not None and order.created_at < timeout_cutoff

        # 节流：同一订单最多每 5 秒查一次（过期订单不节流，直接查最后一次）
        if not expired:
            last = _last_checked.get(order.order_id, 0.0)
            if now - last < ORDER_CHECK_EVERY:
                continue
            _last_checked[order.order_id] = now

        paid, amount, txid = await _query_paid(order)

        # 1) 超时且未付款：标记失效并提醒用户
        if not paid:
            if expired:
                with SessionLocal() as session:
                    fresh = session.scalar(
                        select(PaymentOrder)
                        .where(PaymentOrder.order_id == order.order_id)
                        .with_for_update()
                    )
                    if fresh is not None and fresh.status == "pending":
                        fresh.status = "timeout"
                        session.commit()
                        try:
                            await bot.send_message(
                                fresh.user_id,
                                "⚠️ <b>订单已超时</b>\n\n"
                                f"订单号：<code>{fresh.order_id}</code>\n"
                                "24 小时内未完成支付，订单已失效，请重新下单。\n（已付款但刚确认的订单会自动补上，无需担心）",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
            continue

        # 2) 已付款：行锁 + 幂等入账（与「我已支付」按钮互斥）
        notify_text = None
        # 汇率是网络请求，必须在进入行锁事务之前取好：
        # 在事务里 await 外部 HTTP 会长时间持锁，SQLite 下 FOR UPDATE 又形同虚设，
        # 容易导致同一订单被并发入账两次。
        with SessionLocal() as session:
            rate = await get_usdt_rate_async(session)
        with SessionLocal() as session:
            fresh = session.scalar(
                select(PaymentOrder)
                .where(PaymentOrder.order_id == order.order_id)
                .with_for_update()
            )
            if fresh is None or fresh.status == "paid":
                continue
            notify_text = credit_order(session, fresh, amount, rate, txid)
            if notify_text is None:
                continue
            session.commit()

        if notify_text:
            try:
                await bot.send_message(fresh.user_id, notify_text, parse_mode="HTML")
            except Exception:
                pass
        await asyncio.sleep(0.2)

    if len(_last_checked) > 500:
        for k in [k for k, v in _last_checked.items() if now - v > 600]:
            _last_checked.pop(k, None)


async def start_auto_credit(bot) -> None:
    """后台循环：每隔 AUTO_CREDIT_INTERVAL 秒扫一次待支付订单。"""
    while True:
        await asyncio.sleep(AUTO_CREDIT_INTERVAL)
        try:
            await _sweep_payments(bot)
        except Exception as e:
            logging.warning("⚠️ 自动到账扫描异常：%s", e)


# ---------------- 废弃临时目录清理 ----------------

TEMP_CLEANUP_INTERVAL = 3600      # 每小时一次
TEMP_KEEP_SECONDS = 6 * 3600      # 只保留 6 小时（原来是 24 小时，20MB 文件堆一天容易把磁盘写满）


def _cleanup_stale_config_keys() -> None:
    """清理过期映射（phone_check_code:* 无限增长防护）。

    该表没有时间戳列，采用保守策略：只在总量异常多（>10 万条）时
    删除最早的一批，防止查询退化和数据库膨胀；日常量级下不误删。
    """
    from database import AppConfig, SessionLocal
    with SessionLocal() as session:
        rows = list(session.scalars(
            select(AppConfig).where(AppConfig.key.like("phone_check_code:%"))
        ))
        total = len(rows)
        if total > 100000:
            removed = 0
            for row in rows[: total - 90000]:
                session.delete(row)
                removed += 1
            session.commit()
            logging.info("🧹 已清理 %d 条过期空号检测映射", removed)


def _cleanup_stale_tempdirs() -> None:
    """删除系统临时目录里 24 小时前的 bot_ 工作目录（用户操作中断留下的）。"""
    import shutil
    import tempfile

    root = tempfile.gettempdir()
    cutoff = time.time() - TEMP_KEEP_SECONDS
    removed = 0
    try:
        for name in os.listdir(root):
            if not name.startswith("bot_"):
                continue
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    if removed:
        logging.info("🧹 已清理 %d 个废弃临时目录", removed)


async def start_temp_cleanup() -> None:
    """每小时清理一次：废弃临时目录 + 过期的空号检测映射。"""
    while True:
        # 先清一次再睡：重启那一刻就把上次留下的垃圾收掉，避免磁盘被历史文件占满
        try:
            await asyncio.to_thread(_cleanup_stale_tempdirs)
        except Exception as e:
            logging.warning("⚠️ 临时目录清理异常：%s", e)
        try:
            await asyncio.to_thread(_cleanup_stale_config_keys)
        except Exception as e:
            logging.warning("⚠️ 配置键清理异常：%s", e)
        await asyncio.sleep(TEMP_CLEANUP_INTERVAL)


# ---------------- 主流程 ----------------

async def _set_webhook_with_retry(
    bot, url: str, secret_token: str, allowed_updates, attempts: int = 8
) -> bool:
    """设置 webhook，失败自动重试。

    网络抖动时 set_webhook 会抛 TelegramNetworkError / TelegramServerError，
    以前这里没有重试，异常直接退出进程 → Railway 反复重启 → 死循环。
    """
    from aiogram.exceptions import TelegramNetworkError, TelegramServerError

    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            await bot.set_webhook(
                url=url, secret_token=secret_token, allowed_updates=allowed_updates
            )
            logging.info("✅ Webhook 已设置：%s", url)
            return True
        except (TelegramNetworkError, TelegramServerError) as e:
            logging.warning(
                "set_webhook 失败（%d/%d）：%s，%.0fs 后重试",
                attempt,
                attempts,
                e,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30)
    logging.error("❌ set_webhook 连续失败 %d 次，服务继续运行但暂时收不到消息", attempts)
    return False


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


async def _diag_handler(message: Message) -> None:
    """管理员自检命令 /diag：一条命令看完所有关键状态。"""
    from utils.roles import admin_id_list

    if message.from_user is None or message.from_user.id not in admin_id_list():
        return

    bot = message.bot
    parts: list[str] = ["🩺 <b>自检报告</b>", ""]

    # Telegram 连通性 + 延迟
    t0 = time.perf_counter()
    try:
        await bot.get_me()
        parts.append(f"· Telegram：正常 {(time.perf_counter() - t0) * 1000:.0f} ms")
    except Exception as e:
        parts.append(f"· Telegram：<b>异常</b> {type(e).__name__}: {str(e)[:60]}")

    # 数据库
    t0 = time.perf_counter()
    try:
        with SessionLocal() as s:
            s.execute(select(User).limit(1))
        parts.append(f"· 数据库：正常 {(time.perf_counter() - t0) * 1000:.0f} ms")
    except Exception as e:
        parts.append(f"· 数据库：<b>异常</b> {type(e).__name__}: {str(e)[:60]}")

    # 小号（洗数据中转）
    try:
        from database import get_config as _gc
        from utils.telethon_client import userbot as _ub

        with SessionLocal() as s:
            alt_id = _gc(s, "alt_user_id", "") or "未记录"
            alt_status = _gc(s, "alt_status", "unknown")
        if _ub is not None and _ub.is_connected():
            parts.append(f"· 小号：在线（{alt_id}）")
        else:
            parts.append(f"· 小号：<b>离线</b>（状态 {alt_status}，ID {alt_id}）")
    except Exception as e:
        parts.append(f"· 小号：检测失败 {type(e).__name__}")

    # 磁盘（满了会导致所有文件下载失败）
    try:
        import tempfile as _tf
        import shutil as _sh

        u = _sh.disk_usage(_tf.gettempdir())
        parts.append(f"· 磁盘：剩余 {_fmt_bytes(u.free)} / 共 {_fmt_bytes(u.total)}")
    except Exception:
        pass

    # 待支付订单
    try:
        with SessionLocal() as s:
            n = len(
                list(
                    s.scalars(
                        select(PaymentOrder)
                        .where(PaymentOrder.status == "pending")
                        .limit(1000)
                    )
                )
            )
        parts.append(f"· 待支付订单：{n}")
    except Exception:
        pass

    parts.append(f"· 服务器时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    await message.answer("\n".join(parts), parse_mode=ParseMode.HTML)


async def _net_selftest(bot) -> None:
    """启动时连打 3 次 getMe，把耗时写进日志：用于判断链路是否稳定。

    正常应该 100~800 ms；如果出现接近超时（>5 秒）或失败，
    说明是网络层问题，不是业务代码问题。
    """
    for i in range(3):
        t0 = time.perf_counter()
        try:
            await bot.get_me()
            logging.info(
                "🛰 自检 getMe #%d 成功 %.0f ms",
                i + 1,
                (time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            logging.warning(
                "🛰 自检 getMe #%d 失败 %.0f ms：%s",
                i + 1,
                (time.perf_counter() - t0) * 1000,
                e,
            )
        await asyncio.sleep(1)

async def start_refund_notices(bot) -> None:
    """补发「洗数据超时退款」通知。

    退款本身是在 database.get_washed_breakdown() 里做的：那是同步的数据库层，
    拿不到 bot，所以只把通知落库（refund_notice:*），这里负责发出去并清理。
    """
    while True:
        await asyncio.sleep(20)
        try:
            from database import AppConfig, SessionLocal, delete_config

            with SessionLocal() as session:
                rows = list(
                    session.scalars(
                        select(AppConfig)
                        .where(AppConfig.key.like("refund_notice:%"))
                        .limit(20)
                    )
                )
                items = [(r.key or "", r.value or "") for r in rows]

            for key, value in items:
                try:
                    uid_s, amount_s, lines_s, code = (value.split(":", 3) + ["", "", "", ""])[:4]
                    uid = int(uid_s)
                    text = (
                        "💸 <b>洗数据超时退款</b>\n\n"
                        f"任务编号：<code>{code}</code>\n"
                        f"未回传行数：{int(lines_s or 0):,} 行\n"
                        f"退款金额：¥{float(amount_s or 0):.4f}\n\n"
                        "48 小时内没有收到洗数据结果，已按未回传部分退款到洗数据余额。"
                    )
                    await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
                    logging.info("💸 已发送超时退款通知：用户 %s 任务 %s", uid, code)
                except Exception as e:
                    logging.warning("退款通知发送失败（%s）：%s", key, e)
                # 不论成功失败都删掉，避免坏数据反复重试刷屏
                try:
                    with SessionLocal() as session:
                        delete_config(session, key)
                except Exception:
                    pass
        except Exception as e:
            logging.warning("退款通知扫描异常：%s", e)


async def main() -> None:
    if not settings.BOT_TOKEN:
        raise SystemExit(
            "缺少 BOT_TOKEN 环境变量。请参考 .env.example 配置后再启动。"
        )

    init_db()

    # 启动时加载按钮图标配置到内存，避免每次构建菜单都查数据库
    from utils.keyboards import load_btn_icons
    load_btn_icons()

    # 显式设置连接超时：跨境网络抖动时不要让请求无限期挂着，
    # 超时后 aiogram 会抛 TelegramNetworkError，轮询/webhook 会自动重试。
    # 超时从 30 秒下调到 15 秒：挂死的请求尽快失败，用户等待时间减半。
    http_timeout = float(getattr(settings, "HTTP_TIMEOUT", 15) or 15)
    session = TgSession(timeout=http_timeout)
    bot = TgBot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # 一次性的返佣默认值迁移：把旧默认（洗数据 5% / 会员 10%）改成 3% / 5%
    try:
        from database import SessionLocal as _SessionLocal
        from database import migrate_referral_defaults

        with _SessionLocal() as _s:
            _changed = migrate_referral_defaults(_s)
        if _changed:
            logging.info("返佣默认值已更新：%s", "；".join(_changed))
    except Exception as e:
        logging.warning("返佣默认值迁移失败：%s", e)

    dp = Dispatcher(storage=MemoryStorage())
    register_all_routers(dp)

    # 管理员自检命令：/diag（一条命令看完 Telegram/DB/小号/磁盘）
    from aiogram.filters import Command as _Command

    dp.message.register(_diag_handler, _Command("diag", "诊断"))

    # 启动自检：把 getMe 的耗时打进日志，方便判断是不是网络层在拖
    asyncio.create_task(_net_selftest(bot))

    # 记录机器人用户名：邀请链接要用它拼 t.me/xxx?start=ref_<id>
    try:
        me = await bot.get_me()
        if me and me.username:
            from database import SessionLocal, set_config

            with SessionLocal() as _s:
                set_config(_s, "bot_username", me.username)
            logging.info("机器人用户名：@%s", me.username)
    except Exception as e:
        logging.warning("获取机器人用户名失败：%s", e)

    # 启动小号客户端（洗数据中转）
    from utils.telethon_client import start_userbot
    asyncio.create_task(start_userbot(bot))

    # 启动会员到期提醒 + 每日对账日报
    from utils.reminders import start_reminders
    asyncio.create_task(start_reminders(bot))

    # 启动支付自动到账轮询（本文件自带，不依赖额外文件）
    asyncio.create_task(start_auto_credit(bot))

    # 启动废弃临时目录清理（每小时一次，删除 24 小时前的）
    asyncio.create_task(start_temp_cleanup())

    # 启动超时退款通知补发（退款在数据库层静默发生，这里负责告知用户）
    asyncio.create_task(start_refund_notices(bot))

    # 启动洗数据结果补发：每分钟扫一次洗数据群最近 30 分钟的消息，
    # 把「已经处理好但没送到用户手上」的结果文件补发过去
    from utils.wash_resend import start_wash_resend
    asyncio.create_task(start_wash_resend(bot))

    # 全局异常兜底：记录日志并通知管理员（节流：30 分钟最多一条）
    _err_notify_state = {"ts": 0.0}

    @dp.errors()
    async def _on_global_error(event):
        exc = event.exception
        # 用户连点按钮 / 点开很久以前的按钮这类情况，Telegram 会返回 400，
        # 属于正常现象，不记录也不打扰管理员
        from utils.helpers import is_benign_update_error, is_network_error
        if is_benign_update_error(exc):
            logging.debug("忽略用户操作类异常：%s", exc)
            return
        if is_network_error(exc):
            # 连不上 Telegram（连接被重置/超时）：不是代码问题，
            # 30 分钟最多提示一次，避免网络抖动时刷屏。
            logging.warning("网络异常：%s", exc)
            now = time.time()
            if now - _err_notify_state["ts"] < 1800:
                return
            _err_notify_state["ts"] = now
            from utils.roles import admin_id_list

            for uid in admin_id_list():
                try:
                    await bot.send_message(
                        uid,
                        "🌐 <b>网络异常（不是程序故障）</b>\n\n"
                        "服务器暂时连不上 Telegram（连接被重置或超时）。\n"
                        "消息收发可能延迟，机器人会自动重试；"
                        "如果持续超过 10 分钟，建议检查服务器网络。",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            return
        logging.error("未处理异常：%s", exc, exc_info=(type(exc), exc, exc.__traceback__))
        now = time.time()
        if now - _err_notify_state["ts"] < 1800:
            return
        _err_notify_state["ts"] = now
        from utils.roles import admin_id_list
        for uid in admin_id_list():
            try:
                await bot.send_message(
                    uid,
                    f"⚠️ <b>机器人出现异常</b>\n\n"
                    f"类型：{type(exc).__name__}\n"
                    f"详情：{str(exc)[:300]}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    # 内置网站 + 管理后台（可选模块：缺失时机器人照常运行，仅网站/后台不启用）
    try:
        from webapp import build_app
        app = build_app(bot)
    except ImportError:
        logging.warning("⚠️ 缺少 webapp.py（代码没传全），网站/后台未启用。请重新完整上传代码。")
        app = web.Application()

    if settings.WEBHOOK_DOMAIN:
        # ---- Webhook 模式（Railway 部署推荐） ----
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        # webhook 密钥：未配置时自动生成随机值，防止任何人伪造 update 打我们的接口。
        # set_webhook 与请求校验用的是同一份值，双方都能对上，无需额外配置。
        webhook_secret = settings.WEBHOOK_SECRET or secrets.token_hex(32)
        await _set_webhook_with_retry(
            bot,
            url=f"{settings.WEBHOOK_DOMAIN}{settings.WEBHOOK_PATH}",
            secret_token=webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=webhook_secret
        ).register(app, path=settings.WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        logging.info("Webhook 模式：%s%s（端口 %s）", settings.WEBHOOK_DOMAIN, settings.WEBHOOK_PATH, settings.PORT)
    else:
        # ---- 长轮询模式（本地调试） ----
        logging.info("长轮询模式启动")
        # drop_pending_updates=True 会把重启期间积压的消息直接丢掉（不是延迟，是消失）。
        # 这里改成 False：重启后先把积压的 update 处理完，用户不会「发了没反应」。
        await bot.delete_webhook(drop_pending_updates=False)
        asyncio.create_task(dp.start_polling(bot))

    runner = web.AppRunner(app, access_log=None)  # 关闭访问日志，避免被刷时刷爆日志
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.PORT)
    await site.start()
    logging.info("网站/后台已启动：http://0.0.0.0:%s （/ 介绍页，/admin 后台）", settings.PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
