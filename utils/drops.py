"""隐藏掉落功能：使用功能时概率掉落会员天数，直接到账，并发送通知到通知群。

- 非洗数据功能：1% 概率掉落 1~20 天；不限制频率。
- 洗数据：5% 概率掉落 3~25 天；不限制频率。
- 天数加权随机：越大概率越小（权重 = 1/天）。
- 通知内容由管理员在管理面板自定义模板（占位符见 get_drop_notify_template 说明）。
"""
import random

from database import (
    DropRecord,
    SessionLocal,
    User,
    add_user_days,
    beijing_str,
    get_drop_notify_chat,
    get_drop_notify_template,
    is_member,
)

# 掉落概率与天数范围
OTHER_DROP_PROB = 0.01  # 非洗数据 1%
OTHER_DAYS_RANGE = (1, 20)
WASH_DROP_PROB = 0.05  # 洗数据 5%
WASH_DAYS_RANGE = (3, 25)


def _weighted_days(low: int, high: int) -> int:
    """加权随机掉落天数：越大概率越小（权重 = 1/天）。"""
    weights = [1.0 / d for d in range(low, high + 1)]
    return random.choices(range(low, high + 1), weights=weights)[0]


def roll_drop(session, tg_id: int, feature: str, is_wash: bool = False) -> int:
    """（已下线）隐藏掉落已被抽奖系统取代，保留签名避免调用方报错。"""
    return 0


def _build_notify_text(session, tg_id: int, feature: str, days: int) -> str:
    """按管理员自定义模板生成通知内容。"""
    template = get_drop_notify_template(session)
    user = session.get(User, tg_id)
    username = (user.username or "") if user else ""
    expires = beijing_str(user.membership_expires_at) if user else "无"
    return template.format(
        user_id=tg_id,
        username=username,
        feature=feature,
        days=days,
        expires=expires,
    )


async def notify_drop_chat(bot, tg_id: int, feature: str, days: int) -> None:
    """把掉落通知发送到管理员绑定的通知群（未绑定则不发）。"""
    try:
        with SessionLocal() as session:
            chat_id = get_drop_notify_chat(session)
            text = _build_notify_text(session, tg_id, feature, days)
        if not chat_id:
            return
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        # 通知失败不影响掉落结果
        pass


async def maybe_drop(event_or_message, feature: str, is_wash: bool = False) -> int:
    """（已下线）隐藏掉落已被抽奖系统取代，保留签名避免调用方报错。"""
    return 0


def drop_template_hint() -> str:
    """管理面板设置模板时的占位符说明。"""
    return (
        "可用占位符：\n"
        "• {user_id} — 用户 ID\n"
        "• {username} — 用户名（可能为空）\n"
        "• {feature} — 使用功能\n"
        "• {days} — 掉落天数\n"
        "• {expires} — 会员当前到期时间\n\n"
        "例：用户 {username}（{user_id}）使用「{feature}」掉落 {days} 天，到期 {expires}"
    )
