"""车牌分包：上传文件（不限格式）→ 自动识别车牌与手机号 → 按用户指定顺序与行数输出 TXT。"""
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
from utils.helpers import cleanup_state, ensure_member, get_workdir, send_files_in_batches
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


def _split_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📄 一个文件（不分割）", callback_data="plate:split:all")
    b.button(text="500 行", callback_data="plate:split:500")
    b.button(text="1000 行", callback_data="plate:split:1000")
    b.button(text="2000 行", callback_data="plate:split:2000")
    b.button(text="5000 行", callback_data="plate:split:5000")
    b.button(text="✍️ 自定义行数", callback_data="plate:split:custom")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(2)
    return b.as_markup()


def _generate_outputs(records, phone_first, per_file, src_name, workdir):
    """按顺序与行数生成输出文件，返回 [(路径, 文件名)]。"""
    out_lines = []
    for r in records:
        if phone_first:
            out_lines.append(f"{r['phone']} {r['plate']}")
        else:
            out_lines.append(f"{r['plate']} {r['phone']}")

    stem = os.path.splitext(src_name)[0]
    order_tag = "手机号_车牌" if phone_first else "车牌_手机号"

    outputs: list[tuple[str, str]] = []
    if per_file <= 0 or per_file >= len(out_lines):
        out_name = f"{stem}_{order_tag}.txt"
        out_path = os.path.join(workdir, out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines))
        outputs.append((out_path, out_name))
    else:
        for idx in range(0, len(out_lines), per_file):
            chunk = out_lines[idx:idx + per_file]
            out_name = f"{stem}_{order_tag}_{(idx // per_file) + 1:02d}.txt"
            out_path = os.path.join(workdir, out_name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(chunk))
            outputs.append((out_path, out_name))
    return outputs


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
        "再按你指定的顺序与行数整理成 TXT 发回。\n\n"
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

    await state.update_data(phone_first=phone_first)
    await state.set_state(PlateStates.waiting_split)
    order_str = "手机号 → 车牌" if phone_first else "车牌 → 手机号"
    await cq.message.edit_text(
        f"✅ 顺序已选：<b>{order_str}</b>\n\n"
        "要不要按行数拆成多个文件？\n"
        "· 选「一个文件」则全部输出到一个 TXT\n"
        "· 选行数则每份 N 行，拆成多个 TXT 发送",
        reply_markup=_split_kb(),
    )


@router.callback_query(PlateStates.waiting_split, F.data == "plate:split:custom")
async def plate_split_custom(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.edit_text(
        f"请输入每份行数（正整数，不超过 {settings.MAX_SPLIT_LINES}）：",
        reply_markup=cancel_kb(),
    )


@router.message(PlateStates.waiting_split, F.text)
async def plate_split_custom_input(message: Message, state: FSMContext):
    import re
    m = re.fullmatch(r"\s*(\d+)\s*", message.text or "")
    if not m:
        await message.answer("请输入正整数，例如 <code>1000</code>。")
        return
    n = int(m.group(1))
    if not (1 <= n <= settings.MAX_SPLIT_LINES):
        await message.answer(f"每份行数需在 1 ~ {settings.MAX_SPLIT_LINES} 之间。")
        return
    await _do_finish(message, state, n)


@router.callback_query(PlateStates.waiting_split, F.data.startswith("plate:split:"))
async def plate_split_pick(cq: CallbackQuery, state: FSMContext):
    val = cq.data.split(":")[2]
    await cq.answer()
    per_file = 0 if val == "all" else int(val)
    await _do_finish(cq.message, state, per_file)


async def _do_finish(event, state: FSMContext, per_file: int):
    data = await state.get_data()
    records = data.get("records", [])
    phone_first = data.get("phone_first", True)
    src_name = data.get("src_name", "output")
    workdir = data["workdir"]

    if not records:
        await event.answer("⚠️ 会话异常，请重新开始。")
        await cleanup_state(state)
        return

    outputs = _generate_outputs(records, phone_first, per_file, src_name, workdir)
    order_str = "手机号 → 车牌" if phone_first else "车牌 → 手机号"
    split_note = "一个文件" if len(outputs) == 1 else f"共 {len(outputs)} 个文件（每份 {per_file} 行）"

    await event.answer(
        f"✅ 完成！共 <b>{len(records)}</b> 条，顺序：<b>{order_str}</b>\n"
        f"输出：<b>{split_note}</b>（空格分隔），开始发送："
    )
    await send_files_in_batches(event, outputs)
    await maybe_drop(event, "车牌分包")
    await event.answer("✅ 全部完成！", reply_markup=menu_kb(event.from_user.id))
    await cleanup_state(state)
