"""数据库：用户 / 卡密 / 会员时长。

- 配置了 DATABASE_URL（Railway Postgres 等）则使用该库；
- 未配置则回退到本地 SQLite 文件 bot_data.db（适合本地调试）。
"""
import asyncio
import random
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, DateTime, Integer, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from config import settings


def normalize_db_url(url: str) -> str:
    """把 Railway 常见的 postgres:// 转为 SQLAlchemy 需要的驱动格式。"""
    if not url:
        return "sqlite:///./bot_data.db"
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def utcnow() -> datetime:
    """统一返回无时区 UTC 时间，兼容 SQLite / Postgres。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_beijing(dt: datetime) -> datetime:
    """把 UTC 时间转成北京时间（UTC+8）。"""
    if dt is None:
        return None
    return dt + timedelta(hours=8)


def beijing_str(dt: datetime) -> str:
    """把 UTC 时间转成北京时间字符串。"""
    if dt is None:
        return "无"
    return to_beijing(dt).strftime("%Y-%m-%d %H:%M")


if settings.DATABASE_URL:
    engine = create_engine(
        normalize_db_url(settings.DATABASE_URL), pool_pre_ping=True
    )
else:
    # 本地调试用的 SQLite：check_same_thread 需放在 connect_args 中
    engine = create_engine(
        "sqlite:///./bot_data.db", connect_args={"check_same_thread": False}
    )
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user_id
    username: Mapped[str] = mapped_column(String(255), default="")
    first_name: Mapped[str] = mapped_column(String(255), default="")
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    membership_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wash_balance: Mapped[float] = mapped_column(default=0.0)  # 洗数据余额（人民币 元）
    lottery_tickets: Mapped[int] = mapped_column(Integer, default=0)  # 高级抽奖次数


class CardKey(Base):
    __tablename__ = "card_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    days: Mapped[int] = mapped_column(Integer, default=30)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AppConfig(Base):
    """全局可配置项（key-value 结构），存欢迎语等可编辑文案。"""
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String, default="")


class PaymentOrder(Base):
    """支付订单表。"""
    __tablename__ = "payment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 商户订单号
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)  # Telegram 用户 ID
    days: Mapped[int] = mapped_column(Integer)  # 购买的会员天数
    amount: Mapped[str] = mapped_column(String(32))  # 支付金额
    coin: Mapped[str] = mapped_column(String(16), default="usdt")  # 币种
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/paid/timeout
    txid: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 平台交易号
    pay_url: Mapped[str | None] = mapped_column(String, nullable=True)  # 支付链接
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WashOrder(Base):
    __tablename__ = "wash_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)  # 用户ID
    filename: Mapped[str] = mapped_column(String(255))  # 文件名
    row_count: Mapped[int] = mapped_column(Integer)  # 行数
    cost: Mapped[float] = mapped_column(default=0.0)  # 费用（人民币）
    type: Mapped[str] = mapped_column(String(16))  # recharge（充值）/ deduct（洗数据扣费）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DropRecord(Base):
    """隐藏掉落记录：使用功能时概率掉落会员天数，管理员后台可查。"""

    __tablename__ = "drop_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)  # 用户ID
    username: Mapped[str] = mapped_column(String(255), default="")  # 用户名
    feature: Mapped[str] = mapped_column(String(64))  # 功能名（如 文件切割）
    days: Mapped[int] = mapped_column(Integer)  # 掉落天数
    is_wash: Mapped[bool] = mapped_column(default=False)  # 是否洗数据掉落
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LotteryRecord(Base):
    """抽奖/兑换记录：用户抽奖结果，管理员后台可查。"""

    __tablename__ = "lottery_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)  # 用户ID
    username: Mapped[str] = mapped_column(String(255), default="")  # 用户名
    source: Mapped[str] = mapped_column(String(16), default="ordinary")  # ordinary / premium / redeem
    prize_type: Mapped[str] = mapped_column(String(16))  # days / wash_balance / ticket / none
    prize_value: Mapped[float] = mapped_column(default=0.0)
    prize_label: Mapped[str] = mapped_column(String(128), default="")  # 如 3 天会员
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def init_db(max_retries: int = 10, retry_interval: float = 3.0) -> None:
    """初始化数据库表，带重试机制（等数据库启动）。"""
    last_err = None
    for i in range(max_retries):
        try:
            Base.metadata.create_all(engine)
            # 自动迁移：给老表加新字段
            _auto_migrate()
            return
        except Exception as e:
            last_err = e
            if i < max_retries - 1:
                wait = retry_interval * (i + 1)  # 递增等待：3s, 6s, 9s...
                print(f"⚠️ 数据库连接失败，{wait:.0f} 秒后重试 ({i+1}/{max_retries})...")
                time.sleep(wait)
    # 重试完还是失败，打印错误但不直接崩，让机器人降级用 SQLite 或继续跑
    print(f"❌ 数据库连接最终失败：{last_err}")
    print("⚠️ 继续启动，但数据库相关功能可能不可用。")


def get_or_create_user(
    session, tg_id: int, username: str = "", first_name: str = ""
) -> User:
    user = session.get(User, tg_id)
    if user is None:
        user = User(id=tg_id, username=username, first_name=first_name)
        session.add(user)
        session.commit()
    else:
        changed = False
        if username and username != user.username:
            user.username = username
            changed = True
        if first_name and first_name != user.first_name:
            user.first_name = first_name
            changed = True
        if changed:
            session.commit()
    return user


def _auto_migrate() -> None:
    """自动迁移：给老表加新字段，避免老数据库报错。"""
    from sqlalchemy import text, inspect
    inspector = inspect(engine)
    # 检查 users 表有没有 wash_balance / lottery_tickets 字段
    if "users" in inspector.get_table_names():
        cols = [c["name"] for c in inspector.get_columns("users")]
        if "wash_balance" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN wash_balance FLOAT DEFAULT 0.0"))
                conn.commit()
        if "lottery_tickets" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN lottery_tickets INTEGER DEFAULT 0"))
                conn.commit()


def is_member(user: User) -> bool:
    return user.membership_expires_at is not None and user.membership_expires_at > utcnow()


def get_wash_balance(session, tg_id: int) -> float:
    """获取用户洗数据余额（万行）。"""
    user = session.get(User, tg_id)
    return user.wash_balance if user else 0.0


def add_wash_balance(session, tg_id: int, amount: float) -> None:
    """给用户增加洗数据余额。"""
    user = session.get(User, tg_id)
    if user:
        user.wash_balance = (user.wash_balance or 0) + amount
        session.commit()


def deduct_wash_balance(session, tg_id: int, amount: float) -> bool:
    """扣除用户洗数据余额，余额不足返回 False。"""
    user = session.get(User, tg_id)
    if not user or (user.wash_balance or 0) < amount:
        return False
    user.wash_balance -= amount
    session.commit()
    return True


def remaining_days(user: User) -> int:
    if not user.membership_expires_at:
        return 0
    diff = user.membership_expires_at - utcnow()
    days = diff.days
    if diff.seconds > 0:
        days += 1
    return max(0, days)


def _gen_code() -> str:
    """生成卡密：XXXX-XXXX-XXXX（去掉易混淆字符 0/O/1/I）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(random.choices(alphabet, k=4)) for _ in range(3))


def create_cards(session, count: int, days: int, admin_id: int) -> list[str]:
    codes: list[str] = []
    for _ in range(count):
        while True:
            code = _gen_code()
            exists = session.scalar(select(CardKey).where(CardKey.code == code))
            if not exists:
                break
        session.add(CardKey(code=code, days=days, created_by=admin_id))
        codes.append(code)
    session.commit()
    return codes


def redeem_card(session, tg_id: int, code: str) -> tuple[bool, str]:
    """兑换卡密：给用户增加使用天数。返回 (是否成功, 提示文本)。"""
    code = code.strip().upper()
    card = session.scalar(select(CardKey).where(CardKey.code == code))
    if card is None:
        return False, "❌ 卡密不存在，请检查后重试。"
    if card.used_by is not None:
        return False, "❌ 该卡密已被使用，无法重复兑换。"

    card.used_by = tg_id
    card.used_at = utcnow()

    user = session.get(User, tg_id)
    now = utcnow()
    base = user.membership_expires_at if user.membership_expires_at and user.membership_expires_at > now else now
    user.membership_expires_at = base + timedelta(days=card.days)
    session.commit()
    return (
        True,
        f"✅ 兑换成功！会员有效期增加 {card.days} 天，"
        f"当前到期时间：{beijing_str(user.membership_expires_at)}（北京时间）。",
    )


def delete_card(session, code: str) -> tuple[bool, str]:
    """按卡密删除（不区分大小写）。返回 (是否成功, 提示文本)。"""
    code = code.strip().upper()
    card = session.scalar(select(CardKey).where(CardKey.code == code))
    if card is None:
        return False, "❌ 卡密不存在。"
    used = card.used_by is not None
    session.delete(card)
    session.commit()
    if used:
        return True, "🗑️ 已删除该卡密（此前已被使用，不影响已发放的会员时长）。"
    return True, "🗑️ 已删除该未使用卡密。"


def find_user(session, key: str):
    """按数字 ID 或用户名（可带 @，不区分大小写）查找用户。"""
    key = key.strip()
    if key.isdigit():
        return session.get(User, int(key))
    name = key.lstrip("@").lower()
    return session.scalar(select(User).where(func.lower(User.username) == name))


def get_user_by_id(session, user_id: int):
    """按 Telegram ID 直接查用户。"""
    return session.get(User, user_id)


def add_user_days(session, user: User, days: int) -> str:
    """给用户添加会员天数（在原到期时间基础上累加，已过期则从现在起算）。"""
    now = utcnow()
    base = (
        user.membership_expires_at
        if user.membership_expires_at and user.membership_expires_at > now
        else now
    )
    user.membership_expires_at = base + timedelta(days=days)
    session.commit()
    return (
        f"✅ 已为 <b>{user.id}</b> 添加 {days} 天会员。\n"
        f"当前到期时间：{beijing_str(user.membership_expires_at)}（北京时间）。"
    )


def deduct_user_days(session, user: User, days: int) -> str:
    """扣除用户会员天数。扣到 0 或以下则会员失效。"""
    if not user.membership_expires_at:
        return f"ℹ️ 用户 <b>{user.id}</b> 当前没有会员时长，无需扣除。"
    new_exp = user.membership_expires_at - timedelta(days=days)
    if new_exp <= utcnow():
        user.membership_expires_at = None
        session.commit()
        return f"✅ 已为用户 <b>{user.id}</b> 扣除 {days} 天，该用户会员已到期失效。"
    user.membership_expires_at = new_exp
    session.commit()
    return (
        f"✅ 已为用户 <b>{user.id}</b> 扣除 {days} 天。\n"
        f"当前到期时间：{beijing_str(new_exp)}（北京时间）。"
    )


# ──────────────────────────────────────────────
# 全局配置读写
# ──────────────────────────────────────────────

DEFAULT_WELCOME_TEXT = (
    "<b>👋 欢迎使用 TXT 文件处理机器人</b>\n\n"
    "选择下方按钮开始使用：\n\n"
    "📄 文件切割 —— 按行数切割 TXT，可选插入手机号\n"
    "🗜️ 解压Zip提取TXT —— 从 ZIP 中提取 TXT 文件\n"
    "✏️ 批量重命名 —— 批量重命名 TXT 文件\n"
    "📇 号码转VCF —— 手机号列表转通讯录 VCF\n"
    "🔄 号码去重 —— 去除重复手机号\n"
    "📊 号码分类 —— 按运营商/地区批量分组\n"
    "📚 合并文本 —— 多个 TXT 合并为一个\n"
    "🔀 定制分包 —— 识别字段类型并按指定顺序重排\n\n"
    "本机器人采用 <b>会员制</b>，请在「会员中心」兑换卡密后使用全部功能。"
)


def get_config(session, key: str, default: str = "") -> str:
    """读取配置项，不存在返回默认值。"""
    row = session.get(AppConfig, key)
    return row.value if row else default


def set_config(session, key: str, value: str) -> None:
    """写入配置项。"""
    row = session.get(AppConfig, key)
    if row:
        row.value = value
    else:
        session.add(AppConfig(key=key, value=value))
    session.commit()


def delete_config(session, key: str) -> None:
    """删除配置项。"""
    row = session.get(AppConfig, key)
    if row:
        session.delete(row)
        session.commit()


# ──────────────────────────────────────────────
# 管理员列表（数据库动态管理，总管理员通过管理面板增删）
# ──────────────────────────────────────────────

def get_admin_ids(session) -> list[int]:
    """读取数据库中的动态管理员 ID 列表。"""
    raw = get_config(session, "admin_ids", "")
    ids = []
    for x in raw.replace(",", " ").split():
        if x.strip().isdigit():
            ids.append(int(x.strip()))
    return ids


def add_admin_id(session, tg_id: int) -> bool:
    """添加动态管理员，已存在返回 False。"""
    ids = get_admin_ids(session)
    if tg_id in ids:
        return False
    ids.append(tg_id)
    set_config(session, "admin_ids", ",".join(str(i) for i in ids))
    return True


def remove_admin_id(session, tg_id: int) -> bool:
    """删除动态管理员，不存在返回 False。"""
    ids = get_admin_ids(session)
    if tg_id not in ids:
        return False
    ids.remove(tg_id)
    set_config(session, "admin_ids", ",".join(str(i) for i in ids))
    return True


# ──────────────────────────────────────────────
# 套餐配置（默认值，后台可改）
# 格式：JSON 字符串，[{"days": 7, "price": 5}, {"days": 30, "price": 15}, ...]
# ──────────────────────────────────────────────

DEFAULT_PLANS = '[{"days": 7, "price": 1}, {"days": 30, "price": 3}, {"days": 90, "price": 8}, {"days": 365, "price": 25}]'


def get_plans(session) -> list[dict]:
    """读取套餐列表，返回 [{days, price}, ...]。"""
    import json
    raw = get_config(session, "plans", DEFAULT_PLANS)
    try:
        plans = json.loads(raw)
        # 简单校验
        result = []
        for p in plans:
            if "days" in p and "price" in p:
                result.append({"days": int(p["days"]), "price": float(p["price"])})
        return sorted(result, key=lambda x: x["days"])
    except Exception:
        return [{"days": 7, "price": 5}, {"days": 30, "price": 15}]


def set_plans(session, plans: list[dict]) -> None:
    """保存套餐列表。"""
    import json
    set_config(session, "plans", json.dumps(plans, ensure_ascii=False))


# ---------------- 洗数据定价 ----------------

DEFAULT_WASH_PRICES = [
    {"min_rows": 0, "price_per_row": 0.0013},  # 约 0.13 厘/行
    {"min_rows": 100000, "price_per_row": 0.0010},  # 约 0.1 厘/行
]

DEFAULT_USDT_RATE = 6.8  # 1 USDT = 6.8 人民币（兜底用）


def _fetch_usdt_rate_online() -> float | None:
    """同步请求免费汇率 API，返回 USD→CNY 汇率；失败返回 None。

    注意：这是阻塞的网络请求，不要在事件循环里直接调用，
    async 场景请用 get_usdt_rate_async（内部丢线程池执行）。
    """
    try:
        import requests
        # 用免费的汇率 API
        resp = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        data = resp.json()
        return float(data.get("rates", {}).get("CNY", DEFAULT_USDT_RATE))
    except Exception:
        return None


def _fallback_usdt_rate(session) -> float:
    """汇率 API 不可用时：先取管理员配置的固定汇率，再兜底默认值。"""
    raw = get_config(session, "usdt_rate", "")
    if raw:
        try:
            return float(raw)
        except Exception:
            pass
    return DEFAULT_USDT_RATE


def get_usdt_rate(session) -> float:
    """获取 USDT 对人民币汇率（实时，失败则用配置/默认值）。同步版本。"""
    rate = _fetch_usdt_rate_online()
    return rate if rate is not None else _fallback_usdt_rate(session)


async def get_usdt_rate_async(session) -> float:
    """获取 USDT 汇率的异步版本：网络请求丢线程池，不阻塞机器人事件循环。"""
    rate = await asyncio.to_thread(_fetch_usdt_rate_online)
    return rate if rate is not None else _fallback_usdt_rate(session)


def set_usdt_rate(session, rate: float) -> None:
    """设置固定 USDT 对人民币汇率（实时失败时用这个兜底）。"""
    set_config(session, "usdt_rate", str(rate))


def get_wash_prices(session) -> list[dict]:
    """读取洗数据阶梯定价。
    格式：[{"min_rows": 0, "price_per_row": 0.0018}, {"min_rows": 100000, "price_per_row": 0.0014}]
    """
    import json
    raw = get_config(session, "wash_prices", "")
    if not raw:
        return DEFAULT_WASH_PRICES
    try:
        prices = json.loads(raw)
        result = []
        for p in prices:
            if "min_rows" in p and "price_per_row" in p:
                result.append({"min_rows": int(p["min_rows"]), "price_per_row": float(p["price_per_row"])})
        return sorted(result, key=lambda x: x["min_rows"])
    except Exception:
        return DEFAULT_WASH_PRICES


def set_wash_prices(session, prices: list[dict]) -> None:
    """保存洗数据阶梯定价。"""
    import json
    set_config(session, "wash_prices", json.dumps(prices, ensure_ascii=False))


# ---------------- 洗数据价格下限 / 累计配额 ----------------

DEFAULT_WASH_PRICE_FLOOR = 0.0010  # 每行最低单价（元），设置的档位单价不得低于此值


def get_wash_price_floor(session) -> float:
    """读取洗数据价格下限（元/行）。"""
    raw = get_config(session, "wash_price_floor", "")
    if raw:
        try:
            return float(raw)
        except Exception:
            pass
    return DEFAULT_WASH_PRICE_FLOOR


def set_wash_price_floor(session, value: float) -> None:
    """设置洗数据价格下限（元/行）。"""
    set_config(session, "wash_price_floor", str(value))


def get_wash_quota(session) -> int:
    """读取累计可洗数据总量（行数），0 表示不限制。"""
    raw = get_config(session, "wash_quota", "")
    if raw:
        try:
            return int(raw)
        except Exception:
            pass
    return 0


def set_wash_quota(session, value: int) -> None:
    """设置累计可洗数据总量（行数），0 表示不限制。"""
    set_config(session, "wash_quota", str(value))


# 待扣费任务超过该时长未回传视为作废，不再占用累计洗数据配额
WASH_PENDING_TTL_SECONDS = 48 * 60 * 60
# wash_code 映射的过期清理时间：多个结果文件共用同一映射，不能随首个文件到达删除，
# 只按提交时间戳做保守的 30 天清理，防止旧记录无限增长
WASH_CODE_TTL_SECONDS = 30 * 24 * 60 * 60


def get_washed_breakdown(session) -> tuple[int, int]:
    """累计已洗数据：返回 (已完成已扣费行数, 处理中待扣费行数)。

    待扣费任务格式：wash_pending:{随机码} = user_id:line_count:cost[:提交时间戳]
    带时间戳且超过 WASH_PENDING_TTL_SECONDS 未回传的任务视为作废：不再计入
    待扣费，并顺手删除，避免记录无限增长、配额被永久占用。
    旧格式（3 段、无时间戳）保持兼容，仍正常计入。
    """
    charged = session.scalar(
        select(func.coalesce(func.sum(WashOrder.row_count), 0)).where(WashOrder.type == "deduct")
    ) or 0
    pending = 0
    now = time.time()
    stale_rows = []
    # 待扣费任务：wash_pending:{随机码} = user_id:line_count:cost[:时间戳]
    for row in session.scalars(select(AppConfig).where(AppConfig.key.like("wash_pending:%"))):
        parts = (row.value or "").split(":")
        if len(parts) < 3 or not parts[1].isdigit():
            stale_rows.append(row)
            continue
        if len(parts) >= 4 and parts[3].isdigit():
            if now - int(parts[3]) > WASH_PENDING_TTL_SECONDS:
                stale_rows.append(row)
                continue
        pending += int(parts[1])
    # wash_code:{随机码} = user_id[:提交时间戳]：同一任务多个结果文件共用的映射，
    # 只按时间戳做过期清理；旧格式（无时间戳）保持原样不清除
    for row in session.scalars(select(AppConfig).where(AppConfig.key.like("wash_code:%"))):
        parts = (row.value or "").split(":")
        if len(parts) == 2 and parts[1].isdigit():
            if now - int(parts[1]) > WASH_CODE_TTL_SECONDS:
                stale_rows.append(row)
    for row in stale_rows:
        session.delete(row)
    if stale_rows:
        session.commit()
    return int(charged), int(pending)


def get_washed_total(session) -> int:
    """累计已洗数据行数 = 已扣费行数 + 待扣费行数（已提交但未完成的任务）。"""
    charged, pending = get_washed_breakdown(session)
    return charged + pending


def calc_wash_cost(session, row_count: int) -> float:
    """计算洗数据费用（人民币元），单价不低于价格下限。"""
    prices = get_wash_prices(session)
    floor = get_wash_price_floor(session)
    price_per_row = prices[0]["price_per_row"]
    for p in prices:
        if row_count >= p["min_rows"]:
            price_per_row = p["price_per_row"]
    price_per_row = max(price_per_row, floor)
    return round(row_count * price_per_row, 4)


# ---------------- 隐藏掉落（概率掉落会员天数） ----------------

DEFAULT_DROP_NOTIFY_TEMPLATE = (
    "🎁 会员掉落通知\n\n"
    "用户：{username}（{user_id}）\n"
    "使用功能：{feature}\n"
    "掉落天数：+{days} 天\n"
    "当前到期：{expires}\n\n"
    "已自动到账。"
)


def get_drop_notify_template(session) -> str:
    """读取掉落通知模板，未设置则用默认。"""
    raw = get_config(session, "drop_notify_template", "")
    return raw if raw else DEFAULT_DROP_NOTIFY_TEMPLATE


def set_drop_notify_template(session, template: str) -> None:
    """保存掉落通知模板。"""
    set_config(session, "drop_notify_template", template)


def get_drop_notify_chat(session) -> str:
    """读取掉落通知群 ID（未绑定返回空串）。"""
    return get_config(session, "drop_notify_chat", "")


def set_drop_notify_chat(session, chat_id: str) -> None:
    """绑定掉落通知群。"""
    set_config(session, "drop_notify_chat", str(chat_id))


def get_drop_stats(session) -> dict:
    """掉落统计：总次数/总天数、洗数据次数/天数、今日次数/今日天数。"""
    now = utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _agg(where_extra=None):
        q = select(func.coalesce(func.count(DropRecord.id), 0), func.coalesce(func.sum(DropRecord.days), 0))
        if where_extra is not None:
            q = q.where(where_extra)
        row = session.execute(q).one()
        return int(row[0]), int(row[1])

    total_cnt, total_days = _agg()
    wash_cnt, wash_days = _agg(DropRecord.is_wash.is_(True))
    today_cnt, today_days = _agg(DropRecord.created_at >= today_start)
    other_cnt = total_cnt - wash_cnt
    other_days = total_days - wash_days
    return {
        "total_cnt": total_cnt,
        "total_days": total_days,
        "wash_cnt": wash_cnt,
        "wash_days": wash_days,
        "other_cnt": other_cnt,
        "other_days": other_days,
        "today_cnt": today_cnt,
        "today_days": today_days,
    }


def get_drop_records(session, limit: int = 30) -> list[DropRecord]:
    """最近掉落记录（新→旧）。"""
    return list(
        session.scalars(
            select(DropRecord).order_by(DropRecord.id.desc()).limit(limit)
        )
    )


# ---------------- 抽奖系统（普通抽奖 / 高级抽奖 / 兑换） ----------------

DEFAULT_LOTTERY_PRIZES = [
    {"type": "days", "value": 1, "prob": 40},
    {"type": "days", "value": 2, "prob": 25},
    {"type": "days", "value": 3, "prob": 15},
    {"type": "days", "value": 5, "prob": 10},
    {"type": "days", "value": 7, "prob": 5},
    {"type": "wash_balance", "value": 2, "prob": 3},
    {"type": "ticket", "value": 1, "prob": 2},
]

DEFAULT_LOTTERY_PREMIUM_PRIZES = [
    {"type": "days", "value": 30, "prob": 45},
    {"type": "days", "value": 90, "prob": 30},
    {"type": "days", "value": 365, "prob": 15},
    {"type": "wash_balance", "value": 50, "prob": 10},
]

DEFAULT_LOTTERY_REDEEM = [
    {"cost": 1, "type": "days", "value": 10},
    {"cost": 2, "type": "wash_balance", "value": 20},
]

DEFAULT_LOTTERY_NOTIFY_TEMPLATE = (
    "🎰 抽奖到账通知\n\n"
    "用户：{username}（{user_id}）\n"
    "来源：{source}\n"
    "奖品：{prize}\n\n"
    "已自动到账。"
)


def _load_prizes(raw: str, default: list[dict]) -> list[dict]:
    import json
    if not raw:
        return default
    try:
        data = json.loads(raw)
        out = []
        for p in data:
            if "type" in p and "value" in p and "prob" in p:
                out.append({"type": p["type"], "value": float(p["value"]), "prob": float(p["prob"])})
        return out
    except Exception:
        return default


def get_lottery_prizes(session) -> list[dict]:
    """普通抽奖奖品池。"""
    return _load_prizes(get_config(session, "lottery_prizes", ""), DEFAULT_LOTTERY_PRIZES)


def set_lottery_prizes(session, prizes: list[dict]) -> None:
    import json
    set_config(session, "lottery_prizes", json.dumps(prizes, ensure_ascii=False))


def get_lottery_premium_prizes(session) -> list[dict]:
    """高级抽奖奖品池。"""
    return _load_prizes(get_config(session, "lottery_premium_prizes", ""), DEFAULT_LOTTERY_PREMIUM_PRIZES)


def set_lottery_premium_prizes(session, prizes: list[dict]) -> None:
    import json
    set_config(session, "lottery_premium_prizes", json.dumps(prizes, ensure_ascii=False))


def get_lottery_redeem(session) -> list[dict]:
    """高级券兑换价目表。"""
    import json
    raw = get_config(session, "lottery_redeem", "")
    if not raw:
        return DEFAULT_LOTTERY_REDEEM
    try:
        data = json.loads(raw)
        out = []
        for p in data:
            if "cost" in p and "type" in p and "value" in p:
                out.append({"cost": int(p["cost"]), "type": p["type"], "value": float(p["value"])})
        return out
    except Exception:
        return DEFAULT_LOTTERY_REDEEM


def set_lottery_redeem(session, items: list[dict]) -> None:
    import json
    set_config(session, "lottery_redeem", json.dumps(items, ensure_ascii=False))


def get_lottery_notify_template(session) -> str:
    raw = get_config(session, "lottery_notify_template", "")
    return raw if raw else DEFAULT_LOTTERY_NOTIFY_TEMPLATE


def set_lottery_notify_template(session, template: str) -> None:
    """保存抽奖通知模板（占位符：{user_id} {username} {source} {prize}）。"""
    set_config(session, "lottery_notify_template", template)


def beijing_today_start() -> datetime:
    """北京时间的今天 00:00，换算成 naive UTC（与 utcnow() 同尺度比较）。"""
    bj = utcnow() + timedelta(hours=8)
    return bj.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)


def get_today_lottery_record(session, user_id: int):
    """用户今天（北京时间）是否已抽过普通抽奖，返回最近一条记录或 None。"""
    start = beijing_today_start()
    return session.scalar(
        select(LotteryRecord)
        .where(
            LotteryRecord.user_id == user_id,
            LotteryRecord.source == "ordinary",
            LotteryRecord.created_at >= start,
        )
        .order_by(LotteryRecord.id.desc())
        .limit(1)
    )


def get_lottery_records(session, limit: int = 30) -> list[LotteryRecord]:
    """最近抽奖/兑换记录（新→旧）。"""
    return list(session.scalars(select(LotteryRecord).order_by(LotteryRecord.id.desc()).limit(limit)))


def get_lottery_stats(session) -> dict:
    today_start = beijing_today_start()
    total = session.scalar(select(func.count()).select_from(LotteryRecord)) or 0
    today = session.scalar(
        select(func.count()).select_from(LotteryRecord).where(LotteryRecord.created_at >= today_start)
    ) or 0
    return {"total": int(total), "today": int(today)}


def add_lottery_tickets(session, user_id: int, n: int) -> None:
    """给用户加高级抽奖次数。"""
    user = session.get(User, user_id)
    if user:
        user.lottery_tickets = (user.lottery_tickets or 0) + n
        session.commit()


def spend_lottery_ticket(session, user_id: int) -> bool:
    """消耗一张高级抽奖券，返回是否成功。"""
    user = session.get(User, user_id)
    if not user or (user.lottery_tickets or 0) < 1:
        return False
    user.lottery_tickets -= 1
    session.commit()
    return True
