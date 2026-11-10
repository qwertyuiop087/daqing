"""洗数据：单独计费，按行数收费，转发到洗数据群处理。"""
import asyncio
import html
import json
import math
import os
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import settings
from database import (
    PaymentOrder,
    SessionLocal,
    WashOrder,
    add_wash_balance,
    calc_wash_cost,
    get_config,
    get_usdt_rate_async,
    get_wash_balance,
    get_wash_quota,
    get_washed_total,
)
from states import WashStates
from utils.dali_client import get_client
from utils.helpers import cleanup_state, get_workdir
from utils.keyboards import cancel_kb, menu_kb


class FromAltUserFilter(BaseFilter):
    """只匹配小号发的消息"""
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        with SessionLocal() as session:
            alt_user_id = get_config(session, "alt_user_id", "")
        return str(message.from_user.id) == alt_user_id

router = Router(name="wash")
# 专门处理小号回传文件的路由，要放到最前面注册，避免被其他文件 handler 拦截
wash_reply_router = Router(name="wash_reply")


async def _send_document_retry(bot, chat_id: int, document, caption: str, attempts: int = 3) -> bool:
    """把结果文件发给用户，带重试。

    同一任务的多个结果文件几乎同时回传，连续发送偶尔会触发 Telegram
    限流（429）或网络抖动；这里自动等待重试，避免文件静默丢失。
    """
    from aiogram.exceptions import TelegramRetryAfter

    delay = 2.0
    for i in range(attempts):
        try:
            await bot.send_document(chat_id=chat_id, document=document, caption=caption)
            return True
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", 5)) + 1
            print(f"⏳ Telegram 限流，{wait:.0f}s 后重试发送给 {chat_id}")
            await asyncio.sleep(wait)
        except Exception as e:
            if i == attempts - 1:
                print(f"❌ 发送给 {chat_id} 最终失败: {e}")
                return False
            await asyncio.sleep(delay)
            delay *= 2
    return False


_last_unmatched_alert = 0.0


async def _alert_unmatched(bot, filename: str, code: str) -> None:
    """结果文件匹配不到用户时私聊管理员，避免静默丢文件（10 分钟节流，防止被刷屏）。"""
    global _last_unmatched_alert
    import time as _time
    now = _time.time()
    if now - _last_unmatched_alert < 600:
        return
    _last_unmatched_alert = now
    admin_ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
    if not admin_ids:
        return
    text = (
        "⚠️ <b>洗数据结果文件无法匹配用户</b>\n\n"
        f"文件：<code>{html.escape(filename)}</code>\n"
        f"随机码：<code>{html.escape(code)}</code>\n\n"
        "该文件没有转发给任何人，请检查 wash_code 映射是否丢失。"
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


def _washed_original_name(filename: str) -> str:
    """去掉 7 位随机码前缀，还原给用户看的原文件名。"""
    import re
    return re.sub(r"^[a-zA-Z0-9]{7}_", "", filename or "").strip() or (filename or "unnamed")


def _delivery_keys(code: str) -> list:
    """该任务已经投递过的结果文件键（用于去重，防止重复发/重复扣）。"""
    with SessionLocal() as session:
        raw = get_config(session, f"wash_sent:{code}", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _remember_delivery(code: str, key: str) -> None:
    from database import set_config

    keys = _delivery_keys(code)
    if key in keys:
        return
    keys = (keys + [key])[-100:]
    with SessionLocal() as session:
        set_config(session, f"wash_sent:{code}", json.dumps(keys, ensure_ascii=False))


def _already_delivered(code: str, key: str) -> bool:
    return key in _delivery_keys(code)


_last_undelivered_alert = 0.0


async def _alert_undelivered(bot, filename: str, code: str, user_id: int) -> None:
    """结果文件没能送达用户时私聊管理员（10 分钟节流），避免文件静默丢失。"""
    global _last_undelivered_alert
    import time as _time
    now = _time.time()
    if now - _last_undelivered_alert < 600:
        return
    _last_undelivered_alert = now
    admin_ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
    text = (
        "⚠️ <b>洗数据结果没能发给用户</b>\n\n"
        f"用户：<code>{user_id}</code>\n"
        f"文件：<code>{html.escape(filename)}</code>\n"
        f"随机码：<code>{html.escape(code)}</code>\n\n"
        "文件仍在洗数据群里，请手动转发给该用户；\n"
        "本次没有结算扣费，48 小时未回传会自动按比例退款。"
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


def _settle_wash_result(random_code: str, user_id: int, original_name: str) -> tuple:
    """回传成功后记账：批量任务每收到一份结算一份，全部回传才结清。

    返回 (结算金额, 结算行数, 是否已结清)。只在文件真正送达用户后调用，
    保证「没送达就不扣钱」。
    """
    with SessionLocal() as session:
        pending = get_config(session, f"wash_pending:{random_code}", "")
    if not pending:
        return (0.0, 0, True)

    parts = pending.split(":", 4)
    if len(parts) < 3:
        return (0.0, 0, True)
    line_count = int(parts[1])
    cost = float(parts[2])
    file_map = None
    if len(parts) >= 5:
        try:
            file_map = json.loads(parts[4])
        except Exception:
            file_map = None

    settle_cost = cost
    settle_lines = line_count
    done = True
    if file_map and file_map.get("done"):
        # 这个任务已经结清过（补发扫描可能还会命中它），不再重复扣费
        return (0.0, 0, True)
    if file_map and file_map.get("files"):
        rem = list(file_map["files"])
        charged = float(file_map.get("charged", 0.0))
        share = rem.pop(0)
        if rem:
            settle_cost = round(cost * share / line_count, 4) if line_count else 0.0
            settle_lines = share
            file_map["files"] = rem
            file_map["charged"] = round(charged + settle_cost, 4)
            done = False
        else:
            # 最后一个结果：把剩余金额一次结清，避免四舍五入差额
            settle_cost = round(cost - charged, 4)
            settle_lines = share

    if settle_cost > 0:
        with SessionLocal() as session:
            session.add(
                WashOrder(
                    user_id=user_id,
                    filename=original_name,
                    row_count=settle_lines,
                    cost=settle_cost,
                    type="deduct",
                )
            )
            session.commit()

    # 结清后不删记录，改成 done 标记：补发扫描在窗口内还能认出这个随机码，
    # 「刚结清那一刻没发出去的结果文件」才补得回来；到期由 TTL 统一清理。
    with SessionLocal() as session:
        from database import delete_config, set_config
        if done:
            if file_map is not None:
                file_map["files"] = []
                file_map["charged"] = cost
                file_map["done"] = 1
                set_config(
                    session,
                    f"wash_pending:{random_code}",
                    f"{user_id}:{line_count}:{cost}:{parts[3]}:"
                    f"{json.dumps(file_map, ensure_ascii=False)}",
                )
            else:
                delete_config(session, f"wash_pending:{random_code}")
        elif file_map is not None:
            set_config(
                session,
                f"wash_pending:{random_code}",
                f"{user_id}:{line_count}:{cost}:{parts[3]}:"
                f"{json.dumps(file_map, ensure_ascii=False)}",
            )
    return (settle_cost, settle_lines, done)


def wash_main_kb(balance: float) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="💳 充值洗数据", callback_data="wash:recharge")
    b.button(text="📤 上传文件洗数据", callback_data="wash:upload")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def wash_recharge_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="10 USDT", callback_data="wash:recharge:10")
    b.button(text="50 USDT", callback_data="wash:recharge:50")
    b.button(text="100 USDT", callback_data="wash:recharge:100")
    b.button(text="✏️ 自定义金额", callback_data="wash:recharge_custom")
    # OKPay 收款（配了商户号才显示）
    okpay_ready = False
    try:
        from utils.okaypay_client import is_configured as _okpay_ready
        okpay_ready = _okpay_ready()
    except Exception:
        okpay_ready = False
    if okpay_ready:
        b.button(text="🔵 OKPay 充值", callback_data="okpay:topup")
        b.button(text="🔙 返回", callback_data="menu:wash")
        b.adjust(3, 1, 1, 1)
        return b.as_markup()
    b.button(text="🔙 返回", callback_data="menu:wash")
    b.adjust(3, 1, 1)
    return b.as_markup()
@router.callback_query(F.data == "menu:wash")
async def menu_wash(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    with SessionLocal() as session:
        balance = get_wash_balance(session, cq.from_user.id)
        from database import get_wash_prices
        prices = get_wash_prices(session)
        quota = get_wash_quota(session)
        washed = get_washed_total(session)

    # 生成价格表
    price_text = ""
    for i, p in enumerate(prices):
        min_rows = p["min_rows"]
        # 下一档的 min 就是当前档的 max
        if i + 1 < len(prices):
            max_rows = prices[i + 1]["min_rows"] - 1
            price_text += f"  {min_rows:,} ~ {max_rows:,} 行：¥{p['price_per_row']:.4f}/行\n"
        else:
            price_text += f"  {min_rows:,} 行以上：¥{p['price_per_row']:.4f}/行\n"

    # 累计已洗数据量
    if quota > 0:
        remain = max(0, quota - washed)
        quota_text = (
            f"📊 累计已洗：<b>{washed:,}</b> 行 / 总配额：<b>{quota:,}</b> 行"
            f"（剩余 <b>{remain:,}</b> 行）\n"
            + ("⚠️ 配额已用完，暂不能提交新的洗数据任务！\n" if remain <= 0 else "")
        )
    else:
        quota_text = f"📊 累计已洗：<b>{washed:,}</b> 行（未设上限）\n"

    await cq.message.edit_text(
        "🧼 <b>洗数据</b>\n\n"
        f"当前余额：<b>¥{balance:.4f} 元</b>\n"
        f"{quota_text}"
        "────────────\n"
        "<b>阶梯定价（每行价格）：</b>\n"
        f"{price_text}\n"
        "洗的越多越便宜，洗完再扣费。",
        reply_markup=wash_main_kb(balance),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "wash:recharge")
async def wash_recharge(cq: CallbackQuery):
    await cq.answer()
    with SessionLocal() as session:
        rate = await get_usdt_rate_async(session)
    await cq.message.edit_text(
        "💳 <b>充值洗数据余额</b>\n\n"
        f"当前汇率：1 USDT = {rate} 元人民币\n\n"
        "选择充值金额（USDT）：\n"
        "支付方式：@dlqb 钱包 USDT\n"
        "充值后自动换算成人民币余额。",
        reply_markup=wash_recharge_kb(),
    )


@router.callback_query(F.data == "wash:recharge_custom")
async def wash_recharge_custom(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.edit_text(
        "✏️ <b>自定义充值金额</b>\n\n"
        "请输入要充值的 USDT 金额（至少 5 USDT）：",
        reply_markup=cancel_kb(),
    )
    await state.set_state(WashStates.waiting_custom_amount)


@router.message(WashStates.waiting_custom_amount, F.text)
async def wash_recharge_custom_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
    except Exception:
        await message.answer("金额格式错误，请输入数字，例如：10")
        return

    if not math.isfinite(amount) or amount < 5:
        await message.answer("最少充值 5 USDT，请重新输入：")
        return

    await cleanup_state(state)

    # 创建订单
    client = get_client()
    if not client:
        await message.answer("支付系统未配置，请联系管理员。")
        return

    try:
        order_id = f"wash_{uuid.uuid4().hex[:16]}"
        user_id = message.from_user.id

        # 同步 HTTP 丢线程池，避免卡住事件循环
        result = await asyncio.to_thread(
            client.call,
            "/merchant/payLink",
            {
                "amount": amount,
                "coin": "usdt",
                "userOrder": order_id,
                "name": f"洗数据充值${amount}",
            },
        )
    except Exception as e:
        await message.answer(f"创建订单失败: {str(e)[:100]}")
        return

    if not result.get("ok"):
        await message.answer(f"创建订单失败: {result.get('msg', '未知错误')}")
        return

    pay_data = result["data"]
    pay_url = pay_data.get("pay_url", "")

    # 订单落库：保证「我已支付」查询时只能到账一次（幂等）
    with SessionLocal() as session:
        session.add(
            PaymentOrder(
                order_id=order_id,
                user_id=user_id,
                days=0,
                amount=str(amount),
                coin="usdt",
                status="pending",
                pay_url=pay_url,
            )
        )
        session.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 去支付", url=pay_url)
    kb.button(text="✅ 我已支付，查询状态", callback_data=f"wash:check:{order_id}")
    kb.button(text="🔙 返回", callback_data="wash:recharge")
    kb.adjust(1)

    await message.answer(
        f"🧼 <b>洗数据充值订单</b>\n\n"
        f"充值金额：${amount} USDT\n"
        f"订单号：{order_id}\n\n"
        "────────────\n"
        "点击「去支付」完成付款，付款后点击「我已完成支付」。",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("wash:recharge:"))
async def wash_create_order(cq: CallbackQuery):
    await cq.answer()
    try:
        amount = float(cq.data.split(":")[2])
    except Exception as e:
        await cq.answer(f"参数错误: {e}", show_alert=True)
        return

    try:
        client = get_client()
    except Exception as e:
        await cq.answer(f"支付系统初始化失败: {e}", show_alert=True)
        return

    if not client:
        await cq.answer("支付系统未配置，请联系管理员。", show_alert=True)
        return

    try:
        order_id = f"wash_{uuid.uuid4().hex[:16]}"
        user_id = cq.from_user.id

        # 同步 HTTP 丢线程池，避免卡住事件循环
        result = await asyncio.to_thread(
            client.call,
            "/merchant/payLink",
            {
                "amount": amount,
                "coin": "usdt",
                "userOrder": order_id,
                "name": f"洗数据充值${amount}",
            },
        )
    except Exception as e:
        await cq.answer(f"创建订单失败: {str(e)[:100]}", show_alert=True)
        return

    if not result.get("ok"):
        await cq.answer(f"创建订单失败: {result.get('msg', '未知错误')}", show_alert=True)
        return

    pay_data = result["data"]
    pay_url = pay_data.get("pay_url", "")

    # 订单落库：保证「我已支付」查询时只能到账一次（幂等）
    with SessionLocal() as session:
        session.add(
            PaymentOrder(
                order_id=order_id,
                user_id=user_id,
                days=0,
                amount=str(amount),
                coin="usdt",
                status="pending",
                pay_url=pay_url,
            )
        )
        session.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 去支付", url=pay_url)
    kb.button(text="✅ 我已支付，查询状态", callback_data=f"wash:check:{order_id}")
    kb.button(text="🔙 返回", callback_data="wash:recharge")
    kb.adjust(1)

    await cq.message.edit_text(
        f"🧼 <b>洗数据充值订单</b>\n\n"
        f"充值金额：${amount} USDT\n"
        f"订单号：{order_id}\n\n"
        "────────────\n"
        "点击「去支付」完成付款，付款后点击「我已完成支付」。",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("wash:check:"))
async def wash_check_order(cq: CallbackQuery):
    order_id = cq.data.split(":")[2]

    client = get_client()
    if not client:
        await cq.answer("支付系统未配置。", show_alert=True)
        return

    with SessionLocal() as session:
        order = session.scalar(
            select(PaymentOrder).where(PaymentOrder.order_id == order_id)
        )
        if order is None:
            await cq.answer("订单不存在。", show_alert=True)
            return
        if order.user_id != cq.from_user.id:
            await cq.answer("这不是你的订单。", show_alert=True)
            return
        if order.status == "paid":
            # 幂等：已到账的订单无论点多少次都不再加余额
            await cq.answer("✅ 该订单已到账，请勿重复点击。", show_alert=True)
            return

    # 同步 HTTP 丢线程池，避免卡住事件循环
    result = await asyncio.to_thread(
        client.call, "/merchant/checkDeposit", {"userOrder": order_id}
    )
    if not result.get("ok"):
        await cq.answer("查询失败，请稍后再试。", show_alert=True)
        return

    data = result.get("data", {})
    statusText = data.get("statusText", "")
    amount = float(data.get("amount", 0) or 0)

    if statusText != "paid":
        statusTextCN = data.get("statusTextCN", "")
        await cq.answer(f"当前状态：{statusTextCN}，请完成支付后再查询。", show_alert=True)
        return

    # 支付成功：行锁 + 二次确认，与自动到账扫描互斥，防止同一订单被重复入账
    with SessionLocal() as session:
        locked = session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.order_id == order_id)
            .with_for_update()
        )
        if locked is None or locked.status == "paid":
            await cq.answer("该订单已到账。", show_alert=True)
            return
        locked.status = "paid"
        locked.txid = data.get("txid", locked.txid)

        rate = await get_usdt_rate_async(session)
        rmb_amount = amount * rate
        add_wash_balance(session, cq.from_user.id, rmb_amount)
        balance = get_wash_balance(session, cq.from_user.id)
        # 记录充值账单
        session.add(
            WashOrder(
                user_id=cq.from_user.id,
                filename=f"充值 ${amount} USDT",
                row_count=0,
                cost=rmb_amount,
                type="recharge",
            )
        )
        session.commit()

    await cq.answer("✅ 充值成功！", show_alert=True)
    await cq.message.edit_text(
        f"✅ <b>充值成功</b>\n\n"
        f"充值金额：${amount} USDT\n"
        f"换算汇率：1 USDT = {rate} 元\n"
        f"到账余额：¥{rmb_amount:.4f} 元\n"
        f"当前余额：¥{balance:.4f} 元",
        reply_markup=wash_main_kb(balance),
    )


# 一次洗数据任务最多收集的文件数（防止消息刷屏与发送限流）
WASH_MAX_FILES = 20

# 同一批文件的到达间隔很短（媒体组会几乎同时推送），
# 渲染时先等一下把这一批合并成一次编辑，避免疯狂编辑/触发限流
_UPLOAD_RENDER_DELAY = 0.8

# 每个用户一条上传会话：lock 串行化「追加文件 + 写状态」，
# dirty/task 保证同一时间只有一个渲染循环。
# aiogram 默认把每个 update 丢进独立 task 并发处理，
# 所以 8 个文件是 8 个 task 同时跑，光看 state 里的 status_msg_id 挡不住并发。
_UPLOAD_SESSIONS: dict = {}
_UPLOAD_SESSION_MAX = 500


def _upload_session(user_id: int) -> dict:
    sess = _UPLOAD_SESSIONS.get(user_id)
    if sess is None:
        if len(_UPLOAD_SESSIONS) >= _UPLOAD_SESSION_MAX:
            # 淘汰空闲会话，避免长期运行后字典无限增长
            for uid in [u for u, s in _UPLOAD_SESSIONS.items() if s["task"] is None]:
                _UPLOAD_SESSIONS.pop(uid, None)
                if len(_UPLOAD_SESSIONS) < _UPLOAD_SESSION_MAX:
                    break
        sess = {"lock": asyncio.Lock(), "dirty": False, "task": None}
        _UPLOAD_SESSIONS[user_id] = sess
    return sess


def wash_upload_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 开始洗数据", callback_data="wash:submit")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


def _upload_status_text(files: list, total: int, cost: float) -> str:
    """上传状态文本：文件清单 + 总行数 + 当前单价 + 预估费用。"""
    per = cost / total if total else 0
    shown = "\n".join(f"· {html.escape(f['name'])}（{f['lines']:,} 行）" for f in files[:10])
    if len(files) > 10:
        shown += f"\n… 还有 {len(files) - 10} 个文件"
    return (
        f"📥 已收到 <b>{len(files)}</b> 个文件，共 <b>{total:,}</b> 行\n"
        f"{shown}\n\n"
        f"当前单价：¥{per:.6f}/行 · 预估费用：¥{cost:.4f}\n\n"
        "继续发文件，或点「🚀 开始洗数据」。\n"
        f"（只收 TXT，一次最多 {WASH_MAX_FILES} 个）"
    )


def _edit_gone(err: Exception) -> bool:
    """判断这次编辑失败是不是「消息已经不能改了」，只有这种情况才需要重发一条。"""
    text = str(err).lower()
    if "not modified" in text:  # 内容没变化，不是错误
        return False
    return any(
        key in text
        for key in (
            "message to edit not found",
            "message_id_invalid",
            "message can't be edited",
            "message cannot be edited",
            "message identifier is not specified",
            "topic_deleted",
        )
    )


async def _render_upload_status(bot: Bot, state: FSMContext, chat_id: int, user_id: int) -> None:
    """请求刷新上传状态；同一批更新会合并成一次编辑。"""
    sess = _upload_session(user_id)
    sess["dirty"] = True
    task = sess.get("task")
    if task is not None and not task.done():
        return
    sess["task"] = asyncio.create_task(_upload_render_loop(bot, state, chat_id, user_id))


async def _upload_render_loop(bot: Bot, state: FSMContext, chat_id: int, user_id: int) -> None:
    """唯一的渲染循环：编辑同一条状态消息，绝不新建。"""
    sess = _upload_session(user_id)
    try:
        while sess["dirty"]:
            sess["dirty"] = False
            await asyncio.sleep(_UPLOAD_RENDER_DELAY)  # 等这一批文件都到齐
            if await state.get_state() != WashStates.waiting_file.state:
                return
            data = await state.get_data()
            files = list(data.get("files") or [])
            if not files:
                continue
            total = sum(f["lines"] for f in files)
            with SessionLocal() as session:
                cost = calc_wash_cost(session, total)
            text = _upload_status_text(files, total, cost)

            status_id = data.get("status_msg_id")
            rendered = False
            if status_id:
                try:
                    await bot.edit_message_text(
                        text, chat_id=int(data.get("status_chat_id") or chat_id),
                        message_id=int(status_id),
                        reply_markup=wash_upload_kb(), parse_mode="HTML",
                    )
                    rendered = True
                except Exception as e:
                    rendered = not _edit_gone(e)
                    if not rendered:
                        print(f"[wash-upload] status message not editable, sending a new one: {e}")
            if not rendered:
                sent = await bot.send_message(
                    chat_id, text, reply_markup=wash_upload_kb(), parse_mode="HTML"
                )
                await state.update_data(status_msg_id=sent.message_id, status_chat_id=chat_id)
    except Exception as e:
        print(f"[wash-upload] render loop failed: {e}")
    finally:
        sess["task"] = None
        if sess["dirty"]:
            # 渲染期间又来了新文件，继续一轮
            await _render_upload_status(bot, state, chat_id, user_id)


async def _stop_upload_render(user_id: int) -> None:
    """结束上传状态渲染循环，避免和「提交」抢同一条消息。"""
    sess = _UPLOAD_SESSIONS.get(user_id)
    if not sess:
        return
    sess["dirty"] = False
    task = sess.get("task")
    if task is not None and not task.done():
        # 直接取消刷新任务，最多等 0.5 秒：不能让「开始洗数据」卡在这里
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except Exception:
            pass


async def _edit_or_answer(cq: CallbackQuery, text: str, reply_markup=None) -> None:
    """优先把结果写回点按钮的那条消息，改不动再发新消息。"""
    try:
        await cq.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            await cq.message.answer(text, reply_markup=reply_markup)
        except Exception as e:
            print(f"⚠️ 洗数据结果消息发送失败：{e}")


def _refund_full_batch(user_id: int, total_lines: int, cost: float, code: str, reason: str) -> None:
    """退回整单预扣费用并作废任务（仅在文件还没转发给小号之前失败时调用）。"""
    with SessionLocal() as session:
        from database import WashOrder, add_wash_balance, delete_config
        add_wash_balance(session, user_id, cost)
        session.add(
            WashOrder(
                user_id=user_id,
                filename=reason,
                row_count=-total_lines,
                cost=-cost,
                type="refund",
            )
        )
        delete_config(session, f"wash_pending:{code}")
        session.commit()


@router.callback_query(F.data == "wash:upload")
async def wash_upload(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    # 检查有没有绑定洗数据群
    with SessionLocal() as session:
        wash_chat_id = get_config(session, "wash_chat_id", "")
    if not wash_chat_id:
        await cq.message.edit_text(
            "⚠️ 洗数据功能未配置，请联系管理员。",
            reply_markup=cancel_kb(),
        )
        return

    # 这条说明消息本身就是「上传状态消息」：后面每收到一个文件都编辑它，
    # 一次发 8 个文件也只有这一条消息在滚动更新，不会刷屏。
    await state.update_data(
        workdir=get_workdir(cq.from_user.id),
        files=[],
        status_msg_id=cq.message.message_id,
        status_chat_id=cq.message.chat.id,
    )
    await state.set_state(WashStates.waiting_file)
    await cq.message.edit_text(
        "📤 <b>上传文件洗数据（支持多文件）</b>\n\n"
        "把要洗的 TXT 文件全部发给我，可以一次发多个。\n\n"
        "费用按<b>全部文件的总行数</b>计算：\n"
        "文件越多、总行数越大，档位越高、单价越便宜。\n\n"
        "全部发完后点「🚀 开始洗数据」。",
        reply_markup=wash_upload_kb(),
    )


@router.message(WashStates.waiting_file, F.document)
async def wash_got_file(message: Message, state: FSMContext, bot: Bot):
    """收集文件：每收到一个 TXT 就存起来并更新汇总，提交时按总行数计费。

    一次发多个文件时它们是并发到达的（每个 update 一个 task）：
    下载并发做，只有「追加到列表 + 刷新状态消息」这一步串行化，
    保证始终只有一条状态消息在滚动更新。
    """
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".txt", ".text")):
        await message.answer("⚠️ 只接受 TXT 文件。")
        return

    data = await state.get_data()
    if len(list(data.get("files") or [])) >= WASH_MAX_FILES:
        await message.answer(
            f"⚠️ 一次最多 {WASH_MAX_FILES} 个文件。请点「🚀 开始洗数据」提交当前这批。",
            reply_markup=wash_upload_kb(),
        )
        return

    workdir = data.get("workdir") or get_workdir(message.from_user.id)

    # 下载文件（大小超限直接提示）。临时文件名带 uuid：
    # 并发下载时不能再用「列表长度+1」当序号，否则同一批文件会写到同一个路径。
    try:
        file = await bot.get_file(doc.file_id)
        if (file.file_size or 0) > settings.MAX_FILE_SIZE:
            await message.answer(
                f"⚠️ 文件 <code>{html.escape(doc.file_name or '')}</code> 超过 20MB 上限，已跳过。"
            )
            return
        from services.file_ops import sanitize_filename
        safe = sanitize_filename(doc.file_name or "input.txt")
        tmp_path = os.path.join(workdir, f"in_{uuid.uuid4().hex[:8]}_{safe}")
        await bot.download_file(file.file_path, destination=tmp_path)
    except Exception as e:
        await message.answer(f"⚠️ 下载失败：{str(e)[:100]}")
        return

    # 统计行数与手机号数量
    try:
        from services.file_ops import decode_bytes, read_file
        from utils.phone_db import extract_phones
        text = decode_bytes(read_file(tmp_path))
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        phones = 0
        for ln in lines:
            phones += len(extract_phones(ln))
    except Exception as e:
        await message.answer(f"⚠️ 读取文件失败：{str(e)[:100]}")
        return

    sess = _upload_session(message.from_user.id)
    async with sess["lock"]:
        # 锁内重新读：并发到达的其它文件可能刚写进列表
        cur = await state.get_data()
        files = list(cur.get("files") or [])
        if len(files) >= WASH_MAX_FILES:
            await message.answer(
                f"⚠️ 一次最多 {WASH_MAX_FILES} 个文件，这个文件没有加入本批。\n"
                "请点「🚀 开始洗数据」提交当前这批。",
                reply_markup=wash_upload_kb(),
            )
            return
        if not cur.get("status_chat_id"):
            cur["status_chat_id"] = message.chat.id
        files.append(
            {
                "name": doc.file_name or "input.txt",
                "path": tmp_path,
                "lines": len(lines),
                "phones": phones,
            }
        )
        await state.update_data(
            files=files,
            workdir=workdir,
            status_chat_id=cur["status_chat_id"],
        )

    await _render_upload_status(bot, state, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "wash:submit")
async def wash_submit(cq: CallbackQuery, state: FSMContext, bot: Bot):
    """提交整批文件（外壳）：任何异常都必须让用户看到提示，不能静默无响应。"""
    # 先立刻回一个可见的提示，避免用户点了按钮以为没反应
    await cq.answer("正在提交，请稍等…")
    try:
        await _wash_submit_impl(cq, state, bot)
    except Exception as e:
        print(f"[wash] submit failed: {e}")
        try:
            await _edit_or_answer(
                cq,
                "⚠️ 提交失败：" + html.escape(str(e)[:150])
                + "\n\n请再点一次「🚀 开始洗数据」；如果还是不行，重新上传一次文件。",
                reply_markup=menu_kb(cq.from_user.id),
            )
        except Exception:
            pass


async def _wash_submit_impl(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """提交整批文件：按总行数计费（档位越高单价越低），预扣费后转发到洗数据群。"""
    data = await state.get_data()
    files = list(data.get("files") or [])
    if not files:
        # 常见原因：机器人刚重启，内存里的上传状态丢了
        await cq.answer(
            "没找到上传的文件（可能是机器人刚重启）。\n请重新发一次文件再点开始洗数据。",
            show_alert=True,
        )
        return

    await _stop_upload_render(cq.from_user.id)

    total_lines = sum(f["lines"] for f in files)
    total_phones = sum(f["phones"] for f in files)

    # 行数和手机号数量不一致，说明文件里不只有手机号
    if total_lines != total_phones:
        await _edit_or_answer(
            cq,
            f"⚠️ 文件里不只有手机号！\n\n"
            f"文件总行数：{total_lines:,} 行\n"
            f"提取到手机号：{total_phones:,} 个\n\n"
            "请先用「提取手机号」功能，把文件里的手机号提取出来，"
            "再用提取后的文件来洗数据。",
            reply_markup=menu_kb(cq.from_user.id),
        )
        await cleanup_state(state)
        return

    # 检查累计洗数据配额
    with SessionLocal() as session:
        quota = get_wash_quota(session)
        washed = get_washed_total(session)
    if quota > 0 and washed + total_lines > quota:
        await _edit_or_answer(
            cq,
            f"⚠️ 已达累计洗数据上限，无法继续洗！\n\n"
            f"累计已洗：{washed:,} 行\n"
            f"本次文件：{total_lines:,} 行\n"
            f"总配额：{quota:,} 行（剩余 {max(0, quota - washed):,} 行）\n\n"
            "超出配额的部分暂不能洗，请联系管理员。",
            reply_markup=menu_kb(cq.from_user.id),
        )
        await cleanup_state(state)
        return

    # 按总行数计费
    with SessionLocal() as session:
        cost = calc_wash_cost(session, total_lines)
        balance = get_wash_balance(session, cq.from_user.id)
    if balance < cost:
        await _edit_or_answer(
            cq,
            f"⚠️ 余额不足！\n\n"
            f"总行数：{total_lines:,} 行\n"
            f"本次费用：¥{cost:.4f} 元\n"
            f"当前余额：¥{balance:.4f} 元\n"
            f"还差：¥{cost - balance:.4f} 元\n\n"
            "请先充值。",
            reply_markup=wash_main_kb(balance),
        )
        await cleanup_state(state)
        return

    # 提交时预扣费用：防止并发任务透支余额；发送失败会退回
    with SessionLocal() as session:
        from database import deduct_wash_balance
        if not deduct_wash_balance(session, cq.from_user.id, cost):
            await _edit_or_answer(
                cq,
                f"⚠️ 余额不足！\n\n本次费用：¥{cost:.4f} 元\n当前余额：¥{balance:.4f} 元\n\n请先充值。",
                reply_markup=wash_main_kb(balance),
            )
            await cleanup_state(state)
            return

    user_id = cq.from_user.id

    # 生成7位随机码（字母+数字），保护用户隐私；整批文件共用同一个随机码
    import random
    import string
    random_code = ''.join(random.choices(string.ascii_letters + string.digits, k=7))

    with SessionLocal() as session:
        from database import set_config
        import time
        # 批量任务：把每个文件的行数存进 pending（第 5 段 JSON），
        # 回传时按“收到几个结果结算几份”，一直没回传的部分超时按比例退款
        batch_map = json.dumps(
            {"files": [f["lines"] for f in files], "charged": 0.0},
            ensure_ascii=False,
        )
        set_config(
            session,
            f"wash_pending:{random_code}",
            f"{user_id}:{total_lines}:{cost}:{int(time.time())}:{batch_map}",
        )
        set_config(
            session,
            f"wash_code:{random_code}",
            f"{user_id}:{int(time.time())}",
        )

    # 读取小号信息
    with SessionLocal() as session:
        wash_chat_id = get_config(session, "wash_chat_id", "")
        alt_username = get_config(session, "alt_username", "")
        alt_user_id = get_config(session, "alt_user_id", "")
        alt_session = get_config(session, "alt_session", "")

    if (not alt_username or not alt_user_id) and alt_session:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            from config import settings as _settings
            temp_client = TelegramClient(StringSession(alt_session), _settings.TELETHON_API_ID, _settings.TELETHON_API_HASH)
            await temp_client.connect()
            me = await temp_client.get_me()
            alt_username = me.username or ""
            alt_user_id = str(me.id)
            with SessionLocal() as session:
                from database import set_config
                set_config(session, "alt_username", alt_username)
                set_config(session, "alt_user_id", alt_user_id)
            await temp_client.disconnect()
        except Exception as e:
            _refund_full_batch(user_id, total_lines, cost, random_code, "获取小号信息失败退款")
            await _edit_or_answer(cq, f"⚠️ 获取小号信息失败：{str(e)[:100]}\n\n费用已原路退回。")
            await cleanup_state(state)
            return

    if not alt_user_id:
        _refund_full_batch(user_id, total_lines, cost, random_code, "小号未登录退款")
        await _edit_or_answer(cq, "⚠️ 小号未登录，请先在管理面板登录小号。\n\n费用已原路退回。")
        await cleanup_state(state)
        return

    # 逐个转发给小号（文件名加随机码前缀，回传时靠它识别）
    sent_ok = 0
    send_error = None
    sent_names = []
    try:
        for f in files:
            new_filename = f"{random_code}_{f['name']}"
            await bot.send_document(
                chat_id=int(alt_user_id),
                document=FSInputFile(f["path"], filename=new_filename),
                caption="im",
            )
            sent_ok += 1
            sent_names.append(new_filename)
            await asyncio.sleep(0.6)  # 连发多个文件，稍作间隔避免限流
        # 记下我们自己发出的源文件名：补发扫描要跳过它们（否则会把原文件当结果再发一遍）
        if sent_names:
            with SessionLocal() as session:
                from database import set_config
                set_config(
                    session,
                    f"wash_src:{random_code}",
                    json.dumps(sent_names, ensure_ascii=False),
                )
    except Exception as e:
        send_error = str(e)
        if sent_ok == 0:
            # 一个都没发出去：整单退款并作废
            _refund_full_batch(user_id, total_lines, cost, random_code, "发送失败退款")
            if "chat not found" in send_error.lower():
                tip = (
                    "⚠️ 小号还没和我们的机器人建立会话！\n\n"
                    "请用小号给我们的机器人发一条消息（比如发个 /start），然后再试。"
                )
            else:
                tip = f"⚠️ 转发给小号失败：{send_error[:100]}"
            await _edit_or_answer(cq, f"{tip}\n\n费用已原路退回。", reply_markup=menu_kb(user_id))
            await cleanup_state(state)
            return
        # 部分已发出：只退回未发出部分的费用，已发出的继续等回传结算
        sent_files = files[:sent_ok]
        unsent_files = files[sent_ok:]
        sent_total = sum(f["lines"] for f in sent_files)
        unsent_lines = sum(f["lines"] for f in unsent_files)
        refund = round(cost * unsent_lines / total_lines, 4)
        remain_cost = round(cost - refund, 4)
        with SessionLocal() as session:
            from database import WashOrder, add_wash_balance, set_config
            add_wash_balance(session, user_id, refund)
            session.add(
                WashOrder(
                    user_id=user_id,
                    filename="部分发送失败退款",
                    row_count=-unsent_lines,
                    cost=-refund,
                    type="refund",
                )
            )
            # 更新 pending：行数与费用都改为「已发出部分」，结算按已发出文件的比例分摊
            remain_map = json.dumps(
                {"files": [f["lines"] for f in sent_files], "charged": 0.0},
                ensure_ascii=False,
            )
            set_config(
                session,
                f"wash_pending:{random_code}",
                f"{user_id}:{sent_total}:{remain_cost}:{int(time.time())}:{remain_map}",
            )
            session.commit()
        await _edit_or_answer(
            cq,
            f"⚠️ 转发到第 {sent_ok + 1} 个文件时失败：{send_error[:100]}\n\n"
            f"已发出 <b>{sent_ok}</b> 个文件（继续处理，完成后回传）；\n"
            f"未发出的 <b>{len(unsent_files)}</b> 个文件已退回 ¥{refund:.4f}。",
            reply_markup=menu_kb(user_id),
        )
        await cleanup_state(state)
        return

    await _edit_or_answer(
        cq,
        f"✅ <b>已提交洗数据</b>\n\n"
        f"文件数：{sent_ok} 个\n"
        f"总行数：{total_lines:,} 行\n"
        f"扣除费用：¥{cost:.4f} 元\n"
        f"剩余余额：¥{balance - cost:.4f} 元\n\n"
        "────────────\n"
        "正在处理中，完成后会自动把结果发回给你，请稍等。",
        reply_markup=menu_kb(user_id),
    )
    await cleanup_state(state)


# ---------------- 监听小号回传的所有文件，判断是洗数据还是空号检测 ----------------

@wash_reply_router.message(F.document, FromAltUserFilter())
async def alt_user_reply(message: Message, bot: Bot):
    """监听小号发回的文件，判断是洗数据还是空号检测，分别处理。"""
    doc = message.document
    if not doc:
        return

    import re
    from database import get_config
    filename = doc.file_name or "unnamed"
    caption = message.caption or ""
    search_text = filename + " " + caption
    print(f"📥 收到小号回传文件: 文件名={filename}, caption={caption[:50]}")

    # 判断是不是空号检测的结果
    m_phone = re.search(r"([a-zA-Z0-9]{7})_号码检测", search_text)
    if m_phone:
        # 空号检测的处理
        random_code = m_phone.group(1)
        print(f"📱 空号检测结果: 随机码={random_code}")

        # 只发带分类的结果文件，不发源文件
        if not any(keyword in filename for keyword in ["活跃号", "空号", "沉默号", "风险号"]):
            print(f"⚠️ 这不是结果文件，跳过: {filename}")
            return

        # 从数据库查随机码对应的用户ID
        with SessionLocal() as session:
            user_id_str = get_config(session, f"phone_check_code:{random_code}", "")

        if not user_id_str:
            print(f"⚠️ 随机码 {random_code} 没找到对应的用户")
            await _alert_unmatched(bot, filename, random_code)
            return

        user_id = int(user_id_str)

        ok = await _send_document_retry(
            bot,
            user_id,
            doc.file_id,
            f"✅ 你的空号检测已完成！\n文件：{filename}",
        )
        if ok:
            print(f"✅ 已把空号检测结果发给用户 {user_id}")
        else:
            await _alert_undelivered(bot, filename, random_code, user_id)
        return

    # 判断是不是洗数据的结果
    m_wash = re.search(r"([a-zA-Z0-9]{7})_", search_text)
    print(f"🔍 洗数据判断: 匹配到={m_wash}, search_text={search_text[:100]}")
    if m_wash:
        # 洗数据的处理
        random_code = m_wash.group(1)
        print(f"🧺 洗数据结果: 随机码={random_code}")

        with SessionLocal() as session:
            user_id_str = get_config(session, f"wash_code:{random_code}", "")
        if not user_id_str:
            print(f"⚠️ 洗数据随机码 {random_code} 没找到对应的用户")
            await _alert_unmatched(bot, filename, random_code)
            return
        # 兼容两种格式：旧格式纯 user_id，新格式 "user_id:时间戳"
        user_id = int(user_id_str.split(":")[0])

        # 提取原文件名：只取文件名本身，不要把消息 caption（如“蓝号(IOS/imessage/FaceTime)”）拼进去
        original_name = _washed_original_name(filename)

        # ① 先把文件交给用户：这是用户唯一关心的结果。
        #    记账放在后面，记账只要出一点问题都不能影响交付（以前是先记账后发送，
        #    记账一抛异常就整条 update 被全局错误处理器吞掉，用户永远收不到文件）。
        dkey = f"{filename}|{getattr(doc, 'file_size', 0) or 0}"
        if _already_delivered(random_code, dkey):
            print(f"⚠️ 这个结果文件已经发过了，跳过重复投递: {filename}")
            return
        ok = await _send_document_retry(
            bot,
            user_id,
            doc.file_id,
            f"✅ 你的洗数据已完成！\n原文件：{html.escape(original_name)}",
        )
        if not ok:
            # 没送到用户手上：不记账（不扣钱），通知管理员人工补发
            await _alert_undelivered(bot, filename, random_code, user_id)
            return
        _remember_delivery(random_code, dkey)
        print(f"✅ 已把洗好的文件发给用户 {user_id}")

        # ② 送达后再记账（费用提交时已预扣，这里只记流水，不重复扣费）
        try:
            settle_cost, settle_lines, done = _settle_wash_result(
                random_code, user_id, original_name
            )
            print(f"💳 结算：{settle_lines} 行 / ¥{settle_cost}，本任务{'已结清' if done else '还有未回传部分'}")
        except Exception as e:
            print(f"[wash] settle failed for {random_code}: {e}")
            admin_ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        "⚠️ <b>洗数据结算失败</b>\n\n"
                        f"用户：<code>{user_id}</code>\n"
                        f"随机码：<code>{random_code}</code>\n"
                        f"文件：<code>{html.escape(filename)}</code>\n"
                        f"错误：<code>{html.escape(str(e)[:200])}</code>\n\n"
                        "文件已发给用户，只有账单没记上，请手动核对。",
                    )
                except Exception:
                    pass

        # 洗数据完成，判定隐藏掉落（2%，不限制频率）
        try:
            from utils.drops import notify_drop_chat, roll_drop
            with SessionLocal() as session:
                drop_days = roll_drop(session, user_id, "洗数据", is_wash=True)
            if drop_days:
                await notify_drop_chat(bot, user_id, "洗数据", drop_days)
        except Exception:
            pass
        return


@router.message(Command("resend", "补发"))
async def wash_resend_cmd(message: Message, bot: Bot):
    """管理员手动补发：/resend [随机码]

    不带随机码：立刻扫一轮洗数据群，把漏掉的结果文件补发，并回报扫描情况；
    带随机码：只处理这个任务。
    任何情况下都会回复（不是管理员也会说明原因），不会静默无响应。
    """
    from utils.roles import is_admin
    from utils import wash_resend as rs

    if not is_admin(message.from_user.id):
        await message.answer(
            "⚠️ 这个命令只有管理员能用。\n"
            f"你的 Telegram ID：<code>{message.from_user.id}</code>"
        )
        return

    parts = (message.text or "").split(maxsplit=1)
    only = parts[1].strip() if len(parts) > 1 else ""
    try:
        notice = await message.answer("🔍 正在扫描洗数据群…")
    except Exception:
        notice = None
    try:
        # 不带随机码 = 只检查、不发送（避免把已经发过的历史文件再发一遍）；
        # 带随机码 = 真的补发这个任务，不要求任务记录还在
        stat = await rs._scan(
            bot,
            only_code=only or None,
            require_pending=not bool(only),
            dry_run=not bool(only),
        )
        text = rs.format_report(stat)
    except Exception as e:
        text = f"⚠️ 扫描失败：<code>{html.escape(str(e)[:300])}</code>"
    if notice is not None:
        try:
            await notice.edit_text(text)
            return
        except Exception:
            pass
    await message.answer(text)


# 也挂到最先注册的回传路由上：确保 /resend 不会被任何其它路由抢先吃掉
wash_reply_router.message.register(wash_resend_cmd, Command("resend", "补发"))


# ---------------- 兜底：文件没被任何功能接住 ----------------
# 放在 wash.router 末尾（wash 是最先注册回传、最后注册普通路由的），
# 只有在所有功能的状态处理器都没匹配时才会走到这里。
# 典型场景：点了「上传文件」后机器人重启，内存里的上传状态丢失，
# 用户再发文件就没人接——以前是完全没反应，现在给出明确提示。

@router.message(F.document)
async def document_fallback(message: Message, state: FSMContext):
    await cleanup_state(state)
    await message.answer(
        "⚠️ <b>没认出这个文件要做什么</b>\n\n"
        "可能是刚才机器人重启，上传状态丢了。\n"
        "请从主菜单重新选择功能，再发文件：\n\n"
        "· 洗数据：主菜单 → 🧼 洗数据 → 📤 上传文件洗数据\n"
        "· 其它功能：主菜单里点对应按钮，再发文件",
        reply_markup=menu_kb(message.from_user.id),
    )
