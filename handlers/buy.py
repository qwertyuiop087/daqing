"""购买会员：选套餐 → 下单 → 支付 → 自动开通。"""
import asyncio
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import settings
from database import (
    PaymentOrder,
    SessionLocal,
    add_user_days,
    get_or_create_user,
    get_plans,
    get_user_by_id,
)
from utils.dali_client import get_client
from utils.helpers import cleanup_state
from utils.keyboards import cancel_kb, menu_kb

router = Router(name="buy")


def plans_kb(plans: list[dict]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for p in plans:
        b.button(
            text=f"{p['days']}天 - {p['price']} USDT",
            callback_data=f"buy:plan:{p['days']}:{p['price']}",
        )
    # OKPay 收款入口（配了商户号才显示）
    okpay_ready = False
    try:
        from utils.okaypay_client import is_configured as _okpay_ready
        okpay_ready = _okpay_ready()
    except Exception:
        okpay_ready = False
    if okpay_ready:
        b.button(text="🔵 用 OKPay 支付", callback_data="okpay:plans")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(2)
    return b.as_markup()


@router.callback_query(F.data == "buy:plans")
async def show_plans(cq: CallbackQuery):
    with SessionLocal() as session:
        plans = get_plans(session)
        get_or_create_user(
            session,
            cq.from_user.id,
            cq.from_user.username or "",
            cq.from_user.first_name or "",
        )

    await cq.answer()
    await cq.message.edit_text(
        "💎 <b>购买会员</b>\n\n"
        "选择你要开通的套餐：\n"
        "支付方式：@dlqb 钱包 USDT\n",
        reply_markup=plans_kb(plans),
    )


@router.callback_query(F.data.startswith("buy:plan:"))
async def create_order(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    try:
        days = int(parts[2])
        price = float(parts[3])
    except (ValueError, IndexError):
        await cq.answer("参数错误。", show_alert=True)
        return

    # 服务端定价：回调数据客户端可见可伪造，价格与天数必须以数据库套餐为准
    with SessionLocal() as session:
        plans = get_plans(session)
    plan = next((p for p in plans if p["days"] == days and abs(float(p["price"]) - price) < 1e-9), None)
    if plan is None:
        await cq.answer("套餐不存在或已调整，请重新选择。", show_alert=True)
        return

    client = get_client()
    if not client:
        await cq.answer("支付系统未配置，请联系管理员。", show_alert=True)
        return

    order_id = f"order_{uuid.uuid4().hex[:16]}"
    user_id = cq.from_user.id

    # 调用大力钱包创建支付链接（同步 HTTP 丢线程池，避免卡事件循环）
    try:
        result = await asyncio.to_thread(
            client.call,
            "/merchant/payLink",
            {
                "amount": price,
                "coin": "usdt",
                "userOrder": order_id,
                "name": f"会员{days}天",
            },
        )
    except Exception as e:
        await cq.answer(f"创建支付订单失败，请稍后重试。", show_alert=True)
        return

    if not result.get("ok"):
        await cq.answer("创建支付订单失败，请稍后重试。", show_alert=True)
        return

    pay_data = result["data"]
    txid = pay_data.get("txid", "")
    pay_url = pay_data.get("pay_url", "")

    # 存订单到数据库
    with SessionLocal() as session:
        order = PaymentOrder(
            order_id=order_id,
            user_id=user_id,
            days=days,
            amount=str(price),
            coin="usdt",
            status="pending",
            txid=txid,
            pay_url=pay_url,
        )
        session.add(order)
        session.commit()

    await cq.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 去支付", url=pay_url)
    kb.button(text="✅ 我已支付，查询状态", callback_data=f"buy:check:{order_id}")
    kb.button(text="🔙 返回主菜单", callback_data="menu:home")
    kb.adjust(1)

    await cq.message.edit_text(
        f"💳 <b>订单已创建</b>\n\n"
        f"套餐：会员 {days} 天\n"
        f"金额：{price} USDT\n"
        f"订单号：<code>{order_id}</code>\n\n"
        f"点「去支付」在 @dlqb 钱包内完成付款，\n"
        f"付完后点「我已支付」自动开通。",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("buy:check:"))
async def check_order(cq: CallbackQuery, state: FSMContext):
    order_id = cq.data.split(":")[2]
    client = get_client()

    if not client:
        await cq.answer("支付系统未配置。", show_alert=True)
        return

    with SessionLocal() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.order_id == order_id))
        if not order:
            await cq.answer("订单不存在。", show_alert=True)
            return
        if order.user_id != cq.from_user.id:
            await cq.answer("这不是你的订单。", show_alert=True)
            return
        if order.status == "paid":
            await cq.answer("订单已支付，会员已开通。", show_alert=True)
            return

    # 主动查单（同步 HTTP 丢线程池，避免卡事件循环）
    try:
        result = await asyncio.to_thread(
            client.call, "/merchant/checkDeposit", {"userOrder": order_id}
        )
    except Exception:
        await cq.answer("查询失败，请稍后再试。", show_alert=True)
        return
    if not result.get("ok"):
        await cq.answer("查询失败，请稍后再试。", show_alert=True)
        return

    data = result["data"]
    status_cn = data.get("statusTextCN", "")
    status_text = data.get("statusText", "")

    if status_text != "paid":
        await cq.answer(f"当前状态：{status_cn}，请完成支付后再查询。", show_alert=True)
        return

    # 支付成功：行锁 + 二次确认，与自动到账扫描互斥，防止重复开通
    with SessionLocal() as session:
        locked = session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.order_id == order_id)
            .with_for_update()
        )
        if locked is None or locked.status == "paid":
            await cq.answer("订单已支付。", show_alert=True)
            return
        locked.status = "paid"
        locked.txid = data.get("txid", locked.txid)

        user = get_user_by_id(session, locked.user_id)
        if user:
            add_user_days(session, user, locked.days)
        else:
            # 用户不存在时也要把订单状态提交掉，防止重复点击再次开通
            session.commit()

    await cq.answer(f"支付成功！会员已增加 {locked.days} 天。", show_alert=True)
    await cq.message.edit_text(
        f"✅ <b>支付成功！</b>\n\n"
        f"已为你开通 {locked.days} 天会员。\n"
        f"感谢购买！\n\n"
        f"👉 会员群：@youzui\n"
        f"进群获取最新资源和通知。",
        reply_markup=menu_kb(cq.from_user.id),
        parse_mode="HTML",
    )
