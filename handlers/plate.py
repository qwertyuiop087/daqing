"""车牌分包：支持一次上传多个文件，自动识别 名字 / 车牌 / 手机号，
按用户指定顺序与行数整理成 TXT 发回。"""
import os
import re
import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services.file_ops import (
    decode_bytes,
    extract_name,
    extract_plates,
    extract_txt_from_zip,
    read_file,
    sanitize_filename,
    xlsx_to_lines,
)
from states import PlateStates
from utils.drops import maybe_drop
from utils.helpers import (
    cleanup_state,
    ensure_member,
    get_workdir,
    send_files_in_batches,
    user_lock,
)
from utils.keyboards import cancel_kb, finish_upload_kb, menu_kb
from utils.phone_db import extract_phones

router = Router(name="plate")

# 只保留三种字段，顺序可自定义
_FIELD_KEYS = {"手机号": "phone", "车牌": "plate", "名字": "name"}
_FIELD_LABELS = {"phone": "手机号", "plate": "车牌", "name": "名字"}
_ORDER_PRESETS = {
    "name": ["name", "plate", "phone"],
    "plate": ["plate", "phone", "name"],
    "phone": ["phone", "plate", "name"],
}


def _parse_order(text: str):
    """解析顺序输入，如「名字 车牌 手机号」。返回 (keys, 错误字段)。"""
    keys = []
    for part in re.split(r"[\s,，、/]+", (text or "").strip()):
        if not part:
            continue
        if part in _FIELD_KEYS:
            keys.append(_FIELD_KEYS[part])
        else:
            return None, part
    return keys, None


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


def parse_records(lines: list[str]) -> list[dict]:
    """从文本行里提取 名字/车牌/手机号，返回记录列表。"""
    records = []
    for ln in lines:
        ln = (ln or "").strip()
        if not ln:
            continue
        phones = extract_phones(ln)
        plates = extract_plates(ln)
        nm = extract_name(ln)
        if phones or plates or nm:
            records.append(
                {
                    "phone": phones[0] if phones else "",
                    "plate": plates[0] if plates else "",
                    "name": nm,
                }
            )
    return records


def _order_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 名字 车牌 手机号", callback_data="plate:order:name")
    b.button(text="🚗 车牌 手机号 名字", callback_data="plate:order:plate")
    b.button(text="📱 手机号 车牌 名字", callback_data="plate:order:phone")
    b.button(text="✍️ 自定义顺序", callback_data="plate:order:custom")
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


def generate_outputs(records, order, per_file, stem, workdir):
    """按顺序与行数生成输出文件，返回 [(路径, 文件名)]。"""
    # 空字段直接过滤，保证字段之间只有一个空格
    out_lines = [
        " ".join(p for p in (r.get(k, "") for k in order) if p)
        for r in records
    ]

    order_tag = "_".join(_FIELD_LABELS[k] for k in order)

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
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[], total_records=0)
    await state.set_state(PlateStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🚗 <b>车牌分包（支持多文件）</b>\n\n"
        "把文件一次性发过来，我会自动识别 <b>名字 / 车牌 / 手机号</b>，"
        "再按你指定的顺序与行数整理成 TXT 发回。\n\n"
        "· 可以一次发多个文件，全部发完点「✅ 完成上传」\n"
        "· 支持格式：<b>txt / csv / xlsx / xls / zip</b>\n"
        f"· 单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB",
        reply_markup=finish_upload_kb("plate"),
    )


@router.message(PlateStates.waiting_file, F.document)
async def plate_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = sanitize_filename(doc.file_name or "input.txt")
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ <code>{name}</code> 太大，单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。"
        )
        return

    data = await state.get_data()
    files = list(data.get("files") or [])
    if len(files) >= settings.MAX_UPLOAD_FILES:
        await message.answer(
            f"⚠️ 最多 {settings.MAX_UPLOAD_FILES} 个文件，请先点「✅ 完成上传」。",
            reply_markup=finish_upload_kb("plate"),
        )
        return

    workdir = data.get("workdir") or get_workdir(message.from_user.id)
    # 序号前缀：避免同名文件互相覆盖
    path = os.path.join(workdir, f"plate_{len(files) + 1}_{int(time.time() * 1000)}_{name}")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=path)
        lines = _read_to_lines(path, name)
    except Exception as e:
        await message.answer(f"⚠️ <code>{name}</code> 读取失败：{str(e)[:100]}")
        return

    records = parse_records(lines)
    if not records:
        await message.answer(
            f"⚠️ <code>{name}</code> 里没识别到名字 / 车牌 / 手机号，已跳过。",
            reply_markup=finish_upload_kb("plate"),
        )
        return

    both = sum(1 for r in records if r["phone"] and r["plate"])
    has_name = sum(1 for r in records if r["name"])
    async with user_lock(message.from_user.id):
        data = await state.get_data()
        files = list(data.get("files") or [])
        files.append(
            {
                "name": name,
                "path": path,
                "records": len(records),
                "both": both,
                "has_name": has_name,
            }
        )
        total_records = sum(f["records"] for f in files)
        await state.update_data(files=files, total_records=total_records, workdir=workdir)
        idx = len(files)

    await message.answer(
        f"✅ 已接收第 <b>{idx}</b> 个文件：<code>{name}</code>\n"
        f"　本文件识别：<b>{len(records)}</b> 条（手机号+车牌 {both} · 有名字 {has_name}）\n"
        f"　累计：{idx} 个文件 / <b>{total_records:,}</b> 条记录\n\n"
        "可继续发送，全部发完点「✅ 完成上传」。",
        reply_markup=finish_upload_kb("plate"),
    )


@router.callback_query(PlateStates.waiting_file, F.data == "plate:done")
async def plate_done_cb(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _ask_order(cq.message, state)


@router.message(PlateStates.waiting_file, F.text)
async def plate_done_text(message: Message, state: FSMContext):
    """发文字「完成」也能触发下一步。"""
    if (message.text or "").strip() in ("完成", "done", "ok", "OK", "好"):
        await _ask_order(message, state)


async def _ask_order(event, state: FSMContext):
    data = await state.get_data()
    files = list(data.get("files") or [])
    if not files:
        await event.answer(
            "⚠️ 还没收到文件，请先发送要处理的文件。",
            reply_markup=finish_upload_kb("plate"),
        )
        return

    total_records = sum(f["records"] for f in files)
    both = sum(f["both"] for f in files)
    has_name = sum(f["has_name"] for f in files)
    shown = "\n".join(f"· {f['name']}（{f['records']:,} 条）" for f in files[:8])
    if len(files) > 8:
        shown += f"\n… 还有 {len(files) - 8} 个文件"

    await state.set_state(PlateStates.waiting_order)
    await event.answer(
        f"✅ 共收到 <b>{len(files)}</b> 个文件、<b>{total_records:,}</b> 条记录\n"
        f"{shown}\n\n"
        f"· 手机号+车牌配对：<b>{both:,}</b> 条\n"
        f"· 识别到名字：<b>{has_name:,}</b> 条\n\n"
        "请选择输出顺序（三种字段可任意排列，也可以自定义）：",
        reply_markup=_order_kb(),
    )


@router.callback_query(PlateStates.waiting_order, F.data.startswith("plate:order:"))
async def plate_order_pick(cq: CallbackQuery, state: FSMContext):
    val = cq.data.split(":")[2]
    await cq.answer()
    if val == "custom":
        await cq.message.edit_text(
            "请发送你想要的顺序，例如：\n<code>名字 车牌 手机号</code>\n\n"
            "可用字段：<b>名字 / 车牌 / 手机号</b>（空格分隔，可任意排列）。",
            reply_markup=cancel_kb(),
        )
        return
    order = _ORDER_PRESETS.get(val)
    if not order:
        return
    await _ask_split(cq.message, state, order)


@router.message(PlateStates.waiting_order, F.text)
async def plate_order_text(message: Message, state: FSMContext):
    order, bad = _parse_order(message.text or "")
    if bad is not None:
        await message.answer(
            f"❌ 无法识别字段「<b>{bad}</b>」。可选：名字 / 车牌 / 手机号。请重新发送。"
        )
        return
    if not order:
        await message.answer("请输入至少一个字段，例如：名字 车牌 手机号。")
        return
    await _ask_split(message, state, order)


async def _ask_split(event, state: FSMContext, order: list[str]):
    await state.update_data(order=order)
    await state.set_state(PlateStates.waiting_split)
    order_str = " → ".join(_FIELD_LABELS[k] for k in order)
    await event.answer(
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
    files = list(data.get("files") or [])
    order = data.get("order", ["name", "plate", "phone"])
    workdir = data["workdir"]

    if not files:
        await event.answer("⚠️ 会话异常，请重新开始。")
        await cleanup_state(state)
        return

    progress = await event.answer(f"⏳ 正在整理 {len(files)} 个文件…")

    # 多个文件合并成一批再分包：记录跨文件连续编号，分包不会切断某一份
    records: list[dict] = []
    failed = 0
    for f in files:
        try:
            lines = _read_to_lines(f["path"], f["name"])
            records.extend(parse_records(lines))
        except Exception:
            failed += 1
            continue

    if not records:
        await progress.edit_text("⚠️ 没有可用记录，请重新上传文件。")
        await cleanup_state(state)
        return

    # 只有一个文件时沿用原文件名，多文件时统一命名
    stem = (
        os.path.splitext(files[0]["name"])[0] if len(files) == 1 else "车牌分包"
    )
    outputs = generate_outputs(records, order, per_file, stem, workdir)
    order_str = " → ".join(_FIELD_LABELS[k] for k in order)
    split_note = "一个文件" if len(outputs) == 1 else f"共 {len(outputs)} 个文件（每份 {per_file} 行）"

    summary = (
        f"✅ 完成！共 <b>{len(records):,}</b> 条记录"
        + (f"（{failed} 个文件读取失败）" if failed else "")
        + f"\n顺序：<b>{order_str}</b>\n"
        f"输出：<b>{split_note}</b>（空格分隔），开始发送："
    )
    try:
        await progress.edit_text(summary)
    except Exception:
        await event.answer(summary)

    await send_files_in_batches(event, outputs)
    await maybe_drop(event, "车牌分包")
    await event.answer("✅ 全部完成！", reply_markup=menu_kb(event.from_user.id))
    await cleanup_state(state)
