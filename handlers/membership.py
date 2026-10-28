"""会员中心 + 管理面板（卡密生成/删除/查询/统计、成员列表、加减天数、全站广播）。"""
import asyncio
import html
import os
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import settings
from database import (
    SessionLocal,
    User,
    add_user_days,
    beijing_str,
    create_cards,
    deduct_user_days,
    delete_card,
    find_user,
    get_config,
    get_drop_notify_chat,
    get_drop_notify_template,
    get_drop_records,
    get_drop_stats,
    get_or_create_user,
    get_plans,
    is_member,
    redeem_card,
    remaining_days,
    set_config,
    set_drop_notify_chat,
    set_drop_notify_template,
    set_plans,
)
from states import AdminStates, RedeemStates
from utils.helpers import cleanup_state
from utils.keyboards import admin_kb, cancel_kb, membership_kb, menu_kb
from utils.roles import is_admin, is_super_admin

router = Router(name="membership")

_PROMPT_USER_DAYS = (
    "请输入「用户ID或用户名 天数」，例如：\n"
    "<code>123456789 30</code>\n"
    "<code>@zhangsan 15</code>\n\n"
    "天数需在 1~3650 之间。"
)


def _parse_user_days(text: str):
    """解析「用户 天数」输入，返回 (用户key, 天数)，格式错误返回 None。"""
    parts = (text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    days = int(parts[1])
    if not (1 <= days <= 3650):
        return None
    return parts[0], days


@router.callback_query(F.data == "menu:membership")
async def cb_membership(cq: CallbackQuery):
    with SessionLocal() as session:
        user = get_or_create_user(
            session,
            cq.from_user.id,
            cq.from_user.username or "",
            cq.from_user.first_name or "",
        )
        if is_member(user):
            from database import beijing_str, get_wash_balance
            wash_balance = get_wash_balance(session, cq.from_user.id)
            status = (
                f"✅ <b>会员有效</b>\n\n"
                f"到期时间：{beijing_str(user.membership_expires_at)}（北京时间）\n"
                f"剩余天数：约 <b>{remaining_days(user)}</b> 天\n\n"
                f"🧼 洗数据余额：¥{wash_balance:.4f} 元"
            )
        else:
            from database import get_wash_balance
            wash_balance = get_wash_balance(session, cq.from_user.id)
            status = (
                "❌ <b>未开通 / 已到期</b>\n\n兑换卡密后即可使用全部功能。\n\n"
                f"🧼 洗数据余额：¥{wash_balance:.4f} 元"
            )
    await cq.answer()
    await cq.message.edit_text(f"🎫 <b>会员中心</b>\n\n{status}", reply_markup=membership_kb())


@router.callback_query(F.data == "membership:redeem")
async def cb_redeem(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.edit_text(
        "🎫 <b>兑换卡密</b>\n\n请输入卡密（格式如 <code>XXXXX-XXXXX-XXXXX</code>）：",
        reply_markup=cancel_kb(),
    )
    await state.set_state(RedeemStates.waiting_code)


@router.message(RedeemStates.waiting_code, F.text)
async def msg_redeem(message: Message, state: FSMContext):
    with SessionLocal() as session:
        get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or "",
        )
        ok, text = redeem_card(session, message.from_user.id, message.text)
    await message.answer(text, reply_markup=menu_kb(message.from_user.id))
    # 兑换成功后，发会员群信息
    if ok:
        await message.answer(
            "🎉 <b>恭喜你成为会员！</b>\n\n"
            "👉 会员群：@youzui\n"
            "进群获取最新资源和通知。",
            parse_mode="HTML",
        )
    await cleanup_state(state)


# ---------------- 管理面板 ----------------


@router.message(Command("admin"))
@router.callback_query(F.data == "menu:admin")
async def admin_panel(event):
    if not is_admin(event.from_user.id):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ 无权限", show_alert=True)
        else:
            await event.answer("⛔ 无权限")
        return

    from database import get_wash_price_floor, get_wash_quota, get_washed_total
    with SessionLocal() as session:
        floor = get_wash_price_floor(session)
        quota = get_wash_quota(session)
        washed = get_washed_total(session)
    quota_text = f"{quota:,} 行" if quota > 0 else "不限"
    role = "👑 总管理员" if is_super_admin(event.from_user.id) else "🔧 管理员"
    text = (
        f"🔧 <b>管理面板（{role}）</b>\n\n"
        f"💲 洗数据价格下限：¥{floor:.4f} 元/行\n"
        f"📊 累计洗数据配额：{quota_text}（已洗 {washed:,} 行）\n\n"
        "请选择操作："
    )
    kb = admin_kb(is_super=is_super_admin(event.from_user.id))
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin:gen")
async def admin_gen(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "🎫 <b>生成卡密</b>\n\n请输入「数量 天数」，例如：<code>10 30</code>（生成 10 个、每个 30 天）\n\n"
        "数量 1~500，天数 1~3650。",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.gen_cards)


@router.message(AdminStates.gen_cards, F.text)
async def admin_gen_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("格式错误，请按「数量 天数」输入，例如：<code>10 30</code>")
        return
    count = int(parts[0])
    days = int(parts[1])
    if not (1 <= count <= 500 and 1 <= days <= 3650):
        await message.answer("数量需在 1~500，天数需在 1~3650 之间。")
        return
    with SessionLocal() as session:
        codes = create_cards(session, count, days, message.from_user.id)
    if len(codes) <= 30:
        await message.answer(
            f"✅ 已生成 <b>{len(codes)}</b> 个卡密（每个 {days} 天）：\n\n"
            + "\n".join(f"<code>{c}</code>" for c in codes)
        )
    else:
        import tempfile
        import uuid
        # 用唯一临时路径，避免多个管理员并发生成时互相覆盖
        path = os.path.join(tempfile.gettempdir(), f"cards_{uuid.uuid4().hex}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(codes))
        try:
            await message.answer_document(
                FSInputFile(path, filename="卡密.txt"),
                caption=f"✅ 已生成 {len(codes)} 个卡密（每个 {days} 天），详见文件。",
            )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    await cleanup_state(state)


@router.callback_query(F.data == "admin:unused")
async def admin_unused(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    with SessionLocal() as session:
        from sqlalchemy import select

        from database import CardKey

        rows = list(
            session.execute(
                select(CardKey).where(CardKey.used_by.is_(None)).order_by(CardKey.id.desc())
            ).scalars()
        )
    await cq.answer()
    if not rows:
        await cq.message.edit_text("📋 当前没有未使用的卡密。", reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)))
        return
    lines = [f"{r.code}（{r.days}天）" for r in rows[:50]]
    text = f"📋 <b>未使用卡密：{len(rows)} 个</b>\n\n" + "\n".join(f"<code>{l}</code>" for l in lines)
    if len(rows) > 50:
        text += f"\n\n…仅显示前 50 个"
    await cq.message.edit_text(text, reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)))


@router.callback_query(F.data == "admin:stats")
async def admin_stats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    from sqlalchemy import func, select

    from database import CardKey

    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(CardKey)) or 0
        unused = session.scalar(
            select(func.count()).select_from(CardKey).where(CardKey.used_by.is_(None))
        ) or 0
        used = total - unused
        used_days = session.scalar(
            select(func.coalesce(func.sum(CardKey.days), 0)).where(CardKey.used_by.is_not(None))
        ) or 0
    await cq.answer()
    await cq.message.edit_text(
        "📊 <b>卡密统计</b>\n\n"
        f"全部卡密：<b>{total}</b> 个\n"
        f"未使用：<b>{unused}</b> 个\n"
        f"已使用：<b>{used}</b> 个\n"
        f"已发放总天数：<b>{used_days}</b> 天",
        reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
    )


# ---------------- 成员列表 ----------------

# 用户名自动刷新：每 24 小时最多跑一次，单次最多刷 500 人，避免限流
USERNAME_REFRESH_SECONDS = 24 * 60 * 60
USERNAME_REFRESH_MAX = 500


async def _refresh_usernames(bot: Bot) -> None:
    """后台刷新用户的当前用户名/名字（每 24 小时最多跑一次）。

    Telegram 用户改名字后，机器人只有再次收到该用户消息才会更新数据库；
    这里用 get_chat 主动拉一次，让成员列表里的用户名保持最新。
    失败（用户屏蔽机器人/已注销）静默跳过。
    """
    try:
        with SessionLocal() as session:
            last = get_config(session, "username_check_ts", "")
            if last.isdigit() and time.time() - int(last) < USERNAME_REFRESH_SECONDS:
                return
            user_ids = list(session.scalars(select(User.id)).all())
        if not user_ids:
            return
        changed = 0
        checked = 0
        for uid in user_ids[:USERNAME_REFRESH_MAX]:
            try:
                chat = await bot.get_chat(uid)
                username = getattr(chat, "username", "") or ""
                first_name = getattr(chat, "first_name", "") or ""
                if username or first_name:
                    with SessionLocal() as session:
                        user = session.get(User, uid)
                        if user:
                            if username and username != user.username:
                                user.username = username
                                changed += 1
                            if first_name and first_name != user.first_name:
                                user.first_name = first_name
                            session.commit()
            except Exception:
                pass
            checked += 1
            if checked % 20 == 0:
                await asyncio.sleep(0.5)  # 分批放缓，避免触发 Telegram 限流
        with SessionLocal() as session:
            set_config(session, "username_check_ts", str(int(time.time())))
        print(f"✅ 用户名刷新完成：检查 {checked} 人，更新 {changed} 人")
    except Exception as e:
        print(f"⚠️ 用户名刷新失败：{e}")


@router.callback_query(F.data == "admin:members")
async def admin_members(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    # 后台异步刷新用户名（每 24 小时最多一次），列表先按现有数据显示
    asyncio.create_task(_refresh_usernames(cq.bot))
    with SessionLocal() as session:
        users = list(session.scalars(select(User).order_by(User.registered_at.desc())))
    await cq.answer()
    if not users:
        await cq.message.edit_text("👥 暂无用户。", reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)))
        return

    lines = []
    from database import beijing_str
    for u in users:
        name = html.escape(u.username or u.first_name or str(u.id))
        # 已过期的会员按「无会员」显示，不再显示过期的到期时间
        status = (
            "会员至 " + beijing_str(u.membership_expires_at)
            if is_member(u)
            else "无会员"
        )
        wash_balance = u.wash_balance or 0
        lines.append(f"{u.id} | @{name} | {status} | 洗数据余额:¥{wash_balance:.2f}")

    header = f"👥 <b>成员列表：共 {len(users)} 人</b>\n\n"
    if len(lines) <= 30:
        await cq.message.edit_text(
            header + "\n".join(f"<code>{l}</code>" for l in lines),
            reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
        )
    else:
        import tempfile
        import uuid
        path = os.path.join(tempfile.gettempdir(), f"members_{uuid.uuid4().hex}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        try:
            await cq.message.edit_text(
                header + "\n".join(f"<code>{l}</code>" for l in lines[:20]) + "\n\n…完整列表见文件",
                reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
            )
            await cq.message.answer_document(FSInputFile(path, filename="成员列表.txt"))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------- 添加 / 扣除天数 ----------------


@router.callback_query(F.data == "admin:add")
async def admin_add_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "➕ <b>添加会员天数</b>\n\n" + _PROMPT_USER_DAYS,
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.adjust_add)


@router.message(AdminStates.adjust_add, F.text)
async def admin_add_input(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    parsed = _parse_user_days(message.text)
    if parsed is None:
        await message.answer("格式错误，请按「用户ID或用户名 天数」输入，例如：<code>123456789 30</code>")
        return
    key, days = parsed
    with SessionLocal() as session:
        user = find_user(session, key)
        if user is None:
            await message.answer(f"❌ 找不到用户 <b>{html.escape(key)}</b>（用户需先使用过机器人）。")
            return
        text = add_user_days(session, user, days)
        notify_text = (
            "🎉 <b>会员到账通知</b>\n\n"
            f"管理员为你增加了 <b>{days} 天</b> 会员时长！\n"
            f"当前会员到期时间：<b>{beijing_str(user.membership_expires_at)}</b>（北京时间）\n\n"
            "感谢你的使用，祝你愉快～"
        )
    await message.answer(text)
    # 通知用户本人（用户屏蔽机器人时静默忽略）
    try:
        await bot.send_message(user.id, notify_text, parse_mode="HTML")
    except Exception:
        pass
    await cleanup_state(state)


@router.callback_query(F.data == "admin:deduct")
async def admin_deduct_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "➖ <b>扣除会员天数</b>\n\n" + _PROMPT_USER_DAYS,
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.adjust_deduct)


@router.message(AdminStates.adjust_deduct, F.text)
async def admin_deduct_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    parsed = _parse_user_days(message.text)
    if parsed is None:
        await message.answer("格式错误，请按「用户ID或用户名 天数」输入，例如：<code>@zhangsan 15</code>")
        return
    key, days = parsed
    with SessionLocal() as session:
        user = find_user(session, key)
        if user is None:
            await message.answer(f"❌ 找不到用户 <b>{html.escape(key)}</b>（用户需先使用过机器人）。")
            return
        text = deduct_user_days(session, user, days)
    await message.answer(text)
    await cleanup_state(state)


# ---------------- 全站广播 ----------------


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "📢 <b>全站广播</b>\n\n请输入要广播的内容（纯文本），将发送给所有使用过机器人的用户。",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.broadcast)


@router.message(AdminStates.broadcast, F.text)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    content = (message.text or "").strip()
    if not content:
        await message.answer("广播内容不能为空。")
        return
    with SessionLocal() as session:
        user_ids = list(session.scalars(select(User.id)).all())

    total = len(user_ids)
    if total == 0:
        await message.answer("还没有任何用户。")
        await cleanup_state(state)
        return

    # 先发一条进度消息
    progress_msg = await message.answer(
        f"📢 <b>正在广播…</b>\n\n"
        f"总数：{total} 人\n"
        f"已发送：0 / {total}（成功 0，失败 0）"
    )

    ok_cnt = 0
    fail_blocked = 0    # 被用户屏蔽了
    fail_notfound = 0   # 用户不存在/没聊过
    fail_other = 0      # 其他错误
    BATCH_SIZE = 20
    BATCH_INTERVAL = 1

    for idx, uid in enumerate(user_ids):
        try:
            await bot.send_message(
                uid, f"📢 <b>全站公告</b>\n\n{html.escape(content)}",
                parse_mode="HTML",
            )
            ok_cnt += 1
        except Exception as e:
            err_str = str(e)
            if "bot was blocked" in err_str or "Forbidden" in err_str:
                fail_blocked += 1
            elif "chat not found" in err_str or "not found" in err_str:
                fail_notfound += 1
            else:
                fail_other += 1

        # 每发完一批更新一次进度
        if (idx + 1) % BATCH_SIZE == 0:
            pct = int((idx + 1) / total * 100)
            bar_filled = int(pct / 5)
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            try:
                await progress_msg.edit_text(
                    f"📢 <b>正在广播…</b>\n\n"
                    f"总数：{total} 人\n"
                    f"已发送：{idx + 1} / {total}\n"
                    f"✅ 成功：{ok_cnt}\n"
                    f"🚫 被屏蔽：{fail_blocked}\n"
                    f"❓ 未找到：{fail_notfound}\n"
                    f"{bar} {pct}%"
                )
            except Exception:
                pass
            await asyncio.sleep(BATCH_INTERVAL)

    # 最终结果
    pct = 100
    bar = "█" * 20
    await progress_msg.edit_text(
        f"📢 <b>广播完成</b>\n\n"
        f"总数：{total} 人\n"
        f"✅ 成功送达：{ok_cnt}\n"
        f"🚫 被用户屏蔽：{fail_blocked}\n"
        f"❓ 用户未找到（删了聊天/没聊过）：{fail_notfound}\n"
        f"⚠️ 其他错误：{fail_other}\n"
        f"{bar} 100%\n\n"
        f"💡 说明：被屏蔽和未找到的用户收不到消息，"
        f"这是 Telegram 的限制——只有主动和 bot 聊过的用户才能被主动发消息。",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


# ---------------- 删除卡密 ----------------


@router.callback_query(F.data == "admin:delcard")
async def admin_delcard_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "🗑️ <b>删除卡密</b>\n\n请输入要删除的卡密：",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.delete_card)


@router.message(AdminStates.delete_card, F.text)
async def admin_delcard_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    with SessionLocal() as session:
        ok, text = delete_card(session, message.text)
    await message.answer(text)
    await cleanup_state(state)


# ---------------- 编辑欢迎语 ----------------


@router.callback_query(F.data == "admin:edit_welcome")
async def admin_edit_welcome_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        current = get_config(session, "welcome_text", "")
        current_entities_raw = get_config(session, "welcome_entities", "")

    # 解析 entities
    import json
    from aiogram.types import MessageEntity
    entities = None
    if current_entities_raw:
        try:
            data = json.loads(current_entities_raw)
            entities = [
                MessageEntity(
                    type=item["type"],
                    offset=item["offset"],
                    length=item["length"],
                    url=item.get("url"),
                    language=item.get("language"),
                    custom_emoji_id=item.get("custom_emoji_id"),
                )
                for item in data
            ]
        except Exception:
            entities = None

    # 先提示，再发预览
    await cq.message.edit_text(
        "✏️ <b>编辑欢迎语</b>\n\n"
        "<b>当前欢迎语预览：</b>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    if current:
        await cq.message.answer(
            current,
            entities=entities,
        )
    else:
        await cq.message.answer("（当前使用默认欢迎语）")

    await cq.message.answer(
        "────────────\n"
        "请直接发新的欢迎语（带会员表情、加粗、链接等都会自动保留）：\n"
        "发「重置」可恢复默认欢迎语。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.edit_welcome)


@router.message(AdminStates.edit_welcome, F.text)
async def admin_edit_welcome_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    # 用原始文本 + entities，这样能完整保留会员表情、加粗、链接等所有格式
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("内容不能为空，请重新发送。")
        return

    # 重置默认
    if new_text == "重置":
        with SessionLocal() as session:
            set_config(session, "welcome_text", "")
            set_config(session, "welcome_entities", "")
        await message.answer("✅ 已恢复默认欢迎语。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return

    # 序列化 entities 为 JSON
    import json
    entities_list = []
    for e in (message.entities or []):
        entities_list.append({
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "url": e.url,
            "user": e.user.id if e.user else None,
            "language": e.language,
            "custom_emoji_id": e.custom_emoji_id,
        })

    with SessionLocal() as session:
        set_config(session, "welcome_text", new_text)
        set_config(session, "welcome_entities", json.dumps(entities_list, ensure_ascii=False))

    # 预览效果（直接用原消息转发）
    await message.answer(
        "✅ 欢迎语已保存！效果预览：\n\n────────────"
    )
    await message.answer(
        new_text,
        entities=message.entities,
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


# ---------------- 套餐设置 ----------------


@router.callback_query(F.data == "admin:edit_plans")
async def admin_edit_plans_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        plans = get_plans(session)

    plan_lines = "\n".join([f"  • {p['days']}天 → {p['price']} USDT" for p in plans])
    await cq.message.edit_text(
        "💎 <b>套餐设置</b>\n\n"
        f"当前套餐：\n{plan_lines}\n\n"
        "────────────\n"
        "请发送新的套餐列表，格式：<code>天数:价格,天数:价格</code>\n"
        "例如：<code>7:1,30:3,90:8,365:25</code>\n\n"
        "发「重置」恢复默认套餐。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.edit_plans)


@router.message(AdminStates.edit_plans, F.text)
async def admin_edit_plans_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()

    if raw == "重置":
        with SessionLocal() as session:
            set_config(session, "plans", "")
        await message.answer("✅ 已恢复默认套餐。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return

    # 解析格式：7:5,30:15,90:40
    try:
        plans = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            days_s, price_s = item.split(":")
            days = int(days_s.strip())
            price = float(price_s.strip())
            if days < 1 or price < 0.01:
                raise ValueError("参数不合法")
            plans.append({"days": days, "price": price})
        if not plans:
            raise ValueError("至少一个套餐")
    except Exception:
        await message.answer("格式错误，请按「天数:价格,天数:价格」发送，例如：<code>7:5,30:15,90:40</code>")
        return

    with SessionLocal() as session:
        set_plans(session, plans)

    plan_lines = "\n".join([f"  • {p['days']}天 → ¥{p['price']}" for p in plans])
    await message.answer(
        f"✅ 套餐已保存！当前套餐：\n{plan_lines}",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


# ---------------- 洗数据定价设置 ----------------

@router.callback_query(F.data == "admin:edit_wash_prices")
async def admin_edit_wash_prices_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    from database import get_wash_price_floor, get_wash_prices
    with SessionLocal() as session:
        prices = get_wash_prices(session)
        floor = get_wash_price_floor(session)

    price_lines = "\n".join(
        [f"  • {p['min_rows']:,} 行以上 → ¥{p['price_per_row']:.4f} 元/行" for p in prices]
    )

    await cq.message.edit_text(
        "💲 <b>洗数据阶梯定价</b>\n\n"
        f"当前定价：\n{price_lines}\n\n"
        f"价格下限：<b>¥{floor:.4f} 元/行</b>（档位单价不得低于该值）\n\n"
        "────────────\n"
        "发送新的定价，格式：<code>行数:单价,行数:单价</code>\n"
        "例如：<code>0:0.0013,100000:0.0010</code>\n\n"
        "发「重置」恢复默认。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.edit_wash_prices)


@router.message(AdminStates.edit_wash_prices, F.text)
async def admin_edit_wash_prices_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()

    if raw == "重置":
        from database import set_wash_prices, DEFAULT_WASH_PRICES
        with SessionLocal() as session:
            set_wash_prices(session, DEFAULT_WASH_PRICES)
        await message.answer("✅ 已恢复默认定价。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return

    try:
        prices = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            rows_s, price_s = item.split(":")
            min_rows = int(rows_s.strip())
            price_per_row = float(price_s.strip())
            if min_rows < 0 or price_per_row <= 0:
                raise ValueError("参数不合法")
            prices.append({"min_rows": min_rows, "price_per_row": price_per_row})
        if not prices:
            raise ValueError("至少一个档位")
    except Exception:
        await message.answer("格式错误，请按「行数:单价,行数:单价」发送，例如：<code>0:0.0018,100000:0.0014</code>")
        return

    # 检查是否低于价格下限
    from database import get_wash_price_floor
    with SessionLocal() as session:
        floor = get_wash_price_floor(session)
    low = [p for p in prices if p["price_per_row"] < floor]
    if low:
        await message.answer(
            f"⚠️ 有档位单价低于价格下限 <b>¥{floor:.4f} 元/行</b>：\n"
            + "\n".join(
                f"  • {p['min_rows']:,} 行以上 → ¥{p['price_per_row']:.4f} 元/行"
                for p in low
            )
            + "\n\n请调整后重发，或先在「洗数据价格下限」中修改下限。",
            parse_mode="HTML",
        )
        return

    from database import set_wash_prices
    with SessionLocal() as session:
        set_wash_prices(session, prices)

    price_lines = "\n".join(
        [f"  • {p['min_rows']:,} 行以上 → ¥{p['price_per_row']:.4f} 元/行" for p in prices]
    )
    await message.answer(
        f"✅ 定价已保存！当前定价：\n{price_lines}",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


# ---------------- 洗数据价格下限 ----------------

@router.callback_query(F.data == "admin:set_price_floor")
async def admin_set_price_floor_start(cq: CallbackQuery, state: FSMContext):
    if not is_super_admin(cq.from_user.id):
        await cq.answer("⛔ 仅总管理员可用", show_alert=True)
        return
    await cq.answer()

    from database import get_wash_price_floor, DEFAULT_WASH_PRICE_FLOOR
    with SessionLocal() as session:
        floor = get_wash_price_floor(session)

    await cq.message.edit_text(
        "💲 <b>洗数据价格下限</b>\n\n"
        f"当前价格下限：<b>¥{floor:.4f} 元/行</b>\n\n"
        "────────────\n"
        "发送新的价格下限（元/行），例如：<code>0.0010</code>\n"
        "设置后，所有洗数据档位单价不得低于该值（已有更低档位会自动按下限计费）。\n\n"
        f"发「重置」恢复默认 ¥{DEFAULT_WASH_PRICE_FLOOR}。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.set_price_floor)


@router.message(AdminStates.set_price_floor, F.text)
async def admin_set_price_floor_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ 仅总管理员可用")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()
    if raw == "重置":
        from database import set_wash_price_floor, DEFAULT_WASH_PRICE_FLOOR
        with SessionLocal() as session:
            set_wash_price_floor(session, DEFAULT_WASH_PRICE_FLOOR)
        await message.answer(
            f"✅ 已恢复默认价格下限 ¥{DEFAULT_WASH_PRICE_FLOOR:.4f} 元/行。",
            reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        )
        await cleanup_state(state)
        return

    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except Exception:
        await message.answer("格式错误，请输入大于 0 的数字，例如：0.0010")
        return

    from database import set_wash_price_floor
    with SessionLocal() as session:
        set_wash_price_floor(session, value)

    await message.answer(
        f"✅ 洗数据价格下限已设为 <b>¥{value:.4f} 元/行</b>。\n\n"
        "之后设置的洗数据档位单价不得低于该值。",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


# ---------------- 累计洗数据配额 ----------------

@router.callback_query(F.data == "admin:set_wash_quota")
async def admin_set_wash_quota_start(cq: CallbackQuery, state: FSMContext):
    if not is_super_admin(cq.from_user.id):
        await cq.answer("⛔ 仅总管理员可用", show_alert=True)
        return
    await cq.answer()

    from database import get_wash_quota, get_washed_total
    with SessionLocal() as session:
        quota = get_wash_quota(session)
        washed = get_washed_total(session)

    quota_text = f"{quota:,} 行" if quota > 0 else "不限制（0）"
    remain = (quota - washed) if quota > 0 else None
    remain_text = f"{remain:,} 行" if remain is not None else "不限"

    await cq.message.edit_text(
        "📊 <b>累计洗数据配额</b>\n\n"
        f"当前配额：<b>{quota_text}</b>\n"
        f"累计已洗：<b>{washed:,} 行</b>\n"
        f"剩余可洗：<b>{remain_text}</b>\n\n"
        "────────────\n"
        "发送新的累计可洗数据总量（行数），例如：<code>1000000</code>\n"
        "达到上限后将不再接受新的洗数据任务（<code>0</code> 表示不限制）。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.set_wash_quota)


@router.message(AdminStates.set_wash_quota, F.text)
async def admin_set_wash_quota_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ 仅总管理员可用")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()
    if raw == "重置":
        from database import set_wash_quota
        with SessionLocal() as session:
            set_wash_quota(session, 0)
        await message.answer(
            "✅ 已恢复不限制（0）。",
            reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        )
        await cleanup_state(state)
        return

    if not raw.isdigit():
        await message.answer("格式错误，请输入整数行数，例如：1000000（0 表示不限制）")
        return

    value = int(raw)
    from database import set_wash_quota
    with SessionLocal() as session:
        set_wash_quota(session, value)

    if value == 0:
        text = "✅ 累计洗数据配额已设为 <b>不限制</b>。"
    else:
        text = f"✅ 累计洗数据配额已设为 <b>{value:,} 行</b>。"
    await message.answer(
        text,
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


# ---------------- 洗数据用量统计 ----------------

@router.callback_query(F.data == "admin:wash_usage")
async def admin_wash_usage(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    from database import get_washed_breakdown, get_wash_quota
    with SessionLocal() as session:
        charged, pending = get_washed_breakdown(session)
        quota = get_wash_quota(session)
    total = charged + pending

    quota_text = f"{quota:,} 行" if quota > 0 else "不限制"
    remain = (quota - total) if quota > 0 else None
    remain_text = f"{remain:,} 行" if remain is not None else "不限"

    await cq.message.edit_text(
        "📊 <b>洗数据用量统计</b>\n\n"
        f"🧼 累计已洗数据：<b>{total:,} 行</b>\n"
        f"　└ 已完成（已扣费）：{charged:,} 行\n"
        f"　└ 处理中（待扣费）：{pending:,} 行\n\n"
        f"🎯 累计配额：{quota_text}\n"
        f"　└ 剩余可洗：<b>{remain_text}</b>",
        reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
        parse_mode="HTML",
    )


# ---------------- 管理员管理（仅总管理员） ----------------

def _admin_manage_kb():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="➕ 添加管理员", callback_data="admin:add_admin")
    b.button(text="➖ 删除管理员", callback_data="admin:remove_admin")
    b.button(text="🔙 返回管理面板", callback_data="menu:admin")
    b.adjust(2)
    return b.as_markup()


@router.callback_query(F.data == "admin:manage_admins")
async def admin_manage_admins(cq: CallbackQuery):
    if not is_super_admin(cq.from_user.id):
        await cq.answer("⛔ 仅总管理员可用", show_alert=True)
        return
    await cq.answer()

    from database import SessionLocal, get_admin_ids
    with SessionLocal() as session:
        db_admins = get_admin_ids(session)

    super_admins = settings.SUPER_ADMIN_IDS
    env_admins = settings.ADMIN_IDS

    text = "👥 <b>管理员管理</b>\n\n"
    text += "👑 总管理员（环境变量）：\n" + (
        "\n".join(f"  <code>{i}</code>" for i in super_admins) or "  （未配置）"
    ) + "\n\n"
    text += "🔧 管理员（环境变量 ADMIN_IDS）：\n" + (
        "\n".join(f"  <code>{i}</code>" for i in env_admins) or "  （未配置）"
    ) + "\n\n"
    text += "🗂️ 管理员（数据库动态添加）：\n" + (
        "\n".join(f"  <code>{i}</code>" for i in db_admins) or "  （无）"
    )

    await cq.message.edit_text(text, reply_markup=_admin_manage_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin:add_admin")
async def admin_add_admin_start(cq: CallbackQuery, state: FSMContext):
    if not is_super_admin(cq.from_user.id):
        await cq.answer("⛔ 仅总管理员可用", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "➕ <b>添加管理员</b>\n\n"
        "发送要添加的管理员 Telegram 数字 ID（@userinfobot 可查询）：\n"
        "例如：<code>123456789</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.add_admin)


@router.message(AdminStates.add_admin, F.text)
async def admin_add_admin_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ 仅总管理员可用")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("格式错误，请输入数字 ID。")
        return
    tg_id = int(raw)

    from database import SessionLocal, add_admin_id
    with SessionLocal() as session:
        ok = add_admin_id(session, tg_id)

    if ok:
        text = f"✅ 已将 <code>{tg_id}</code> 设为管理员。"
    else:
        text = f"ℹ️ <code>{tg_id}</code> 已在管理员列表中。"
    await message.answer(text, reply_markup=_admin_manage_kb(), parse_mode="HTML")
    await cleanup_state(state)


@router.callback_query(F.data == "admin:remove_admin")
async def admin_remove_admin_start(cq: CallbackQuery, state: FSMContext):
    if not is_super_admin(cq.from_user.id):
        await cq.answer("⛔ 仅总管理员可用", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "➖ <b>删除管理员</b>\n\n"
        "发送要删除的管理员 Telegram 数字 ID：\n"
        "例如：<code>123456789</code>\n\n"
        "（环境变量里配置的管理员/总管理员无法通过这里删除）",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.remove_admin)


@router.message(AdminStates.remove_admin, F.text)
async def admin_remove_admin_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ 仅总管理员可用")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("格式错误，请输入数字 ID。")
        return
    tg_id = int(raw)

    if tg_id in settings.ADMIN_IDS or tg_id in settings.SUPER_ADMIN_IDS:
        await message.answer(
            f"⚠️ <code>{tg_id}</code> 是环境变量配置的管理员/总管理员，无法在这里删除。",
            reply_markup=_admin_manage_kb(),
            parse_mode="HTML",
        )
        await cleanup_state(state)
        return

    from database import SessionLocal, remove_admin_id
    with SessionLocal() as session:
        ok = remove_admin_id(session, tg_id)

    if ok:
        text = f"✅ 已删除管理员 <code>{tg_id}</code>。"
    else:
        text = f"ℹ️ <code>{tg_id}</code> 不在管理员列表中。"
    await message.answer(text, reply_markup=_admin_manage_kb(), parse_mode="HTML")
    await cleanup_state(state)


# ---------------- 绑定洗数据群 ----------------

@router.callback_query(F.data == "admin:bind_wash")
async def admin_bind_wash_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    with SessionLocal() as session:
        current = get_config(session, "wash_chat_id", "")

    current_name = f"当前绑定：{current}" if current else "未绑定"

    await cq.message.edit_text(
        f"🧼 <b>绑定洗数据群</b>\n\n"
        f"{current_name}\n\n"
        "────────────\n"
        "使用方法：\n"
        "1. 把机器人拉进洗数据群\n"
        "2. 在洗数据群里发命令：<code>/bind_wash</code>\n"
        "3. 我会自动读取群ID并绑定\n\n"
        "把机器人拉进洗数据群后，在群里发 /bind_wash 命令。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.bind_wash_chat)


@router.message(Command("bind_wash"))
async def admin_bind_wash_got(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    # 必须在群里发这个命令才有用
    if message.chat.type == "private":
        await message.answer("⚠️ 请在洗数据群里发这个命令。")
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title or "未知群"

    with SessionLocal() as session:
        set_config(session, "wash_chat_id", chat_id)

    await message.answer(
        f"✅ 已绑定洗数据群！\n\n"
        f"群名称：{chat_title}\n"
        f"群ID：{chat_id}\n\n"
        "用户上传的洗数据文件会自动转发到这个群。",
    )
    # 私聊里发提示
    try:
        from aiogram import Bot
        bot = message.bot
        await bot.send_message(
            chat_id=message.from_user.id,
            text=f"✅ 洗数据群已绑定：{chat_title}（{chat_id}）",
            reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        )
    except Exception:
        pass
    await cleanup_state(state)


# ---------------- 隐藏掉落：记录后台 / 通知群绑定 / 通知模板 ----------------

@router.callback_query(F.data == "admin:drop_records")
async def admin_drop_records(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    with SessionLocal() as session:
        stats = get_drop_stats(session)
        records = get_drop_records(session, 30)
        notify_chat = get_drop_notify_chat(session)

    lines = [
        "🎁 <b>掉落记录</b>\n\n"
        f"累计掉落：{stats['total_cnt']} 次 / {stats['total_days']} 天\n"
        f"├ 普通功能：{stats['other_cnt']} 次 / {stats['other_days']} 天\n"
        f"├ 洗数据：{stats['wash_cnt']} 次 / {stats['wash_days']} 天\n"
        f"└ 今日：{stats['today_cnt']} 次 / {stats['today_days']} 天\n\n"
        f"📢 通知群：{'已绑定 ' + notify_chat if notify_chat else '未绑定'}\n"
        "────────────\n"
        "<b>最近掉落：</b>"
    ]
    if not records:
        lines.append("（暂无掉落记录）")
    for r in records:
        who = f"@{r.username}" if r.username else str(r.user_id)
        tag = "🧼" if r.is_wash else "📄"
        lines.append(
            f"{beijing_str(r.created_at)} {tag} {who} · {r.feature} · +{r.days}天"
        )
    await cq.message.edit_text(
        "\n".join(lines),
        reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:bind_drop_chat")
async def admin_bind_drop_chat_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    with SessionLocal() as session:
        current = get_drop_notify_chat(session)
    current_name = f"当前绑定：{current}" if current else "未绑定"

    await cq.message.edit_text(
        f"🎁 <b>绑定抽奖通知群</b>\n\n"
        f"{current_name}\n\n"
        "────────────\n"
        "使用方法：\n"
        "1. 把机器人拉进抽奖通知群\n"
        "2. 在群里发命令：<code>/bind_drop</code>\n"
        "3. 我会自动读取群ID并绑定\n\n"
        "之后有人抽中奖品，通知会自动发到这个群。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.bind_drop_chat)


@router.message(Command("bind_drop"))
async def admin_bind_drop_got(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    # 必须在群里发这个命令才有用
    if message.chat.type == "private":
        await message.answer("⚠️ 请在抽奖通知群里发这个命令。")
        return

    chat_id = str(message.chat.id)
    chat_title = message.chat.title or "未知群"

    with SessionLocal() as session:
        set_drop_notify_chat(session, chat_id)

    await message.answer(
        f"✅ 已绑定抽奖通知群！\n\n"
        f"群名称：{chat_title}\n"
        f"群ID：{chat_id}\n\n"
        "之后有人抽中奖品，通知会自动发到这个群。",
    )
    # 私聊里发提示
    try:
        await message.bot.send_message(
            chat_id=message.from_user.id,
            text=f"✅ 抽奖通知群已绑定：{chat_title}（{chat_id}）",
            reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        )
    except Exception:
        pass
    await cleanup_state(state)


@router.callback_query(F.data == "admin:edit_drop_template")
async def admin_edit_drop_template_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    with SessionLocal() as session:
        current = get_drop_notify_template(session)

    from utils.drops import drop_template_hint
    await cq.message.edit_text(
        "🎁 <b>掉落通知模板</b>\n\n"
        "发送新的模板内容即可（支持换行）。\n\n"
        f"<b>当前模板：</b>\n<code>{html.escape(current)}</code>\n\n"
        f"{drop_template_hint()}",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.edit_drop_template)


@router.message(AdminStates.edit_drop_template, F.text)
async def admin_edit_drop_template_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    template = (message.text or "").strip()
    if not template:
        await message.answer("模板不能为空，请重新发送。")
        return

    with SessionLocal() as session:
        set_drop_notify_template(session, template)

    preview = template.format(
        user_id="123456789",
        username="user123",
        feature="文件切割",
        days=5,
        expires="2026-12-31 23:59",
    )
    await message.answer(
        f"✅ 掉落通知模板已更新！\n\n"
        f"<b>预览：</b>\n{preview}",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        parse_mode="HTML",
    )
    await cleanup_state(state)

# 所有可设置图标的按钮列表
BTN_ICONS_LIST = [
    ("文件切割", "menu:split"),
    ("解压Zip提取TXT", "menu:zip"),
    ("批量重命名", "menu:rename"),
    ("号码转VCF", "menu:vcf"),
    ("号码去重", "menu:dedup"),
    ("提取手机号", "menu:extract"),
    ("加区号", "menu:addcode"),
    ("号码分类", "menu:classify"),
    ("空号检测", "menu:phonecheck"),
    ("合并文本", "menu:merge"),
    ("催收分包", "menu:sort"),
    ("Excel转txt", "menu:xlsx"),
    ("洗数据", "menu:wash"),
    ("抽奖中心", "menu:lottery"),
    ("会员中心", "menu:membership"),
    ("购买会员", "buy:plans"),
    ("管理面板", "menu:admin"),
    ("绑定抽奖通知群", "admin:bind_drop_chat"),
]


@router.callback_query(F.data == "admin:btn_icons")
async def admin_btn_icons_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    # 一次读出所有按钮的图标状态
    with SessionLocal() as session:
        icon_map = {}
        for text, key in BTN_ICONS_LIST:
            icon_map[key] = get_config(session, f"btn_icon:{key}", "")

    # 列出所有按钮，每个按钮一个回调
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    for text, key in BTN_ICONS_LIST:
        has_icon = icon_map.get(key, "")
        mark = "✅" if has_icon else "⬜"
        try:
            b.button(text=f"{mark} {text}", callback_data=f"btn_icon:set:{key}")
        except Exception:
            b.button(text=f"{mark} {text}", callback_data=f"btn_icon:set:{key}")
    b.button(text="返回管理面板", callback_data="menu:admin")
    b.adjust(1)

    await cq.message.edit_text(
        "🎨 <b>按钮图标设置</b>\n\n"
        "选择要设置会员表情图标的按钮：\n"
        "✅ = 已设置图标\n"
        "⬜ = 未设置图标\n\n"
        "设置后，按钮文字前面会显示你的会员表情。",
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.btn_icon_pick)


@router.callback_query(AdminStates.btn_icon_pick, F.data.startswith("btn_icon:set:"))
async def admin_btn_icon_pick(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    btn_key = cq.data.split(":", 2)[2]
    btn_name = next((t for t, k in BTN_ICONS_LIST if k == btn_key), btn_key)

    with SessionLocal() as session:
        current_icon = get_config(session, f"btn_icon:{btn_key}", "")
        current_style = get_config(session, f"btn_style:{btn_key}", "")

    style_names = {"primary": "蓝色", "success": "绿色", "danger": "红色", "": "默认灰色"}
    current_style_name = style_names.get(current_style, current_style or "默认灰色")

    await state.update_data(btn_icon_key=btn_key)
    await cq.message.edit_text(
        f"🎨 <b>设置按钮</b>\n\n"
        f"当前按钮：<b>{btn_name}</b>\n"
        f"当前图标：{'已设置' if current_icon else '未设置'}\n"
        f"当前颜色：{current_style_name}\n\n"
        "────────────\n"
        "<b>设置图标：</b>直接发一个会员表情，我会自动提取 ID 保存。\n\n"
        "<b>设置颜色：</b>点下面的颜色按钮切换。\n\n"
        "发「清除图标」可以去掉图标。",
        parse_mode="HTML",
    )

    # 颜色选择按钮
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="🔵 蓝色", callback_data=f"btn_style:set:{btn_key}:primary")
    b.button(text="🟢 绿色", callback_data=f"btn_style:set:{btn_key}:success")
    b.button(text="🔴 红色", callback_data=f"btn_style:set:{btn_key}:danger")
    b.button(text="⚪ 默认灰色", callback_data=f"btn_style:set:{btn_key}:")
    b.button(text="🔙 返回按钮列表", callback_data="admin:btn_icons")
    b.adjust(2)
    await cq.message.answer("选择颜色：", reply_markup=b.as_markup())
    await state.set_state(AdminStates.btn_icon_input)


@router.callback_query(AdminStates.btn_icon_input, F.data.startswith("btn_style:set:"))
async def admin_btn_style_set(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    # 格式：btn_style:set:{btn_key}:{style}，注意 btn_key 本身可能包含冒号
    # 所以用 rsplit 从右边分割一次
    data = cq.data[len("btn_style:set:"):]
    if ":" in data:
        btn_key, style = data.rsplit(":", 1)
    else:
        btn_key, style = data, ""

    with SessionLocal() as session:
        set_config(session, f"btn_style:{btn_key}", style)

    # 同步更新内存缓存
    from utils.keyboards import update_btn_style_cache
    update_btn_style_cache(btn_key, style if style else None)

    style_names = {"primary": "蓝色", "success": "绿色", "danger": "红色", "": "默认灰色"}
    await cq.answer(f"✅ 颜色已设为：{style_names.get(style, '默认')}", show_alert=False)


@router.message(AdminStates.btn_icon_input, F.text)
async def admin_btn_icon_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    data = await state.get_data()
    btn_key = data.get("btn_icon_key")
    if not btn_key:
        await message.answer("出错了，请重新操作。")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()

    # 清除图标
    if raw == "清除图标":
        with SessionLocal() as session:
            set_config(session, f"btn_icon:{btn_key}", "")
        # 同步更新内存缓存
        from utils.keyboards import update_btn_icon_cache
        update_btn_icon_cache(btn_key, None)
        await message.answer("✅ 已清除该按钮的图标。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return

    # 从消息 entities 里找 custom_emoji_id
    custom_emoji_id = None
    for e in (message.entities or []):
        if e.type == "custom_emoji" and e.custom_emoji_id:
            custom_emoji_id = e.custom_emoji_id
            break

    if not custom_emoji_id:
        await message.answer(
            "⚠️ 没有在你发的消息里找到会员表情。\n\n"
            "请直接发一个会员表情（Premium Emoji）给我，不要加其他文字。"
        )
        return

    with SessionLocal() as session:
        set_config(session, f"btn_icon:{btn_key}", custom_emoji_id)

    # 同步更新内存缓存
    from utils.keyboards import update_btn_icon_cache
    update_btn_icon_cache(btn_key, custom_emoji_id)

    await message.answer(
        f"✅ 已保存！按钮图标已设置为你发的那个会员表情。\n\n"
        "返回主菜单看看效果。",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


def delete_or_cancel_kb() -> InlineKeyboardMarkup:
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="🗑️ 删除小号", callback_data="admin:alt_delete")
    b.button(text="❌ 取消", callback_data="admin:home")
    b.adjust(2)
    return b.as_markup()


# ---------------- 洗数据账单 ----------------

@router.callback_query(F.data == "admin:alt_delete")
async def admin_alt_delete(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    from database import get_config, delete_config
    with SessionLocal() as session:
        # 删除所有小号相关的配置
        for key in ["alt_session", "alt_username", "alt_user_id", "alt_status"]:
            delete_config(session, key)

    await cq.message.edit_text(
        "🗑️ 小号已删除，所有登录信息已清除。",
        reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
    )


@router.callback_query(F.data == "admin:wash_orders")
async def admin_wash_orders(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    from database import WashOrder, User, beijing_str
    from sqlalchemy import select, desc, func

    with SessionLocal() as session:
        orders = session.execute(
            select(WashOrder).order_by(desc(WashOrder.created_at)).limit(20)
        ).scalars().all()

        # 统计总充值和总扣费
        total_recharge = session.execute(
            select(func.sum(WashOrder.cost)).where(WashOrder.type == "recharge")
        ).scalar() or 0
        total_deduct = session.execute(
            select(func.sum(WashOrder.cost)).where(WashOrder.type == "deduct")
        ).scalar() or 0

    if not orders:
        await cq.message.edit_text("暂无洗数据账单。", reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)))
        return

    # 查用户名
    user_ids = set(o.user_id for o in orders)
    with SessionLocal() as session:
        users = session.execute(select(User).where(User.id.in_(user_ids))).scalars().all()
        user_map = {u.id: (u.username or u.first_name or str(u.id)) for u in users}

    lines = [
        "📊 <b>最近20条洗数据账单</b>\n",
        f"💰 总充值：¥{total_recharge:.2f} | 总扣费：¥{total_deduct:.2f}\n",
        "────────────\n",
    ]
    for o in orders:
        type_name = "充值" if o.type == "recharge" else "扣费"
        sign = "+" if o.type == "recharge" else "-"
        uname = user_map.get(o.user_id, str(o.user_id))
        lines.append(
            f"[{type_name}] @{uname}({o.user_id}) | ¥{sign}{o.cost:.4f}\n"
            f"  文件：{o.filename} | {o.row_count:,}行\n"
            f"  时间：{beijing_str(o.created_at)}\n"
        )

    # 加下载按钮
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 下载全部账单", callback_data="wash:download_orders")
    kb.button(text="🔙 返回", callback_data="admin:home")
    kb.adjust(1)

    await cq.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "wash:download_orders")
async def wash_download_orders(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    from database import WashOrder, User, beijing_str
    from sqlalchemy import select, desc
    import tempfile
    import os
    import uuid

    with SessionLocal() as session:
        orders = session.execute(
            select(WashOrder).order_by(desc(WashOrder.created_at))
        ).scalars().all()

        user_ids = set(o.user_id for o in orders)
        users = session.execute(select(User).where(User.id.in_(user_ids))).scalars().all()
        user_map = {u.id: (u.username or u.first_name or str(u.id)) for u in users}

    if not orders:
        await cq.answer("暂无账单可下载。", show_alert=True)
        return

    lines = ["洗数据账单导出", f"共 {len(orders)} 条记录", "=" * 50, ""]
    for o in orders:
        type_name = "充值" if o.type == "recharge" else "扣费"
        sign = "+" if o.type == "recharge" else "-"
        uname = user_map.get(o.user_id, str(o.user_id))
        lines.append(f"[{type_name}] @{uname}({o.user_id})")
        lines.append(f"  金额: ¥{sign}{o.cost:.4f}")
        lines.append(f"  文件: {o.filename}")
        lines.append(f"  行数: {o.row_count:,}")
        lines.append(f"  时间: {beijing_str(o.created_at)}")
        lines.append("-" * 30)

    tmp = os.path.join(tempfile.gettempdir(), f"wash_orders_{uuid.uuid4().hex}.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    try:
        await cq.message.answer_document(
            FSInputFile(tmp, filename="洗数据账单.txt"),
            caption=f"📥 共导出 {len(orders)} 条账单",
        )
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------- 洗数据加/扣余额 ----------------

@router.callback_query(F.data == "admin:wash_balance")
async def admin_wash_balance_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    await cq.message.edit_text(
        "💰 <b>洗数据加/扣余额</b>\n\n"
        "请发送：<code>用户ID或用户名 金额(正数加/负数扣)</code>\n"
        "例如：\n"
        "<code>123456 10</code> → 给用户ID加10元\n"
        "<code>@zhangsan 10</code> → 给用户名加10元\n"
        "<code>123456 -5</code> → 给用户扣5元",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.wash_balance_input)


@router.message(AdminStates.wash_balance_input, F.text)
async def admin_wash_balance_input(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    raw = (message.text or "").strip()
    parts = raw.split()
    if len(parts) != 2:
        await message.answer("格式错误，请按「用户ID或用户名 金额」发送，例如：<code>123456 10</code>")
        return

    user_identifier = parts[0].lstrip("@")
    amount = float(parts[1]) if parts[1].replace("-", "").replace(".", "").isdigit() else 0
    if amount == 0:
        await message.answer("金额格式错误，请输入数字。")
        return

    from database import WashOrder, User, add_wash_balance, deduct_wash_balance, get_wash_balance
    from sqlalchemy import select

    with SessionLocal() as session:
        # 先按数字当用户ID查，不是就按用户名查（不区分大小写）
        if user_identifier.isdigit():
            user_id = int(user_identifier)
            user = session.get(User, user_id)
        else:
            user = session.execute(
                select(User).where(func.lower(User.username) == user_identifier.lower())
            ).scalar_one_or_none()
            user_id = user.id if user else None

        # 数字 ID 不存在时 user 为 None，这里必须先判空，否则下面会崩溃
        if user is None:
            await message.answer("找不到这个用户（用户需先使用过机器人）。")
            await cleanup_state(state)
            return

        user_name = user.username or user.first_name or str(user_id)

        if amount >= 0:
            add_wash_balance(session, user_id, amount)
            type_name = "手动充值"
        else:
            ok = deduct_wash_balance(session, user_id, -amount)
            if not ok:
                balance = get_wash_balance(session, user_id)
                await message.answer(
                    f"⚠️ 扣费失败：该用户余额不足（当前 ¥{balance:.4f}），未记录扣费。"
                )
                await cleanup_state(state)
                return
            type_name = "手动扣费"

        balance = get_wash_balance(session, user_id)

        # 记录账单
        order = WashOrder(
            user_id=user_id,
            filename=type_name,
            row_count=0,
            cost=amount,
            type="recharge" if amount >= 0 else "deduct",
        )
        session.add(order)
        session.commit()

    sign = "+" if amount >= 0 else ""
    await message.answer(
        f"✅ 已操作！\n\n"
        f"用户：@{user_name}（{user_id}）\n"
        f"操作金额：¥{sign}{amount:.4f}\n"
        f"当前余额：¥{balance:.4f} 元",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    # 加余额时通知用户本人（扣费不通知；用户屏蔽机器人时静默忽略）
    if amount >= 0:
        try:
            await bot.send_message(
                user_id,
                "💰 <b>余额到账通知</b>\n\n"
                f"管理员为你充值了 <b>¥{amount:.4f}</b> 洗数据余额！\n"
                f"当前洗数据余额：<b>¥{balance:.4f}</b> 元\n\n"
                "可用于「洗数据」功能，祝你使用愉快～",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await cleanup_state(state)


# ---------------- 小号登录 ----------------

@router.callback_query(F.data == "admin:alt_login")
async def admin_alt_login_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()

    from database import get_config
    with SessionLocal() as session:
        session_str = get_config(session, "alt_session", "")
        alt_status = get_config(session, "alt_status", "unknown")
        alt_username = get_config(session, "alt_username", "")
        alt_user_id = get_config(session, "alt_user_id", "")

    if not settings.TELETHON_API_ID or not settings.TELETHON_API_HASH:
        await cq.message.edit_text(
            "⚠️ 请先在环境变量配置：\n"
            "<code>TELETHON_API_ID</code>\n"
            "<code>TELETHON_API_HASH</code>",
            reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
            parse_mode="HTML",
        )
        return

    if session_str and alt_status == "online":
        status = "✅ 已登录（在线）"
    elif session_str:
        status = "⚠️ 已登录但离线"
    else:
        status = "❌ 未登录"

    info_text = ""
    if alt_username:
        info_text = f"\n用户名：@{alt_username}\nID：{alt_user_id}"

    await cq.message.edit_text(
        f"👤 <b>小号登录</b>\n\n"
        f"状态：{status}\n"
        f"{info_text}\n\n"
        "────────────\n"
        "发送你的小号手机号（带国家码）：\n"
        "例如：<code>+8613800138000</code>\n\n"
        "然后会发送验证码到小号，再发验证码登录。",
        reply_markup=delete_or_cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.alt_login_step1)


@router.message(AdminStates.alt_login_step1, F.text)
async def admin_alt_login_step1(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    phone = (message.text or "").strip()

    if not phone.startswith("+"):
        await message.answer("手机号格式不对，请带国家码，例如：+8613800138000")
        return

    # 发送验证码
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), settings.TELETHON_API_ID, settings.TELETHON_API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        await state.update_data(alt_client=client, alt_phone=phone, alt_sent=sent)
        await message.answer(
            "✅ 验证码已发送到你的小号 Telegram！\n\n"
            "请把验证码发给我。",
        )
        await state.set_state(AdminStates.alt_login_step2)
    except Exception as e:
        await message.answer(f"⚠️ 发送验证码失败：{e}", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)


@router.message(AdminStates.alt_login_step2, F.text)
async def admin_alt_login_step2(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    code = (message.text or "").strip()
    data = await state.get_data()
    client: TelegramClient = data.get("alt_client")
    phone = data.get("alt_phone")
    sent = data.get("alt_sent")

    if not client or not phone:
        await message.answer("⚠️ 会话过期，请重新开始。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        # 登录成功
        session_str = client.session.save()
        me = await client.get_me()

        from database import set_config
        with SessionLocal() as session:
            set_config(session, "alt_session", session_str)
            set_config(session, "alt_username", me.username or "")
            set_config(session, "alt_user_id", str(me.id))

        await message.answer(
            f"✅ 小号登录成功！\n\n"
            f"用户名：@{me.username or '无'}\n"
            f"名字：{me.first_name}\n\n"
            "洗数据中转已自动启动。",
            reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        )
        await client.disconnect()

        # 自动启动小号客户端（bot 由 aiogram 自动注入）
        from utils.telethon_client import start_userbot
        asyncio.create_task(start_userbot(bot))
    except Exception as e:
        # 遇到两步验证
        if "Two-step" in str(e) or "password" in str(e):
            await message.answer(
                "🔐 你的小号开了两步验证，请输入两步验证密码："
            )
            await state.update_data(alt_client=client, alt_phone=phone, alt_sent=sent)
            await state.set_state(AdminStates.alt_login_step3)
            return
        await message.answer(f"⚠️ 登录失败：{e}\n\n请重新开始。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        try:
            await client.disconnect()
        except Exception:
            pass
    await cleanup_state(state)


@router.message(AdminStates.alt_login_step3, F.text)
async def admin_alt_login_step3(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return

    password = (message.text or "").strip()
    data = await state.get_data()
    client: TelegramClient = data.get("alt_client")

    if not client:
        await message.answer("⚠️ 会话过期，请重新开始。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return

    try:
        await client.sign_in(password=password)
        session_str = client.session.save()
        me = await client.get_me()

        from database import set_config
        with SessionLocal() as session:
            set_config(session, "alt_session", session_str)
            set_config(session, "alt_username", me.username or "")
            set_config(session, "alt_user_id", str(me.id))

        await message.answer(
            f"✅ 小号登录成功！\n\n"
            f"用户名：@{me.username or '无'}\n"
            f"名字：{me.first_name}\n\n"
            "洗数据中转已自动启动。",
            reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        )
        await client.disconnect()

        # 自动启动小号客户端（bot 由 aiogram 自动注入）
        from utils.telethon_client import start_userbot
        asyncio.create_task(start_userbot(bot))
    except Exception as e:
        from telethon.errors import PasswordHashInvalidError
        if isinstance(e, PasswordHashInvalidError):
            await message.answer(f"⚠️ 密码错误：{e}\n\n请重新开始。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        else:
            await message.answer(f"⚠️ 登录处理失败：{e}\n\n请重新开始。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        try:
            await client.disconnect()
        except Exception:
            pass
    await cleanup_state(state)
