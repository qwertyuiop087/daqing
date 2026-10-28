"""抽奖系统：奖品池按概率抽取、兑换价目表，中奖直接到账并留痕。"""
import random

from database import (
    LotteryRecord,
    SessionLocal,
    add_lottery_tickets,
    add_user_days,
    add_wash_balance,
    get_lottery_notify_template,
    get_lottery_premium_prizes,
    get_lottery_prizes,
    get_lottery_redeem,
)

TYPE_KEYS = {"天": "days", "余额": "wash_balance", "高级券": "ticket"}
SOURCE_LABELS = {"ordinary": "普通抽奖", "premium": "高级抽奖", "redeem": "兑换"}


def parse_prizes_input(text: str):
    """解析奖品池输入：类型:数值:概率（逗号分隔）。

    类型：天=会员天数，余额=洗数据余额（元），高级券=高级抽奖次数。
    例：天:1:40,余额:5:5,高级券:1:3
    返回 (prizes, None) 或 (None, 错误提示)。
    """
    prizes = []
    total = 0.0
    for item in (text or "").strip().split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            return None, f"「{item}」格式错误，应为 类型:数值:概率"
        typ = parts[0].strip()
        if typ not in TYPE_KEYS:
            return None, f"「{item}」类型只支持 天 / 余额 / 高级券"
        try:
            prob = float(parts[2].strip())
        except ValueError:
            return None, f"「{item}」概率不是数字"
        if not (0 < prob <= 100):
            return None, f"「{item}」概率需在 0~100 之间"
        ptype = TYPE_KEYS[typ]
        try:
            value = int(parts[1].strip()) if ptype in ("days", "ticket") else float(parts[1].strip())
        except ValueError:
            return None, f"「{item}」数值不是数字"
        if value <= 0:
            return None, f"「{item}」数值需大于 0"
        total += prob
        prizes.append({"type": ptype, "value": value, "prob": prob})
    if not prizes:
        return None, "至少设置一条奖品"
    if total > 100:
        return None, f"概率总和 {total}% 超过 100%"
    return prizes, None


def parse_redeem_input(text: str):
    """解析兑换价目表输入：券数:类型:数值（逗号分隔）。

    类型：天=会员天数，余额=洗数据余额（元）。
    例：1:天:10,2:余额:20
    返回 (items, None) 或 (None, 错误提示)。
    """
    items = []
    for item in (text or "").strip().split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            return None, f"「{item}」格式错误，应为 券数:类型:数值"
        try:
            cost = int(parts[0].strip())
        except ValueError:
            return None, f"「{item}」券数不是整数"
        if cost < 1:
            return None, f"「{item}」券数需 ≥1"
        typ = parts[1].strip()
        if typ not in ("天", "余额"):
            return None, f"「{item}」类型只支持 天 / 余额"
        ptype = "days" if typ == "天" else "wash_balance"
        try:
            value = int(parts[2].strip()) if ptype == "days" else float(parts[2].strip())
        except ValueError:
            return None, f"「{item}」数值不是数字"
        if value <= 0:
            return None, f"「{item}」数值需大于 0"
        items.append({"cost": cost, "type": ptype, "value": value})
    if not items:
        return None, "至少设置一条兑换"
    return items, None


def _apply_prize(session, user, ptype: str, value: float) -> str:
    """发放奖品并返回中文标签。"""
    if ptype == "days":
        add_user_days(session, user, int(value))
        return f"{int(value)} 天会员"
    if ptype == "wash_balance":
        add_wash_balance(session, user.id, float(value))
        return f"¥{float(value):.4f} 洗数据余额"
    if ptype == "ticket":
        add_lottery_tickets(session, user.id, int(value))
        return f"高级抽奖 {int(value)} 次"
    return ""


def _pick(prizes: list[dict]):
    """按概率抽取一条奖品，未命中返回 None（谢谢参与）。"""
    r = random.uniform(0, 100)
    cum = 0.0
    for p in prizes:
        cum += float(p.get("prob", 0))
        if r < cum:
            return p
    return None


def _record(session, user, source: str, ptype: str, value: float, label: str) -> None:
    session.add(
        LotteryRecord(
            user_id=user.id,
            username=user.username or "",
            source=source,
            prize_type=ptype,
            prize_value=float(value),
            prize_label=label,
        )
    )


def draw_ordinary(session, user) -> dict:
    """普通抽奖一次（每天限一次，调用方负责检查）。返回结果 dict。"""
    chosen = _pick(get_lottery_prizes(session))
    if chosen is None:
        _record(session, user, "ordinary", "none", 0.0, "谢谢参与")
        session.commit()
        return {"type": "none", "value": 0.0, "label": "谢谢参与"}
    label = _apply_prize(session, user, chosen["type"], chosen["value"])
    _record(session, user, "ordinary", chosen["type"], chosen["value"], label)
    session.commit()
    return {"type": chosen["type"], "value": chosen["value"], "label": label}


def draw_premium(session, user) -> dict:
    """高级抽奖一次：消耗 1 张高级券。调用前应已确认券数足够。"""
    user.lottery_tickets = (user.lottery_tickets or 0) - 1
    chosen = _pick(get_lottery_premium_prizes(session))
    if chosen is None:
        _record(session, user, "premium", "none", 0.0, "谢谢参与")
        session.commit()
        return {"type": "none", "value": 0.0, "label": "谢谢参与"}
    label = _apply_prize(session, user, chosen["type"], chosen["value"])
    _record(session, user, "premium", chosen["type"], chosen["value"], label)
    session.commit()
    return {"type": chosen["type"], "value": chosen["value"], "label": label}


def redeem_prize(session, user, item: dict) -> str:
    """按兑换价目表用高级券兑换奖品，返回奖品标签。"""
    user.lottery_tickets = (user.lottery_tickets or 0) - int(item["cost"])
    label = _apply_prize(session, user, item["type"], item["value"])
    _record(session, user, "redeem", item["type"], item["value"], label)
    session.commit()
    return label


def mask_name(name: str) -> str:
    """名字打码：2 字隐 1 个（张三→张*）、3 字隐中间（王小二→王*二）、更长隐中间。"""
    name = (name or "").strip()
    if not name:
        return ""
    if len(name) == 1:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


async def notify_lottery(bot, tg_id: int, user_name: str, source: str, prize_label: str) -> None:
    """抽奖/兑换到账后通知绑定的通知群（未绑定则不通知）。

    user_name 传用户 Telegram 名字（first_name），通知里会打码显示。
    """
    try:
        with SessionLocal() as session:
            from database import get_drop_notify_chat
            chat_id = get_drop_notify_chat(session)
            template = get_lottery_notify_template(session)
        if not chat_id:
            return
        display = mask_name(user_name) or f"用户{tg_id}"
        text = template.format(
            user_id=tg_id,
            username=display,
            source=SOURCE_LABELS.get(source, source),
            prize=prize_label,
        )
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        pass
