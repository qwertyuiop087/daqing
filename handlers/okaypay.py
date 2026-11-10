"""OKPay 收款：充值洗数据余额 / 购买会员。

文档：https://docs.okaypay.me/doc.html
- 下单：POST /shop/payLink（amount / coin=USDT / unique_id / callback_url）
- 到账：平台回调 /okpay/callback（验签入账）+ 定时查单兜底（main._sweep_payments）
- 用户侧：下完单点「去支付」跳转 OKPay，点「我已支付」立即查单
"""
from __future__ import annotations

import asyncio
import math
import uuid

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from database import (
    PaymentOrder,
    SessionLocal,
    get_plans,
    get_usdt_rate_async,
    get_wash_balance,
)
from states import OkayPayStates
from utils import okaypay_client as okpay
from utils.helpers import cleanup_state
from utils.keyboards import cancel_kb, menu_kb
from utils.pay_credit import credit_order

router = Router(name="okpay")

MIN_TOPUP = 5.0  # 最少充值 5 USDT


def okpay_enabled() -> bool:
    """商户号与密钥都配好了才显示入口。"""
    return okpay.is_configured()


def _pay_kb(pay_url: str, order_id: str, back: str = "menu:home") -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 去支付", url=pay_url)
    kb.button(text="✅ 我已支付，查询状态", callback_data=f"okpay:check:{order_id}")
    kb.button(text="🔙 返回", callback_data=back)
    kb.adjust(1)
    return kb


async def _create_order(
    user_id: int,
    amount_raw: str,
    coin: str = "USDT",
    days: int = 0,
    name: str = "",
) -> tuple[str, str, str]:
    """创建 OKPay 订单，返回 (order_id, pay_url, amount_raw)。失败抛异常。"""
    shop_id, token, callback_url = okpay.get_config_values()
    if not (shop_id and token):
        raise RuntimeError("OKPay 未配置")

    order_id = f"okpay_{uuid.uuid4().hex[:16]}"
    data = await asyncio.to_thread(
        okpay.pay_link,
        shop_id,
        token,
        amount_raw,
        coin,
        order_id,
        name,
        callback_url,
    )
    pay_url = str(data.get("pay_url") or "")
    if not pay_url:
        raise RuntimeError("OKPay 未返回支付链接")

    with SessionLocal() as session:
        session.add(
            PaymentOrder(
                order_id=order_id,
                user_id=user_id,
                days=days,
                amount=str(amount_raw),
                coin="okpay",
                status="pending",
                txid=str(data.get("order_id") or ""),
                pay_url=pay_url,
            )
        )
        session.commit()
    return order_id, pay_url, str(amount_raw)


# ---------------- 洗数据充值 ----------------

@router.callback_query(F.data == "okpay:topup")
async def okpay_topup_start(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    if not okpay_enabled():
        await cq.answer("OKPay 未配置，请联系管理员。", show_alert=True)
        return
    await state.set_state(OkayPayStates.waiting_amount)
    await cq.message.edit_text(
        "🔵 <b>OKPay 充值洗数据余额</b>\n\n"
        f"请输入要充值的金额（USDT，最少 {MIN_TOPUP:g}）：\n\n"
        "付款在 OKPay 机器人里完成，付完自动到账。",
        reply_markup=cancel_kb(),
    )


@router.message(OkayPayStates.waiting_amount, F.text)
async def okpay_topup_amount(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    try:
        amount = float(raw)
    except Exception:
        await message.answer("金额格式错误，请输入数字，例如：10")
        return
    if not math.isfinite(amount) or amount < MIN_TOPUP:
        await message.answer(f"最少充值 {MIN_TOPUP:g} USDT，请重新输入：")
        return

    await cleanup_state(state)
    try:
        order_id, pay_url, amount_raw = await _create_order(
            message.from_user.id, raw, "USDT", 0, f"洗数据充值 ${raw}"
        )
    except Exception as e:
        await message.answer(f"⚠️ 创建支付订单失败：{str(e)[:120]}", reply_markup=menu_kb(message.from_user.id))
        return

    await message.answer(
        "🔵 <b>OKPay 订单已创建</b>\n\n"
        f"充值金额：${amount_raw} USDT\n"
        f"订单号：<code>{order_id}</code>\n\n"
        "点「去支付」在 OKPay 机器人里完成付款，付完点「我已支付」自动到账。",
        reply_markup=_pay_kb(pay_url, order_id, "menu:wash").as_markup(),
    )


# ---------------- 购买会员 ----------------

@router.callback_query(F.data == "okpay:plans")
async def okpay_plans(cq: CallbackQuery):
    await cq.answer()
    if not okpay_enabled():
        await cq.answer("OKPay 未配置，请联系管理员。", show_alert=True)
        return
    with SessionLocal() as session:
        plans = get_plans(session)

    kb = InlineKeyboardBuilder()
    for p in plans:
        kb.button(
            text=f"{p['days']}天 - {p['price']} USDT",
            callback_data=f"okpay:buy:{p['days']}:{p['price']}",
        )
    kb.button(text="🔙 返回", callback_data="buy:plans")
    kb.adjust(2)

    await cq.message.edit_text(
        "🔵 <b>OKPay 购买会员</b>\n\n选择套餐：\n付款在 OKPay 机器人里完成，付完自动开通。",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("okpay:buy:"))
async def okpay_buy(cq: CallbackQuery):
    parts = cq.data.split(":")
    try:
        days = int(parts[2])
        price = float(parts[3])
    except (ValueError, IndexError):
        await cq.answer("参数错误。", show_alert=True)
        return

    # 服务端定价：以数据库套餐为准，防止回调数据被篡改
    with SessionLocal() as session:
        plans = get_plans(session)
    plan = next(
        (p for p in plans if p["days"] == days and abs(float(p["price"]) - price) < 1e-9),
        None,
    )
    if plan is None:
        await cq.answer("套餐不存在或已调整，请重新选择。", show_alert=True)
        return
    if not okpay_enabled():
        await cq.answer("OKPay 未配置，请联系管理员。", show_alert=True)
        return

    amount_raw = str(plan["price"])
    try:
        order_id, pay_url, amount_raw = await _create_order(
            cq.from_user.id, amount_raw, "USDT", days, f"会员{days}天"
        )
    except Exception as e:
        await cq.answer(f"创建支付订单失败：{str(e)[:100]}", show_alert=True)
        return

    await cq.answer()
    await cq.message.edit_text(
        "🔵 <b>OKPay 订单已创建</b>\n\n"
        f"套餐：会员 {days} 天\n"
        f"金额：{amount_raw} USDT\n"
        f"订单号：<code>{order_id}</code>\n\n"
        "点「去支付」在 OKPay 机器人里完成付款，付完点「我已支付」自动开通。",
        reply_markup=_pay_kb(pay_url, order_id, "buy:plans").as_markup(),
    )


# ---------------- 查单到账 ----------------

@router.callback_query(F.data.startswith("okpay:check:"))
async def okpay_check(cq: CallbackQuery, bot: Bot):
    order_id = cq.data.split(":")[2]
    shop_id, token, _cb = okpay.get_config_values()
    if not (shop_id and token):
        await cq.answer("OKPay 未配置。", show_alert=True)
        return

    with SessionLocal() as session:
        order = session.scalar(select(PaymentOrder).where(PaymentOrder.order_id == order_id))
        if order is None:
            await cq.answer("订单不存在。", show_alert=True)
            return
        if order.user_id != cq.from_user.id:
            await cq.answer("这不是你的订单。", show_alert=True)
            return
        if order.status == "paid":
            await cq.answer("✅ 该订单已到账，请勿重复点击。", show_alert=True)
            return
        amount_fallback = float(order.amount or 0)

    try:
        data = await asyncio.to_thread(okpay.check_deposit, shop_id, token, order_id)
    except Exception as e:
        await cq.answer(f"查询失败，请稍后再试。", show_alert=True)
        return

    if str(data.get("status", "0")) != "1":
        await cq.answer("当前还未支付，请完成支付后再查询。", show_alert=True)
        return

    try:
        amount = float(data.get("amount") or amount_fallback)
    except Exception:
        amount = amount_fallback

    notify_text = None
    with SessionLocal() as session:
        locked = session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.order_id == order_id)
            .with_for_update()
        )
        if locked is None:
            await cq.answer("订单不存在。", show_alert=True)
            return
        rate = await get_usdt_rate_async(session)
        notify_text = credit_order(session, locked, amount, rate, str(data.get("order_id") or ""))
        if notify_text is None:
            await cq.answer("该订单已到账。", show_alert=True)
            return
        session.commit()

    await cq.answer("✅ 支付成功，已到账！", show_alert=True)
    try:
        await cq.message.edit_text(notify_text, reply_markup=menu_kb(cq.from_user.id))
    except Exception:
        await cq.message.answer(notify_text, reply_markup=menu_kb(cq.from_user.id))
