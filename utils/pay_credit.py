"""订单入账：把已支付的 PaymentOrder 发放成会员天数 / 洗数据余额。

自动到账扫描（大力钱包）与 OKPay 回调/查单共用这一份逻辑，
保证两条收款渠道的账目口径完全一致。
"""
from __future__ import annotations


def credit_order(session, order, amount_usdt: float, rate: float, txid: str = "") -> str | None:
    """标记订单已支付并发放权益，返回给用户的通知文本。

    调用方负责加行锁（with_for_update）并在返回后 commit。
    返回 None 表示该订单不需要处理（已支付/不存在）。
    """
    if order is None or order.status == "paid":
        return None

    from database import User, WashOrder, add_user_days, add_wash_balance, get_wash_balance

    # 实付金额校验：会员权益不能只看"渠道说已支付"，
    # 否则用户把支付金额改小（USDT 转账可改额）就能拿到整档会员。
    try:
        paid_amount = float(amount_usdt or 0)
        expect_amount = float(order.amount or 0)
    except (TypeError, ValueError):
        paid_amount, expect_amount = 0.0, float(order.amount or 0)
    if expect_amount > 0 and paid_amount + 1e-6 < expect_amount:
        order.status = "underpaid"
        session.commit()
        return (
            "⚠️ <b>支付金额不足</b>\n\n"
            f"订单应付：{expect_amount:g} USDT\n"
            f"实际收到：{paid_amount:g} USDT\n\n"
            "订单已转人工核对，请联系客服处理。"
        )

    order.status = "paid"
    if txid:
        order.txid = txid

    if (order.days or 0) > 0:
        user = session.get(User, order.user_id)
        if user is not None:
            add_user_days(session, user, order.days)
            _pay_referral(session, user, "member_buy", float(amount_usdt or 0) * float(rate or 0))
        return (
            "🎉 <b>支付成功，会员已自动到账！</b>\n\n"
            f"套餐：<b>{order.days}</b> 天会员\n"
            f"金额：{order.amount} USDT\n\n"
            "感谢购买，祝你使用愉快～"
        )

    rmb = float(amount_usdt) * float(rate)
    add_wash_balance(session, order.user_id, rmb)
    buyer = session.get(User, order.user_id)
    if buyer is not None:
        _pay_referral(session, buyer, "wash_recharge", rmb)
    balance = get_wash_balance(session, order.user_id)
    channel = "OKPay" if (order.coin or "").lower().startswith("okpay") else "大力钱包"
    session.add(
        WashOrder(
            user_id=order.user_id,
            filename=f"充值 ${amount_usdt} USDT（{channel}到账）",
            row_count=0,
            cost=rmb,
            type="recharge",
        )
    )
    return (
        f"💰 <b>洗数据充值已自动到账（{channel}）</b>\n\n"
        f"充值金额：${amount_usdt} USDT\n"
        f"换算汇率：1 USDT = {rate} 元\n"
        f"到账余额：¥{rmb:.4f} 元\n"
        f"当前余额：¥{balance:.4f} 元"
    )


def _pay_referral(session, buyer, kind: str, base_rmb: float) -> None:
    """给邀请人发返佣（进洗数据余额）。

    kind: wash_recharge（洗数据充值） / member_buy（开通会员）
    返佣环节出问题不能影响买家到账，所以整体兜住异常。
    """
    try:
        from database import add_referral_reward, get_referral_enabled, get_referral_rates

        if not buyer or not buyer.inviter_id:
            return
        if not get_referral_enabled(session):
            return
        wash_rate, member_rate = get_referral_rates(session)
        rate = wash_rate if kind == "wash_recharge" else member_rate
        if rate <= 0 or base_rmb <= 0:
            return
        add_referral_reward(session, buyer.inviter_id, buyer.id, kind, base_rmb, rate)
    except Exception as e:
        print(f"[referral] 返佣失败：{e}")
