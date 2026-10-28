"""洗数据：单独计费，按行数收费，转发到洗数据群处理。"""
import asyncio
import html
import os
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
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


async def _alert_unmatched(bot, filename: str, code: str) -> None:
    """结果文件匹配不到用户时私聊管理员，避免静默丢文件。"""
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

    if amount < 5:
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

        # 支付成功：先改订单状态再入账，同一次提交内完成，防止重复到账
        order.status = "paid"
        order.txid = data.get("txid", order.txid)

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

    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(WashStates.waiting_file)
    await cq.message.edit_text(
        "📤 <b>上传文件洗数据</b>\n\n"
        "请发送要洗的 TXT 文件，我会自动计算行数、扣余额，然后转发到洗数据群处理。\n\n"
        "处理完成后会自动把结果发回给你。",
        reply_markup=cancel_kb(),
    )


@router.message(WashStates.waiting_file, F.document)
async def wash_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".txt", ".text")):
        await message.answer("⚠️ 只接受 TXT 文件。")
        return

    data = await state.get_data()
    workdir = data["workdir"]
    tmp_path = os.path.join(workdir, doc.file_name or "input.txt")

    progress_msg = await message.answer("⏳ 正在下载文件并计算行数…")

    try:
        await progress_msg.edit_text("⏳ 正在下载文件…")
        file = await bot.get_file(doc.file_id)
        file_size = file.file_size

        # 大于20MB的文件提示联系客服
        if file_size > 20 * 1024 * 1024:
            size_mb = file_size / 1024 / 1024
            await progress_msg.edit_text(
                f"⚠️ 文件大小：{size_mb:.1f}MB\n\n"
                "大于20MB的文件请联系 @mmmmg 手动处理。"
            )
            await cleanup_state(state)
            return

        await bot.download_file(file.file_path, destination=tmp_path)

        await progress_msg.edit_text("⏳ 正在计算行数…")
        # 计算行数
        with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        line_count = len(lines)

        # 检查是不是只有手机号
        from utils.phone_db import extract_phones
        all_phones = []
        for ln in lines:
            all_phones.extend(extract_phones(ln))
        phone_count = len(all_phones)

        # 如果行数和手机号数量不一样，说明文件里不只有手机号
        if line_count != phone_count:
            await progress_msg.edit_text(
                f"⚠️ 文件里不只有手机号！\n\n"
                f"文件总行数：{line_count:,} 行\n"
                f"提取到手机号：{phone_count:,} 个\n\n"
                "请先使用「提取手机号」功能，把文件里的手机号提取出来，\n"
                "再用提取后的文件来洗数据。"
            )
            await cleanup_state(state)
            return

        # 检查累计洗数据配额
        await progress_msg.edit_text("⏳ 正在检查累计洗数据配额…")
        with SessionLocal() as session:
            quota = get_wash_quota(session)
            washed = get_washed_total(session)
        if quota > 0 and washed + line_count > quota:
            await progress_msg.edit_text(
                f"⚠️ 已达累计洗数据上限，无法继续洗！\n\n"
                f"累计已洗：{washed:,} 行\n"
                f"本次文件：{line_count:,} 行\n"
                f"总配额：{quota:,} 行（剩余 {max(0, quota - washed):,} 行）\n\n"
                "超出配额的部分暂不能洗，请联系管理员。",
                reply_markup=menu_kb(message.from_user.id),
            )
            await cleanup_state(state)
            return

        await progress_msg.edit_text("⏳ 正在计算费用…")
        # 计算费用
        with SessionLocal() as session:
            cost = calc_wash_cost(session, line_count)
            balance = get_wash_balance(session, message.from_user.id)
    except Exception as e:
        await progress_msg.edit_text(f"⚠️ 处理失败：{str(e)[:200]}")
        import traceback
        traceback.print_exc()
        return

    if balance < cost:
        await progress_msg.edit_text(
            f"⚠️ 余额不足！\n\n"
            f"文件行数：{line_count:,} 行\n"
            f"本次费用：¥{cost:.4f} 元\n"
            f"当前余额：¥{balance:.4f} 元\n"
            f"还差：¥{cost - balance:.4f} 元\n\n"
            "请先充值。",
            reply_markup=wash_main_kb(balance),
        )
        await cleanup_state(state)
        return

    user_id = message.from_user.id
    safe_name = doc.file_name or "input.txt"

    # 生成7位随机码（字母+数字），保护用户隐私
    import random
    import string
    random_code = ''.join(random.choices(string.ascii_letters + string.digits, k=7))

    # 暂不扣费，洗完收到文件后再扣
    # 存下来这次的费用和行数，洗完再扣；末尾附提交时间戳，
    # 超时未回传的任务会由 get_washed_breakdown 自动作废，不再占用配额
    with SessionLocal() as session:
        from database import set_config
        import time
        set_config(
            session,
            f"wash_pending:{random_code}",
            f"{user_id}:{line_count}:{cost}:{int(time.time())}",
        )

    # 存映射：随机码 → 用户ID（附时间戳，供过期清理）。
    # 注意：一份任务可能回传多个结果文件，它们共用同一个随机码，
    # 所以收到第一个文件后绝不能删除这条映射，否则后面的文件找不到用户。
    with SessionLocal() as session:
        from database import set_config
        import time
        set_config(
            session,
            f"wash_code:{random_code}",
            f"{user_id}:{int(time.time())}",
        )

    # 转发文件给小号，让小号转发到洗数据群（绕过机器人隐私模式限制）
    with SessionLocal() as session:
        wash_chat_id = get_config(session, "wash_chat_id", "")
        alt_username = get_config(session, "alt_username", "")
        alt_user_id = get_config(session, "alt_user_id", "")
        alt_session = get_config(session, "alt_session", "")

    # 如果用户名是空的，自动从会话里获取
    if (not alt_username or not alt_user_id) and alt_session:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            from config import settings
            temp_client = TelegramClient(StringSession(alt_session), settings.TELETHON_API_ID, settings.TELETHON_API_HASH)
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
            await progress_msg.edit_text(f"⚠️ 获取小号信息失败：{e}")
            await cleanup_state(state)
            return

    if not alt_user_id:
        await progress_msg.edit_text("⚠️ 小号未登录，请先在管理面板登录小号。")
        await cleanup_state(state)
        return

    user_id = message.from_user.id
    safe_name = doc.file_name or "input.txt"

    # 文件名加随机码前缀，方便洗好回来的时候识别
    new_filename = f"{random_code}_{safe_name}"

    # 发给小号（优先用 user_id，更可靠）
    chat_target = int(alt_user_id)
    try:
        await bot.send_document(
            chat_id=chat_target,
            document=FSInputFile(tmp_path, filename=new_filename),
            caption="im",
        )
    except Exception as e:
        if "chat not found" in str(e).lower():
            await progress_msg.edit_text(
                f"⚠️ 小号还没和我们的机器人建立会话！\n\n"
                f"请用小号给我们的机器人发一条消息（比如发个 /start），然后再试。"
            )
        else:
            await progress_msg.edit_text(f"⚠️ 转发给小号失败：{str(e)[:100]}")
        await cleanup_state(state)
        return

    await progress_msg.edit_text(
        f"✅ <b>已提交洗数据</b>\n\n"
        f"文件：{html.escape(doc.file_name or '')}\n"
        f"行数：{line_count:,} 行\n"
        f"扣除费用：¥{cost:.4f} 元\n"
        f"剩余余额：¥{balance - cost:.4f} 元\n\n"
        "────────────\n"
        "正在处理中，完成后会自动把结果发回给你，请稍等。",
        reply_markup=menu_kb(message.from_user.id),
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
            pending = get_config(session, f"wash_pending:{random_code}", "")
        if not user_id_str:
            print(f"⚠️ 洗数据随机码 {random_code} 没找到对应的用户")
            await _alert_unmatched(bot, filename, random_code)
            return
        # 兼容两种格式：旧格式纯 user_id，新格式 "user_id:时间戳"
        user_id = int(user_id_str.split(":")[0])

        # 提取原文件名：只取文件名本身，不要把消息 caption（如“蓝号(IOS/imessage/FaceTime)”）拼进去
        original_name = re.sub(r"^[a-zA-Z0-9]{7}_", "", filename).strip() or filename

        # 洗完了，现在扣费
        if pending:
            parts = pending.split(":")
            # 兼容旧格式（3段）与新格式（4段：末尾带提交时间戳）
            if len(parts) >= 3:
                line_count = int(parts[1])
                cost = float(parts[2])
                from database import deduct_wash_balance
                with SessionLocal() as session:
                    deduct_wash_balance(session, user_id, cost)
                    # 记录洗数据账单
                    order = WashOrder(
                        user_id=user_id,
                        filename=original_name,
                        row_count=line_count,
                        cost=cost,
                        type="deduct",
                    )
                    session.add(order)
                    session.commit()
                # 只删待扣费记录；wash_code 映射必须保留：
                # 同一任务的多个结果文件共用随机码，删了后续文件会丢失，
                # 它的过期清理由 get_washed_breakdown 按时间戳统一处理
                with SessionLocal() as session:
                    from database import delete_config
                    delete_config(session, f"wash_pending:{random_code}")

                # 洗数据完成，判定隐藏掉落（2%，不限制频率）
                try:
                    from utils.drops import notify_drop_chat, roll_drop
                    with SessionLocal() as session:
                        drop_days = roll_drop(session, user_id, "洗数据", is_wash=True)
                    if drop_days:
                        await notify_drop_chat(bot, user_id, "洗数据", drop_days)
                except Exception:
                    pass

        # 把洗好的文件转发给用户（带重试，避免连发时被限流丢掉）
        ok = await _send_document_retry(
            bot,
            user_id,
            doc.file_id,
            f"✅ 你的洗数据已完成！\n原文件：{html.escape(original_name)}",
        )
        if ok:
            print(f"✅ 已把洗好的文件发给用户 {user_id}")
        return
