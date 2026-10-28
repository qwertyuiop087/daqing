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
    days = int(parts[2])
    price = float(parts[3])

    client = get_client()
    if not client:
        await cq.answer("支付系统未配置，请联系管理员。", show_alert=True)
        return

    order_id = f"order_{uuid.uuid4().hex[:16]}"
    user_id = cq.from_user.id

    # 调用大力钱包创建支付链接（同步 HTTP 丢线程池，避免卡事件循环）
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

        if order.status == "paid":
            await cq.answer(f"✅ 订单已支付，会员已开通！", show_alert=True)
            return

        # 主动查单（同步 HTTP 丢线程池，避免卡事件循环）
        result = await asyncio.to_thread(
            client.call, "/merchant/checkDeposit", {"userOrder": order_id}
        )
        if not result.get("ok"):
            await cq.answer("查询失败，请稍后再试。", show_alert=True)
            return

        data = result["data"]
        status_cn = data.get("statusTextCN", "")
        status_text = data.get("statusText", "")

        if status_text == "paid":
            # 支付成功，开通会员；先落订单状态保证幂等
            order.status = "paid"
            order.txid = data.get("txid", order.txid)

            user = get_user_by_id(session, order.user_id)
            if user:
                add_user_days(session, user, order.days)
            else:
                # 用户不存在时也要把订单状态提交掉，防止重复点击再次开通
                session.commit()

            await cq.answer(f"🎉 支付成功！会员已增加 {order.days} 天。", show_alert=True)
            await cq.message.edit_text(
                f"✅ <b>支付成功！</b>\n\n"
                f"已为你开通 {order.days} 天会员。\n"
                f"感谢购买！\n\n"
                f"👉 会员群：@youzui\n"
                f"进群获取最新资源和通知。",
                reply_markup=menu_kb(cq.from_user.id),
                parse_mode="HTML",
            )
        else:
            await cq.answer(f"当前状态：{status_cn}，请完成支付后再查询。", show_alert=True)
