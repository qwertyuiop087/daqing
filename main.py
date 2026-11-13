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

# ---------------- 支付自动到账（内联在本文件，避免依赖单独模块） ----------------

AUTO_CREDIT_INTERVAL = 1           # 扫描间隔（秒）
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
TEMP_KEEP_SECONDS = 24 * 3600     # 只清理 24 小时前的


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
        await asyncio.sleep(TEMP_CLEANUP_INTERVAL)
        try:
            await asyncio.to_thread(_cleanup_stale_tempdirs)
        except Exception as e:
            logging.warning("⚠️ 临时目录清理异常：%s", e)
        try:
            await asyncio.to_thread(_cleanup_stale_config_keys)
        except Exception as e:
            logging.warning("⚠️ 配置键清理异常：%s", e)


# ---------------- 主流程 ----------------

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
    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession(timeout=float(getattr(settings, "HTTP_TIMEOUT", 30) or 30))
    bot = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_all_routers(dp)

    # 启动小号客户端（洗数据中转）
    from utils.telethon_client import start_userbot
    asyncio.create_task(start_userbot(bot))

    # 启动支付自动到账轮询（本文件自带，不依赖额外文件）
    asyncio.create_task(start_auto_credit(bot))

    # 启动废弃临时目录清理（每小时一次，删除 24 小时前的）
    asyncio.create_task(start_temp_cleanup())

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
        await bot.set_webhook(
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
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(dp.start_polling(bot))

    runner = web.AppRunner(app, access_log=None)  # 关闭访问日志，避免被刷时刷爆日志
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.PORT)
    await site.start()
    logging.info("网站/后台已启动：http://0.0.0.0:%s （/ 介绍页，/admin 后台）", settings.PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
