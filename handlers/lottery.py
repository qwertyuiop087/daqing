"""抽奖系统：普通抽奖（每天一次）、高级抽奖（消耗高级券）、兑换（券换奖品）。"""
import asyncio
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    DEFAULT_LOTTERY_NOTIFY_TEMPLATE,
    DEFAULT_LOTTERY_PREMIUM_PRIZES,
    DEFAULT_LOTTERY_PRIZES,
    DEFAULT_LOTTERY_REDEEM,
    SessionLocal,
    beijing_str,
    get_lottery_notify_template,
    get_lottery_premium_prizes,
    get_lottery_prizes,
    get_lottery_records,
    get_lottery_redeem,
    get_lottery_stats,
    get_or_create_user,
    get_today_lottery_record,
    set_lottery_notify_template,
    set_lottery_premium_prizes,
    set_lottery_prizes,
    set_lottery_redeem,
)
from states import AdminStates
from utils.helpers import cleanup_state, ensure_member
from utils.keyboards import admin_kb, cancel_kb, menu_kb
from utils.lottery import (
    draw_ordinary,
    draw_premium,
    notify_lottery,
    parse_prizes_input,
    parse_redeem_input,
    redeem_prize,
)
from utils.roles import is_admin, is_super_admin

router = Router(name="lottery")


def _tickets_of(user) -> int:
    return int(user.lottery_tickets or 0)


def _center_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎰 普通抽奖", callback_data="lottery:ordinary")
    b.button(text="🎁 高级抽奖", callback_data="lottery:premium")
    b.button(text="🛍️ 奖品兑换", callback_data="menu:lottery_redeem")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def _back_kb() -> InlineKeyboardMarkup:
    """结果页返回按钮：回抽奖中心或主菜单。"""
    b = InlineKeyboardBuilder()
    b.button(text="🎰 回抽奖中心", callback_data="menu:lottery")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


async def _spin(cq: CallbackQuery, title: str) -> None:
    """转盘动画：圆弧围绕转盘旋转，模拟轮盘转动。"""
    frames = ("◐🎡◑", "◓🎠◒", "◑🎢◐", "◒🎪◓", "◐🎡◑", "◓🎠◒")
    for f in frames:
        try:
            await cq.message.edit_text(f"{title}\n\n{f}\n转动中…")
        except Exception:
            pass
        await asyncio.sleep(0.3)


async def _finish(cq: CallbackQuery, title: str, result: dict, user, source: str) -> None:
    """展示结果并通知。"""
    if result["type"] == "none":
        text = f"{title}\n\n😢 谢谢参与，下次再来！"
    else:
        text = f"{title}\n\n🎉 恭喜获得：<b>{html.escape(result['label'])}</b>\n\n已自动到账。"
    await cq.message.edit_text(text, reply_markup=_back_kb())
    if result["type"] != "none":
        await notify_lottery(cq.bot, cq.from_user.id, cq.from_user.first_name or "", source, result["label"])


# ---------------- 抽奖中心 ----------------

@router.callback_query(F.data == "menu:lottery")
async def menu_lottery_center(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await cq.answer()
    with SessionLocal() as session:
        user = get_or_create_user(
            session, cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or ""
        )
        tickets = _tickets_of(user)
    await cq.message.edit_text(
        "🎰 <b>抽奖中心</b>\n\n"
        f"当前高级抽奖次数：<b>{tickets}</b> 次\n\n"
        "• <b>普通抽奖</b>：会员每天免费 1 次，可抽中高级券\n"
        "• <b>高级抽奖</b>：消耗 1 次高级券，奖品更大\n"
        "• <b>奖品兑换</b>：高级券直接换固定奖品\n\n"
        "选择要玩的：",
        reply_markup=_center_kb(),
    )


# ---------------- 普通抽奖 ----------------

@router.callback_query(F.data == "lottery:ordinary")
async def lottery_ordinary(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await cq.answer()
    with SessionLocal() as session:
        user = get_or_create_user(
            session, cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or ""
        )
        if get_today_lottery_record(session, user.id):
            await cq.message.edit_text(
                "🎰 <b>普通抽奖</b>\n\n你今天已经抽过啦，明天（北京时间 0 点）再来～\n\n"
                f"当前高级抽奖次数：<b>{_tickets_of(user)}</b> 次",
                reply_markup=_back_kb(),
            )
            return
        # 先判定并落库（原子），再播放动画，避免连点导致重复抽
        result = draw_ordinary(session, user)
    await _spin(cq, "🎰 <b>普通抽奖</b>")
    await _finish(cq, "🎰 <b>普通抽奖</b>", result, user, "ordinary")


# ---------------- 高级抽奖 ----------------

@router.callback_query(F.data == "lottery:premium")
async def lottery_premium(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await cq.answer()
    with SessionLocal() as session:
        user = get_or_create_user(
            session, cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or ""
        )
        if _tickets_of(user) < 1:
            await cq.message.edit_text(
                "🎁 <b>高级抽奖</b>\n\n"
                f"你当前的高级抽奖次数：<b>0</b> 次\n\n"
                "高级抽奖次数可通过「普通抽奖」抽中「高级券」获得。",
                reply_markup=_back_kb(),
            )
            return
        # 先判定并落库（原子），再播放动画
        result = draw_premium(session, user)
    await _spin(cq, "🎁 <b>高级抽奖</b>")
    await _finish(cq, "🎁 <b>高级抽奖</b>", result, user, "premium")


# ---------------- 兑换 ----------------

def _redeem_kb(items: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, it in enumerate(items):
        cost = int(it["cost"])
        if it["type"] == "days":
            label = f"{cost} 券 → {int(it['value'])} 天会员"
        else:
            label = f"{cost} 券 → ¥{float(it['value']):g} 洗数据余额"
        b.button(text=label, callback_data=f"lottery:redeem:{i}")
    b.button(text="🎁 去高级抽奖", callback_data="lottery:premium")
    b.button(text="🎰 回抽奖中心", callback_data="menu:lottery")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "menu:lottery_redeem")
async def menu_lottery_redeem(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await cq.answer()
    with SessionLocal() as session:
        user = get_or_create_user(
            session, cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or ""
        )
        items = get_lottery_redeem(session)
    lines = "\n".join(
        f"• {int(it['cost'])} 券 → {'%d 天会员' % int(it['value']) if it['type'] == 'days' else '¥%g 洗数据余额' % float(it['value'])}"
        for it in items
    )
    await cq.message.edit_text(
        "🛍️ <b>奖品兑换</b>\n\n"
        f"当前高级抽奖次数：<b>{_tickets_of(user)}</b> 次\n\n"
        f"兑换价目表：\n{lines}\n\n"
        "点下方按钮直接兑换（不靠运气）：",
        reply_markup=_redeem_kb(items),
    )


@router.callback_query(F.data.startswith("lottery:redeem:"))
async def lottery_redeem_do(cq: CallbackQuery):
    try:
        idx = int(cq.data.split(":")[2])
    except (ValueError, IndexError):
        await cq.answer("参数错误", show_alert=True)
        return
    with SessionLocal() as session:
        user = get_or_create_user(
            session, cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or ""
        )
        items = get_lottery_redeem(session)
        if idx < 0 or idx >= len(items):
            await cq.answer("该兑换项不存在", show_alert=True)
            return
        item = items[idx]
        if _tickets_of(user) < int(item["cost"]):
            await cq.answer(f"高级抽奖次数不足（需要 {int(item['cost'])} 次）", show_alert=True)
            return
        label = redeem_prize(session, user, item)
    await cq.answer()
    await cq.message.edit_text(
        f"🛍️ <b>兑换成功</b>\n\n"
        f"已用 <b>{int(item['cost'])}</b> 次高级抽奖兑换：<b>{html.escape(label)}</b>\n"
        f"剩余高级抽奖次数：<b>{_tickets_of(user)}</b> 次",
        reply_markup=_back_kb(),
    )
    await notify_lottery(cq.bot, cq.from_user.id, cq.from_user.first_name or "", "redeem", label)


# ---------------- 管理后台 ----------------

def _prize_display(prizes: list[dict]) -> str:
    lines = []
    for p in prizes:
        if p["type"] == "ticket":
            lines.append(f"  • 高级券 {int(p['value'])} 次 → {p['prob']}%")
        elif p["type"] == "days":
            lines.append(f"  • {int(p['value'])} 天会员 → {p['prob']}%")
        else:
            lines.append(f"  • ¥{float(p['value']):g} 洗数据余额 → {p['prob']}%")
    return "\n".join(lines)


@router.callback_query(F.data == "admin:lottery_pool")
async def admin_lottery_pool(cq: CallbackQuery):
    """抽奖总览：一页看完全部奖池与概率合计。"""
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        ordinary = get_lottery_prizes(session)
        premium = get_lottery_premium_prizes(session)
        redeem = get_lottery_redeem(session)

    def _total(prizes: list[dict]) -> float:
        return sum(float(p["prob"]) for p in prizes)

    ord_total = _total(ordinary)
    prem_total = _total(premium)
    lines = ["🎰 <b>抽奖总览</b>（全部奖池）\n"]
    lines.append("🎰 <b>普通抽奖</b> · 会员每天 1 次，可抽中高级券")
    lines.append(_prize_display(ordinary) or "  （未配置）")
    lines.append(f"  概率合计 {ord_total}%，剩余 {100 - ord_total}% 为「谢谢参与」\n")
    lines.append("🎁 <b>高级抽奖</b> · 消耗 1 张高级券")
    lines.append(_prize_display(premium) or "  （未配置）")
    lines.append(f"  概率合计 {prem_total}%，剩余 {100 - prem_total}% 为「谢谢参与」\n")
    lines.append("🛍️ <b>兑换价目表</b> · 高级券直接兑换")
    if redeem:
        for it in redeem:
            if it["type"] == "days":
                lines.append(f"  • {int(it['cost'])} 券 → {int(it['value'])} 天会员")
            else:
                lines.append(f"  • {int(it['cost'])} 券 → ¥{float(it['value']):g} 洗数据余额")
    else:
        lines.append("  （未配置）")
    await cq.message.edit_text(
        "\n".join(lines),
        reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:lottery_config")
async def admin_lottery_config_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        prizes = get_lottery_prizes(session)
    total = sum(float(p["prob"]) for p in prizes)
    await cq.message.edit_text(
        "🎰 <b>普通抽奖奖品设置</b>\n\n"
        f"当前奖品：\n{_prize_display(prizes)}\n"
        f"概率合计 {total}%，剩余 {100 - total}% 为「谢谢参与」。\n\n"
        "────────────\n"
        "发送新奖品列表，格式：<code>类型:数值:概率,类型:数值:概率</code>\n"
        "类型：<code>天</code>=会员天数，<code>余额</code>=洗数据余额，<code>高级券</code>=高级抽奖次数\n\n"
        "例：<code>天:1:40,天:3:30,余额:5:5,高级券:1:3</code>\n"
        "发「重置」恢复默认。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.lottery_config)


@router.message(AdminStates.lottery_config, F.text)
async def admin_lottery_config_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    raw = (message.text or "").strip()
    if raw == "重置":
        with SessionLocal() as session:
            set_lottery_prizes(session, DEFAULT_LOTTERY_PRIZES)
        await message.answer("✅ 已恢复默认普通抽奖奖品。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return
    prizes, err = parse_prizes_input(raw)
    if err:
        await message.answer(f"❌ {err}\n\n格式：<code>天:1:40,余额:5:5,高级券:1:3</code>")
        return
    with SessionLocal() as session:
        set_lottery_prizes(session, prizes)
    total = sum(float(p["prob"]) for p in prizes)
    await message.answer(
        f"✅ 普通抽奖奖品已保存！\n\n{_prize_display(prizes)}\n\n概率合计 {total}%，剩余 {100 - total}% 为「谢谢参与」。",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


@router.callback_query(F.data == "admin:lottery_premium_config")
async def admin_lottery_premium_config_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        prizes = get_lottery_premium_prizes(session)
    total = sum(float(p["prob"]) for p in prizes)
    await cq.message.edit_text(
        "🎁 <b>高级抽奖奖品设置</b>\n\n"
        f"当前奖品：\n{_prize_display(prizes)}\n"
        f"概率合计 {total}%，剩余 {100 - total}% 为「谢谢参与」。\n\n"
        "────────────\n"
        "发送新奖品列表，格式：<code>类型:数值:概率,类型:数值:概率</code>\n"
        "类型：<code>天</code>=会员天数，<code>余额</code>=洗数据余额\n\n"
        "例：<code>天:30:45,天:90:30,天:365:15,余额:50:10</code>\n"
        "发「重置」恢复默认。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.lottery_premium_config)


@router.message(AdminStates.lottery_premium_config, F.text)
async def admin_lottery_premium_config_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    raw = (message.text or "").strip()
    if raw == "重置":
        with SessionLocal() as session:
            set_lottery_premium_prizes(session, DEFAULT_LOTTERY_PREMIUM_PRIZES)
        await message.answer("✅ 已恢复默认高级抽奖奖品。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return
    prizes, err = parse_prizes_input(raw)
    if err:
        await message.answer(f"❌ {err}\n\n格式：<code>天:30:45,余额:50:10</code>")
        return
    with SessionLocal() as session:
        set_lottery_premium_prizes(session, prizes)
    total = sum(float(p["prob"]) for p in prizes)
    await message.answer(
        f"✅ 高级抽奖奖品已保存！\n\n{_prize_display(prizes)}\n\n概率合计 {total}%，剩余 {100 - total}% 为「谢谢参与」。",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


@router.callback_query(F.data == "admin:lottery_redeem_config")
async def admin_lottery_redeem_config_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        items = get_lottery_redeem(session)
    lines = "\n".join(
        f"  • {int(it['cost'])} 券 → {'%d 天会员' % int(it['value']) if it['type'] == 'days' else '¥%g 洗数据余额' % float(it['value'])}"
        for it in items
    )
    await cq.message.edit_text(
        "🛍️ <b>兑换价目表设置</b>\n\n"
        f"当前价目表：\n{lines}\n\n"
        "────────────\n"
        "发送新价目表，格式：<code>券数:类型:数值,券数:类型:数值</code>\n"
        "类型：<code>天</code>=会员天数，<code>余额</code>=洗数据余额\n\n"
        "例：<code>1:天:10,2:余额:20</code>\n"
        "发「重置」恢复默认。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.lottery_redeem_config)


@router.message(AdminStates.lottery_redeem_config, F.text)
async def admin_lottery_redeem_config_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    raw = (message.text or "").strip()
    if raw == "重置":
        with SessionLocal() as session:
            set_lottery_redeem(session, DEFAULT_LOTTERY_REDEEM)
        await message.answer("✅ 已恢复默认兑换价目表。", reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)))
        await cleanup_state(state)
        return
    items, err = parse_redeem_input(raw)
    if err:
        await message.answer(f"❌ {err}\n\n格式：<code>1:天:10,2:余额:20</code>")
        return
    with SessionLocal() as session:
        set_lottery_redeem(session, items)
    lines = "\n".join(
        f"  • {int(it['cost'])} 券 → {'%d 天会员' % int(it['value']) if it['type'] == 'days' else '¥%g 洗数据余额' % float(it['value'])}"
        for it in items
    )
    await message.answer(
        f"✅ 兑换价目表已保存！\n\n{lines}",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
    )
    await cleanup_state(state)


@router.callback_query(F.data == "admin:lottery_records")
async def admin_lottery_records(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        stats = get_lottery_stats(session)
        records = get_lottery_records(session, 30)
    source_names = {"ordinary": "普通", "premium": "高级", "redeem": "兑换"}
    lines = [
        "🎰 <b>抽奖记录</b>\n\n"
        f"累计：{stats['total']} 次 / 今日：{stats['today']} 次\n"
        "────────────\n"
        "<b>最近记录：</b>"
    ]
    if not records:
        lines.append("（暂无记录）")
    for r in records:
        who = f"@{r.username}" if r.username else str(r.user_id)
        src = source_names.get(r.source, r.source)
        lines.append(f"{beijing_str(r.created_at)} [{src}] {who} · {html.escape(r.prize_label)}")
    await cq.message.edit_text(
        "\n".join(lines),
        reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id)),
        parse_mode="HTML",
    )


# ---------------- 抽奖通知模板 ----------------

@router.callback_query(F.data == "admin:lottery_template")
async def admin_lottery_template_start(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ 无权限", show_alert=True)
        return
    await cq.answer()
    with SessionLocal() as session:
        current = get_lottery_notify_template(session)
    await cq.message.edit_text(
        "📣 <b>抽奖通知模板</b>\n\n"
        f"当前模板：\n<code>{html.escape(current)}</code>\n\n"
        "────────────\n"
        "发送新模板即可（支持换行）。可用占位符：\n"
        "<code>{username}</code> 用户名字（Telegram 昵称，自动打码，如 张* / 王*二）\n"
        "<code>{user_id}</code> 用户 Telegram ID\n"
        "<code>{source}</code> 来源（普通抽奖/高级抽奖/兑换）\n"
        "<code>{prize}</code> 奖品（如 3 天会员）\n\n"
        "发「重置」恢复默认。",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.lottery_template)


@router.message(AdminStates.lottery_template, F.text)
async def admin_lottery_template_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ 无权限")
        await cleanup_state(state)
        return
    template = (message.text or "").strip()
    if not template:
        await message.answer("模板不能为空，请重新发送。")
        return
    is_reset = template == "重置"
    with SessionLocal() as session:
        if is_reset:
            set_lottery_notify_template(session, DEFAULT_LOTTERY_NOTIFY_TEMPLATE)
        else:
            set_lottery_notify_template(session, template)
    preview = (DEFAULT_LOTTERY_NOTIFY_TEMPLATE if is_reset else template).format(
        user_id="123456789",
        username="王*二",
        source="普通抽奖",
        prize="3 天会员",
    )
    await message.answer(
        f"✅ 抽奖通知模板已{'恢复默认' if is_reset else '更新'}！\n\n"
        f"<b>预览：</b>\n{html.escape(preview)}",
        reply_markup=admin_kb(is_super=is_super_admin(message.from_user.id)),
        parse_mode="HTML",
    )
    await cleanup_state(state)
