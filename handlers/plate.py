"""车牌分包：上传文件（不限格式）→ 自动识别车牌与手机号 → 按用户指定顺序输出 TXT。"""
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services.file_ops import (
    decode_bytes,
    extract_plates,
    extract_txt_from_zip,
    read_file,
    sanitize_filename,
    xlsx_to_lines,
)
from states import PlateStates
from utils.drops import maybe_drop
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.keyboards import cancel_kb, menu_kb
from utils.phone_db import extract_phones

router = Router(name="plate")


def _read_to_lines(path: str, filename: str) -> list[str]:
    """按扩展名把文件内容读成行列表，支持 txt/csv/xlsx/xls/zip。"""
    lower = (filename or "").lower()
    data = read_file(path)
    if lower.endswith((".xlsx", ".xls")):
        return xlsx_to_lines(data)
    if lower.endswith(".zip"):
        lines: list[str] = []
        for _name, text in extract_txt_from_zip(data):
            lines.extend(text.splitlines())
        return lines
    return decode_bytes(data).splitlines()


def _order_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 手机号 → 车牌", callback_data="plate:order:phone")
    b.button(text="🚗 车牌 → 手机号", callback_data="plate:order:plate")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "menu:plate")
async def menu_plate(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(PlateStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🚗 <b>车牌分包</b>\n\n"
        "上传文件，我会自动识别里面的<b>车牌号</b>和<b>手机号</b>，"
        "再按你指定的顺序整理成 TXT 发回。\n\n"
        "支持格式：<b>txt / csv / xlsx / xls / zip</b>\n"
        f"单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。\n\n"
        "请发送文件：",
        reply_markup=cancel_kb(),
    )


@router.message(PlateStates.waiting_file, F.document)
async def plate_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = sanitize_filename(doc.file_name or "input.txt")
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(f"⚠️ 文件过大，最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。")
        return

    data = await state.get_data()
    path = os.path.join(data["workdir"], name)
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=path)

    try:
        lines = _read_to_lines(path, name)
    except Exception as e:
        await message.answer(f"⚠️ 无法读取该文件：{str(e)[:100]}")
        await cleanup_state(state)
        return

    records = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        phones = extract_phones(ln)
        plates = extract_plates(ln)
        if phones or plates:
            records.append(
                {"phone": phones[0] if phones else "", "plate": plates[0] if plates else ""}
            )

    if not records:
        await message.answer("⚠️ 未识别到车牌或手机号，请检查文件内容。", reply_markup=cancel_kb())
        return

    both = sum(1 for r in records if r["phone"] and r["plate"])
    only_phone = sum(1 for r in records if r["phone"] and not r["plate"])
    only_plate = sum(1 for r in records if r["plate"] and not r["phone"])

    await state.update_data(records=records, src_name=name)
    await state.set_state(PlateStates.waiting_order)
    await message.answer(
        f"✅ 已识别 <b>{len(records)}</b> 条记录：\n"
        f"· 手机号+车牌配对：<b>{both}</b> 条\n"
        f"· 仅有手机号：<b>{only_phone}</b> 条\n"
        f"· 仅有车牌：<b>{only_plate}</b> 条\n\n"
        "请选择输出顺序（每行用空格分隔）：",
        reply_markup=_order_kb(),
    )


@router.callback_query(PlateStates.waiting_order, F.data.startswith("plate:order:"))
async def plate_order_pick(cq: CallbackQuery, state: FSMContext):
    phone_first = cq.data.endswith(":phone")
    await cq.answer()
    data = await state.get_data()
    records = data.get("records", [])
    if not records:
        await cq.message.answer("⚠️ 会话异常，请重新开始。")
        await cleanup_state(state)
        return

    out_lines = []
    for r in records:
        if phone_first:
            out_lines.append(f"{r['phone']} {r['plate']}")
        else:
            out_lines.append(f"{r['plate']} {r['phone']}")

    stem = os.path.splitext(data.get("src_name", "output"))[0]
    order_tag = "手机号_车牌" if phone_first else "车牌_手机号"
    out_name = f"{stem}_{order_tag}.txt"
    out_path = os.path.join(data["workdir"], out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))

    await cq.message.edit_text(
        f"✅ 完成！共 <b>{len(records)}</b> 条，顺序："
        f"<b>{'手机号 → 车牌' if phone_first else '车牌 → 手机号'}</b>（空格分隔）",
        reply_markup=None,
    )
    await cq.message.answer_document(FSInputFile(out_path, filename=out_name))
    await maybe_drop(cq.message, "车牌分包")
    await cq.message.answer("✅ 全部完成！", reply_markup=menu_kb(cq.from_user.id))
    await cleanup_state(state)
