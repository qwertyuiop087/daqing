"""机器人入口：支持 Webhook（Railway 部署推荐）与长轮询（本地调试）两种模式。

内置功能（本文件自带，不依赖额外新模块，避免部署漏文件导致崩溃）：
- 支付自动到账轮询：每 1 秒扫一次待支付订单，已支付自动入账，5 分钟未支付作废；
- 网站/管理后台（可选）：webapp.py 存在则同进程提供 /（介绍页）与 /admin（后台）。
"""
import logging

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

logging.basicConfig(level=logging.INFO)

# ---------------- 支付自动到账（内联在本文件，避免依赖单独模块） ----------------

AUTO_CREDIT_INTERVAL = 1           # 扫描间隔（秒）
ORDER_TIMEOUT_SECONDS = 5 * 60     # 订单 5 分钟内未支付视为无效
ORDER_CHECK_EVERY = 5              # 同一订单最多每 5 秒查一次支付状态
AUTO_CREDIT_MAX_ORDERS = 50        # 每轮最多处理订单数

_last_checked: dict[str, float] = {}


async def _sweep_payments(bot) -> None:
    client = get_client()
    if not client:
        return
    now = time.time()
    timeout_cutoff = utcnow() - timedelta(seconds=ORDER_TIMEOUT_SECONDS)

    with SessionLocal() as session:
        orders = list(
            session.scalars(
                select(PaymentOrder)
                .where(PaymentOrder.status == "pending")
                .order_by(PaymentOrder.id.desc())
                .limit(AUTO_CREDIT_MAX_ORDERS)
            )
        )
    for order in orders:
        # 1) 超时判定：创建超过 5 分钟仍未支付 → 无效
        if order.created_at is not None and order.created_at < timeout_cutoff:
            with SessionLocal() as session:
                fresh = session.scalar(
                    select(PaymentOrder).where(PaymentOrder.order_id == order.order_id)
                )
                if fresh is not None and fresh.status == "pending":
                    fresh.status = "timeout"
                    session.commit()
                    try:
                        await bot.send_message(
                            fresh.user_id,
                            "⚠️ <b>订单已超时</b>\n\n"
                            f"订单号：<code>{fresh.order_id}</code>\n"
                            "5 分钟内未完成支付，订单已失效，请重新下单。",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            continue

        # 2) 节流：同一订单最多每 5 秒查一次
        last = _last_checked.get(order.order_id, 0.0)
        if now - last < ORDER_CHECK_EVERY:
            continue
        _last_checked[order.order_id] = now

        # 3) 查支付状态
        try:
            result = await asyncio.to_thread(
                client.call, "/merchant/checkDeposit", {"userOrder": order.order_id}
            )
        except Exception:
            continue
        if not result.get("ok"):
            continue
        data = result.get("data", {})
        if data.get("statusText") != "paid":
            continue
        txid = data.get("txid", "") or ""
        amount = float(data.get("amount", 0) or 0)

        # 4) 自动入账
        notify_text = None
        with SessionLocal() as session:
            fresh = session.scalar(
                select(PaymentOrder).where(PaymentOrder.order_id == order.order_id)
            )
            if fresh is None or fresh.status == "paid":
                continue
            fresh.status = "paid"
            fresh.txid = txid or fresh.txid
            if fresh.days > 0:
                user = session.get(User, fresh.user_id)
                if user is not None:
                    add_user_days(session, user, fresh.days)
                session.commit()
                notify_text = (
                    "🎉 <b>支付成功，会员已自动到账！</b>\n\n"
                    f"套餐：<b>{fresh.days}</b> 天会员\n"
                    f"金额：{fresh.amount} USDT\n\n"
                    "感谢购买，祝你使用愉快～"
                )
            else:
                rate = await get_usdt_rate_async(session)
                rmb = amount * rate
                add_wash_balance(session, fresh.user_id, rmb)
                balance = get_wash_balance(session, fresh.user_id)
                session.add(
                    WashOrder(
                        user_id=fresh.user_id,
                        filename=f"充值 ${amount} USDT（自动到账）",
                        row_count=0,
                        cost=rmb,
                        type="recharge",
                    )
                )
                session.commit()
                notify_text = (
                    "💰 <b>洗数据充值已自动到账！</b>\n\n"
                    f"充值金额：${amount} USDT\n"
                    f"换算汇率：1 USDT = {rate} 元\n"
                    f"到账余额：¥{rmb:.4f} 元\n"
                    f"当前余额：¥{balance:.4f} 元"
                )
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

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_all_routers(dp)

    # 启动小号客户端（洗数据中转）
    from utils.telethon_client import start_userbot
    asyncio.create_task(start_userbot(bot))

    # 启动支付自动到账轮询（本文件自带，不依赖额外文件）
    asyncio.create_task(start_auto_credit(bot))

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

        await bot.set_webhook(
            url=f"{settings.WEBHOOK_DOMAIN}{settings.WEBHOOK_PATH}",
            secret_token=settings.WEBHOOK_SECRET or None,
            allowed_updates=dp.resolve_used_update_types(),
        )
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=settings.WEBHOOK_SECRET or None
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
