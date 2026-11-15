"""定时提醒：会员到期提醒 + 每日对账日报。

两个循环都在后台跑，用 config 里的日期标记保证「每天只发一次」，
重启或部署也不会重复发。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from database import (
    AppConfig,
    PaymentOrder,
    SessionLocal,
    User,
    WashOrder,
    beijing_str,
    get_config,
    remaining_days,
    set_config,
)
from utils.roles import admin_id_list

BJT = timezone(timedelta(hours=8))
REMIND_AT_HOUR = 10          # 北京时间 10 点后发到期提醒
REPORT_AT_HOUR = 9           # 北京时间 9 点后发日报
REMIND_DAYS = (7, 3, 1)      # 到期前 7 / 3 / 1 天提醒
CHECK_INTERVAL = 900         # 15 分钟检查一次


def _now_bj() -> datetime:
    return datetime.now(BJT)


def _today_bj() -> str:
    return _now_bj().strftime("%Y-%m-%d")


def _day_range_bj(days_ago: int = 0) -> tuple[datetime, datetime]:
    """北京时间某天的 [00:00, 次日 00:00)，返回 UTC naive（库里存 UTC）。"""
    day = (_now_bj() - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = day.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (day + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _renew_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 立即续费", callback_data="buy:plans")],
            [InlineKeyboardButton(text="🎫 兑换卡密", callback_data="membership:redeem")],
        ]
    )


async def send_expiry_reminders(bot: Bot) -> int:
    """给到期前 7/3/1 天的会员发提醒，同一天只发一轮。"""
    today = _today_bj()
    with SessionLocal() as session:
        flag = f"expiry_remind_date:{today}"
        if get_config(session, flag, ""):
            return 0
        set_config(session, flag, "1")  # 先占位，避免多实例重复发
        users = list(
            session.scalars(select(User).where(User.membership_expires_at.is_not(None)))
        )
        targets = [(u.id, remaining_days(u), u.membership_expires_at) for u in users]

    sent = 0
    for uid, days, expires_at in targets:
        if days not in REMIND_DAYS:
            continue
        text = (
            "⏰ <b>会员到期提醒</b>\n\n"
            f"你的会员还有 <b>{days}</b> 天到期。\n"
            f"到期时间：{beijing_str(expires_at)}（北京时间）\n\n"
            "到期后洗数据、加区号、车牌分包等功能会暂停使用，"
            "续费或兑换卡密后立即恢复。"
        )
        try:
            await bot.send_message(uid, text, reply_markup=_renew_kb())
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return sent


def _pending_task_count(session) -> int:
    """统计还没回传完的洗数据任务数（done 的不算）。"""
    count = 0
    for row in session.scalars(
        select(AppConfig).where(AppConfig.key.like("wash_pending:%"))
    ):
        parts = (row.value or "").split(":", 4)
        if len(parts) >= 5:
            try:
                fm = json.loads(parts[4])
                if isinstance(fm, dict) and fm.get("done"):
                    continue
            except Exception:
                pass
        count += 1
    return count


def collect_daily_report() -> str:
    """生成昨天的对账日报文本。"""
    start, end = _day_range_bj(1)
    yesterday = (_now_bj() - timedelta(days=1)).strftime("%m-%d")

    with SessionLocal() as session:
        paid = list(
            session.scalars(
                select(PaymentOrder).where(
                    PaymentOrder.status == "paid",
                    PaymentOrder.created_at >= start,
                    PaymentOrder.created_at < end,
                )
            )
        )
        recharge_orders = [o for o in paid if not (o.days or 0)]
        member_orders = [o for o in paid if (o.days or 0)]

        washes = list(
            session.scalars(
                select(WashOrder).where(
                    WashOrder.created_at >= start, WashOrder.created_at < end
                )
            )
        )
        deduct = [w for w in washes if w.type == "deduct"]
        refund = [w for w in washes if w.type == "refund"]
        recharge_rows = [w for w in washes if w.type == "recharge"]
        lottery_rows = [w for w in washes if w.type == "lottery"]

        new_users = session.scalar(
            select(func.count(User.id)).where(
                User.registered_at >= start, User.registered_at < end
            )
        ) or 0
        members = session.scalar(
            select(func.count(User.id)).where(User.membership_expires_at.is_not(None))
        ) or 0
        total_balance = session.scalar(
            select(func.coalesce(func.sum(User.wash_balance), 0.0))
        ) or 0.0
        sum_recharge = session.scalar(
            select(func.coalesce(func.sum(WashOrder.cost), 0.0)).where(
                WashOrder.type == "recharge"
            )
        ) or 0.0
        sum_lottery = session.scalar(
            select(func.coalesce(func.sum(WashOrder.cost), 0.0)).where(
                WashOrder.type == "lottery"
            )
        ) or 0.0
        sum_deduct = session.scalar(
            select(func.coalesce(func.sum(WashOrder.cost), 0.0)).where(
                WashOrder.type == "deduct"
            )
        ) or 0.0
        sum_refund = session.scalar(
            select(func.coalesce(func.sum(WashOrder.cost), 0.0)).where(
                WashOrder.type == "refund"
            )
        ) or 0.0
        pending_orders = session.scalar(
            select(func.count(PaymentOrder.id)).where(PaymentOrder.status == "pending")
        ) or 0
        pending_tasks = _pending_task_count(session)

    income = sum(float(o.amount or 0) for o in paid)
    check = round(float(sum_recharge + sum_lottery + sum_deduct + sum_refund), 4)
    diff = round(float(total_balance) - check, 4)

    def money(x) -> str:
        return f"¥{float(x):,.2f}"

    return (
        f"📊 <b>对账日报（{yesterday}）</b>\n\n"
        f"💰 <b>收入</b>\n"
        f"· 洗数据充值：{money(sum(w.cost for w in recharge_rows))}"
        f"（{len(recharge_rows)} 笔）\n"
        f"· 会员开通：{len(member_orders)} 单\n"
        f"· 其它充值订单：{len(recharge_orders)} 笔\n"
        f"· 实收 USDT：{income:g}\n\n"
        f"🧼 <b>消耗</b>\n"
        f"· 洗数据扣费：{money(sum_deduct)}（{len(deduct)} 条）\n"
        f"· 退款：{money(abs(sum(w.cost for w in refund)))}（{len(refund)} 条）\n\n"
        f"👥 <b>用户</b>\n"
        f"· 新增：{new_users} 人\n"
        f"· 会员总数：{members} 人\n\n"
        f"🎁 抽奖发放：{money(sum_lottery)}（{len(lottery_rows)} 次）\n\n"
        f"🔍 <b>对账</b>\n"
        f"· 用户余额合计：{money(total_balance)}\n"
        f"· 账目校验：{money(check)}\n"
        f"· 差额：{money(diff)}（正常应接近 0）\n\n"
        f"⚠️ <b>待处理</b>\n"
        f"· 待支付订单：{pending_orders} 个\n"
        f"· 未回传洗数据任务：{pending_tasks} 个"
    )


async def send_daily_report(bot: Bot) -> bool:
    """每天北京时间 9 点后给管理员发一次日报。"""
    today = _today_bj()
    with SessionLocal() as session:
        flag = f"daily_report_date:{today}"
        if get_config(session, flag, ""):
            return False
        set_config(session, flag, "1")
    try:
        text = collect_daily_report()
    except Exception as e:
        text = f"⚠️ 对账日报生成失败：{e}"
    for uid in admin_id_list():
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass
    return True


async def start_reminders(bot: Bot) -> None:
    """到期提醒 + 日报的常驻循环。"""
    while True:
        try:
            now = _now_bj()
            if now.hour >= REMIND_AT_HOUR:
                await send_expiry_reminders(bot)
            if now.hour >= REPORT_AT_HOUR:
                await send_daily_report(bot)
        except Exception as e:
            print(f"[reminders] 循环异常：{e}")
        await asyncio.sleep(CHECK_INTERVAL)
