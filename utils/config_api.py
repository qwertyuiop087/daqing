"""网页后台共享核心：配置读取/校验/写入。

机器人内置 webapp（aiohttp）与独立的 webadmin（FastAPI）共用本模块，
保证两边校验规则与存取格式完全一致。
"""
import json

import database
from utils.lottery import parse_prizes_input, parse_redeem_input


def parse_plans_input(text: str) -> list[dict]:
    """解析套餐输入：天数:价格,天数:价格。格式错误抛 ValueError。"""
    plans = []
    for item in (text or "").strip().split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise ValueError(f"「{item}」格式错误，应为 天数:价格")
        try:
            days = int(parts[0].strip())
            price = float(parts[1].strip())
        except ValueError:
            raise ValueError(f"「{item}」数值不是数字")
        if days < 1 or price <= 0:
            raise ValueError(f"「{item}」天数需≥1、价格需>0")
        plans.append({"days": days, "price": price})
    if not plans:
        raise ValueError("至少一个套餐")
    return plans


def parse_wash_prices_input(text: str) -> list[dict]:
    """解析洗数据阶梯定价：行数:单价,行数:单价。格式错误抛 ValueError。"""
    prices = []
    for item in (text or "").strip().split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise ValueError(f"「{item}」格式错误，应为 行数:单价")
        try:
            min_rows = int(parts[0].strip())
            price = float(parts[1].strip())
        except ValueError:
            raise ValueError(f"「{item}」数值不是数字")
        if min_rows < 0 or price <= 0:
            raise ValueError(f"「{item}」行数需≥0、单价需>0")
        prices.append({"min_rows": min_rows, "price_per_row": price})
    if not prices:
        raise ValueError("至少一个档位")
    return prices


_PRIZE_TYPE_LABEL = {"days": "天", "wash_balance": "余额", "ticket": "高级券"}


def _prizes_to_text(prizes: list[dict]) -> str:
    """奖品池 → 「类型:数值:概率」文本（与网页后台输入格式一致）。"""
    parts = []
    for p in prizes:
        t = _PRIZE_TYPE_LABEL.get(p["type"], p["type"])
        v = int(p["value"]) if p["type"] in ("days", "ticket") else f"{float(p['value']):g}"
        parts.append(f"{t}:{v}:{float(p['prob']):g}")
    return ",".join(parts)


def _redeem_to_text(items: list[dict]) -> str:
    """兑换价目表 → 「券数:类型:数值」文本（与网页后台输入格式一致）。"""
    parts = []
    for it in items:
        t = _PRIZE_TYPE_LABEL.get(it["type"], it["type"])
        v = int(it["value"]) if it["type"] == "days" else f"{float(it['value']):g}"
        parts.append(f"{int(it['cost'])}:{t}:{v}")
    return ",".join(parts)


def get_all_config(session) -> dict:
    return {
        "lottery_prizes": _prizes_to_text(database.get_lottery_prizes(session)),
        "lottery_premium_prizes": _prizes_to_text(database.get_lottery_premium_prizes(session)),
        "lottery_redeem": _redeem_to_text(database.get_lottery_redeem(session)),
        "plans": json.dumps(database.get_plans(session), ensure_ascii=False),
        "wash_prices": json.dumps(database.get_wash_prices(session), ensure_ascii=False),
        "wash_price_floor": database.get_wash_price_floor(session),
        "wash_quota": database.get_wash_quota(session),
        "usdt_rate": database.get_config(session, "usdt_rate", ""),
        "welcome_text": database.get_config(session, "welcome_text", database.DEFAULT_WELCOME_TEXT),
        "lottery_notify_template": database.get_lottery_notify_template(session),
    }


def save_config(session, key: str, value: str) -> None:
    """写入配置项。格式错误抛 ValueError。"""
    if key in ("lottery_prizes", "lottery_premium_prizes"):
        prizes, err = parse_prizes_input(value)
        if err:
            raise ValueError(err)
        if key == "lottery_prizes":
            database.set_lottery_prizes(session, prizes)
        else:
            database.set_lottery_premium_prizes(session, prizes)
    elif key == "lottery_redeem":
        items, err = parse_redeem_input(value)
        if err:
            raise ValueError(err)
        database.set_lottery_redeem(session, items)
    elif key == "plans":
        database.set_plans(session, parse_plans_input(value))
    elif key == "wash_prices":
        database.set_wash_prices(session, parse_wash_prices_input(value))
    elif key == "wash_price_floor":
        try:
            v = float(value)
        except ValueError:
            raise ValueError("价格下限不是数字")
        if v <= 0:
            raise ValueError("价格下限需大于 0")
        database.set_wash_price_floor(session, v)
    elif key == "wash_quota":
        if not value.strip().isdigit():
            raise ValueError("配额需为非负整数")
        database.set_wash_quota(session, int(value))
    elif key == "usdt_rate":
        value = value.strip()
        if value:
            try:
                float(value)
            except ValueError:
                raise ValueError("汇率不是数字")
            database.set_usdt_rate(session, float(value))
        else:
            database.set_config(session, "usdt_rate", "")
    elif key == "welcome_text":
        database.set_config(session, "welcome_text", value)
        # 网页编辑不带消息实体（emoji/加粗），清掉 entities，机器人会按 HTML 渲染
        database.set_config(session, "welcome_entities", "")
    elif key == "lottery_notify_template":
        if not value.strip():
            raise ValueError("模板不能为空")
        database.set_lottery_notify_template(session, value)
    else:
        raise ValueError("未知配置项")


def reset_config(session, key: str) -> None:
    """恢复某项配置为默认值。未知配置项抛 ValueError。"""
    if key == "lottery_prizes":
        database.set_lottery_prizes(session, database.DEFAULT_LOTTERY_PRIZES)
    elif key == "lottery_premium_prizes":
        database.set_lottery_premium_prizes(session, database.DEFAULT_LOTTERY_PREMIUM_PRIZES)
    elif key == "lottery_redeem":
        database.set_lottery_redeem(session, database.DEFAULT_LOTTERY_REDEEM)
    elif key == "plans":
        database.set_plans(session, json.loads(database.DEFAULT_PLANS))
    elif key == "wash_prices":
        database.set_wash_prices(session, database.DEFAULT_WASH_PRICES)
    elif key == "wash_price_floor":
        database.set_wash_price_floor(session, database.DEFAULT_WASH_PRICE_FLOOR)
    elif key == "wash_quota":
        database.set_wash_quota(session, 0)
    elif key == "usdt_rate":
        database.set_config(session, "usdt_rate", "")
    elif key == "welcome_text":
        database.set_config(session, "welcome_text", "")
        database.set_config(session, "welcome_entities", "")
    elif key == "lottery_notify_template":
        database.set_lottery_notify_template(session, database.DEFAULT_LOTTERY_NOTIFY_TEMPLATE)
    else:
        raise ValueError("未知配置项")


def get_stats(session) -> dict:
    """概览统计 + 最近抽奖记录。"""
    from sqlalchemy import func, select

    total_users = session.scalar(select(func.count()).select_from(database.User)) or 0
    members = session.scalar(
        select(func.count()).select_from(database.User).where(
            database.User.membership_expires_at > database.utcnow()
        )
    ) or 0
    lottery = database.get_lottery_stats(session)
    charged, pending = database.get_washed_breakdown(session)
    records = database.get_lottery_records(session, 50)
    rec_list = [
        {
            "time": database.beijing_str(r.created_at),
            "user": r.username or str(r.user_id),
            "source": r.source,
            "prize": r.prize_label,
        }
        for r in records
    ]
    return {
        "total_users": int(total_users),
        "members": int(members),
        "lottery_total": lottery["total"],
        "lottery_today": lottery["today"],
        "washed_charged": int(charged),
        "washed_pending": int(pending),
        "records": rec_list,
    }


# ---------------- 成员管理（网页后台） ----------------

def search_members(session, q: str = "", limit: int = 100) -> list[dict]:
    """按 ID / 用户名 / 昵称搜索成员，返回会员状态与余额等。"""
    from sqlalchemy import or_, select

    query = select(database.User).order_by(database.User.id.desc())
    qs = (q or "").strip()
    if qs:
        if qs.isdigit():
            query = query.where(database.User.id == int(qs))
        else:
            like = f"%{qs}%"
            query = query.where(
                or_(
                    database.User.username.ilike(like),
                    database.User.first_name.ilike(like),
                )
            )
    users = list(session.scalars(query.limit(limit)))
    now = database.utcnow()
    out = []
    for u in users:
        if u.membership_expires_at and u.membership_expires_at > now:
            member_text = f"{database.remaining_days(u)} 天"
            expires = database.beijing_str(u.membership_expires_at)
        else:
            member_text = "无会员"
            expires = ""
        out.append(
            {
                "id": u.id,
                "username": u.username or "",
                "first_name": u.first_name or "",
                "member_text": member_text,
                "expires": expires,
                "wash_balance": round(u.wash_balance or 0, 4),
                "lottery_tickets": int(u.lottery_tickets or 0),
            }
        )
    return out


def adjust_member(session, user_id: int, days_delta: int = 0, balance_delta: float = 0.0) -> dict:
    """调整用户会员天数 / 洗数据余额。正数增加、负数扣减。

    天数扣到 0 以下按到期处理；余额扣减时不足会抛 ValueError。
    余额变动会写 WashOrder 账单（与机器人管理面板一致）。
    返回调整后的用户状态。
    """
    user = session.get(database.User, user_id)
    if user is None:
        raise ValueError("用户不存在")

    changed = []
    if days_delta:
        if days_delta > 0:
            database.add_user_days(session, user, days_delta)
            changed.append(f"会员 +{days_delta} 天")
        else:
            database.deduct_user_days(session, user, -days_delta)
            changed.append(f"会员 -{-days_delta} 天")
    if balance_delta:
        if balance_delta > 0:
            database.add_wash_balance(session, user_id, balance_delta)
            session.add(
                database.WashOrder(
                    user_id=user_id,
                    filename="网页后台手动充值",
                    row_count=0,
                    cost=balance_delta,
                    type="recharge",
                )
            )
            changed.append(f"余额 +¥{balance_delta:.4f}")
        else:
            if not database.deduct_wash_balance(session, user_id, -balance_delta):
                raise ValueError("洗数据余额不足，无法扣减")
            session.add(
                database.WashOrder(
                    user_id=user_id,
                    filename="网页后台手动扣费",
                    row_count=0,
                    cost=balance_delta,
                    type="deduct",
                )
            )
            changed.append(f"余额 -¥{-balance_delta:.4f}")
        session.commit()

    now = database.utcnow()
    if user.membership_expires_at and user.membership_expires_at > now:
        member_text = f"{database.remaining_days(user)} 天"
        expires = database.beijing_str(user.membership_expires_at)
    else:
        member_text = "无会员"
        expires = ""
    return {
        "id": user.id,
        "member_text": member_text,
        "expires": expires,
        "wash_balance": round(user.wash_balance or 0, 4),
        "changed": changed,
    }


# ---------------- 网站设置（落地页内容可后台配置） ----------------

SITE_FIELDS = {
    "site_title", "brand", "hero_highlight", "hero_subtitle",
    "accent", "contact1", "contact2", "bot_link",
}

DEFAULT_SITE_CONFIG = {
    "site_title": "大晴分包机器人 · DQFB",
    "brand": "大晴分包",
    "hero_highlight": "大晴分包机器人",
    "hero_subtitle": "文件切割、号码去重、空号检测、催收分包、洗数据…… 上传即处理，结果秒回。内置抽奖系统与会员体系，运营一步到位。",
    "accent": "#3b82f6",
    "contact1": "@mmmmg",
    "contact2": "@kefu686",
    "bot_link": "https://t.me/sechou_bot",
}


def get_site_config(session) -> dict:
    """读取网站配置（带默认值）。"""
    cfg = dict(DEFAULT_SITE_CONFIG)
    raw = database.get_config(session, "site_config", "")
    if raw:
        try:
            data = json.loads(raw)
            for k in SITE_FIELDS:
                if k in data:
                    cfg[k] = data[k]
        except Exception:
            pass
    return cfg


def set_site_config(session, updates: dict) -> dict:
    """保存网站配置。只接受白名单字段，返回保存后的完整配置。"""
    import json as _json
    cfg = dict(DEFAULT_SITE_CONFIG)
    raw = database.get_config(session, "site_config", "")
    if raw:
        try:
            cfg.update(_json.loads(raw))
        except Exception:
            pass
    for k, v in (updates or {}).items():
        if k not in SITE_FIELDS:
            raise ValueError(f"未知字段：{k}")
        cfg[k] = v
    database.set_config(session, "site_config", _json.dumps(cfg, ensure_ascii=False))
    return cfg
