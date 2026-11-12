"""提取手机号：支持一次上传多个文件（TXT / CSV / XLSX / XLS），
每个文件分别提取，最后合并导出（可选跨文件去重）。"""
import os
import re
import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services.file_ops import decode_bytes, sanitize_filename, xlsx_to_lines
from states import ExtractStates
from utils.drops import maybe_drop
from utils.helpers import cleanup_state, ensure_member, get_workdir, user_lock
from utils.keyboards import finish_upload_kb, menu_kb

router = Router(name="extract")

_SUPPORTED = (".txt", ".text", ".csv", ".xlsx", ".xls")
_RE_PHONE = re.compile(r"(?<![\d])(?:\+?86)?[ -]?1[3-9](?:[ -]?\d){9}(?![\d])")


def extract_phone_numbers(text: str) -> list[str]:
    """提取 11 位手机号（可带 +86 / 空格 / 横杠），返回纯数字。"""
    result = []
    for m in _RE_PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) > 11:
            digits = digits[-11:]
        if len(digits) == 11 and digits[0] == "1" and digits[1] in "3456789":
            result.append(digits)
    return result


def read_text_by_suffix(path: str, fname: str) -> str:
    """按扩展名读成文本（xlsx/xls 走表格转换）。"""
    with open(path, "rb") as f:
        content = f.read()
    if (fname or "").lower().endswith((".xlsx", ".xls")):
        return "\n".join(xlsx_to_lines(content))
    return decode_bytes(content)


def extract_dedupe_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="✅ 去重后输出", callback_data="extract:dedup:yes")
    b.button(text="❌ 不去重，全部输出", callback_data="extract:dedup:no")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "menu:extract")
async def menu_extract(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[], total_phones=0)
    await state.set_state(ExtractStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "📱 <b>提取手机号（支持多文件）</b>\n\n"
        "把文件一次性发过来，自动提取所有手机号"
        "（11 位、1 开头，可带 +86 / 空格 / 横杠）。\n\n"
        "· 支持格式：<b>.txt / .csv / .xlsx / .xls</b>\n"
        "· 可以一次发多个，全部发完点「✅ 完成上传」\n"
        f"· 单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB",
        reply_markup=finish_upload_kb("extract"),
    )


@router.message(ExtractStates.waiting_file, F.document)
async def extract_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    raw_name = doc.file_name or "input.txt"
    fname = raw_name.lower()

    if not fname.endswith(_SUPPORTED):
        await message.answer("⚠️ 只支持 txt / csv / xlsx / xls 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ <code>{raw_name}</code> 太大，单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。"
        )
        return

    data = await state.get_data()
    files = list(data.get("files") or [])
    if len(files) >= settings.MAX_UPLOAD_FILES:
        await message.answer(
            f"⚠️ 最多 {settings.MAX_UPLOAD_FILES} 个文件，请先点「✅ 完成上传」。",
            reply_markup=finish_upload_kb("extract"),
        )
        return

    workdir = data.get("workdir") or get_workdir(message.from_user.id)
    safe = sanitize_filename(raw_name)
    tmp_path = os.path.join(workdir, f"ex_{len(files) + 1}_{int(time.time() * 1000)}_{safe}")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        text = read_text_by_suffix(tmp_path, fname)
        phones = extract_phone_numbers(text)
    except Exception as e:
        err = str(e)
        if "Bad magic number" in err or "zip" in err.lower():
            tip = "文件损坏，请用 Excel 打开另存后再上传"
        elif "xlrd" in err.lower() or "xls" in err.lower():
            tip = "无法读取该 Excel，建议另存为 xlsx 再试"
        else:
            tip = err[:100]
        await message.answer(f"⚠️ <code>{raw_name}</code> 处理失败：{tip}", reply_markup=finish_upload_kb("extract"))
        return

    if not phones:
        await message.answer(
            f"⚠️ <code>{raw_name}</code> 里没找到有效手机号，已跳过。",
            reply_markup=finish_upload_kb("extract"),
        )
        return

    # 每个文件的结果先落盘，避免把几十万个号码塞进 FSM 内存
    list_path = os.path.join(workdir, f"phones_{len(files) + 1}_{int(time.time() * 1000)}.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(phones))

    async with user_lock(message.from_user.id):
        data = await state.get_data()
        files = list(data.get("files") or [])
        files.append({"name": raw_name, "count": len(phones), "path": list_path})
        total = sum(f["count"] for f in files)
        await state.update_data(files=files, total_phones=total, workdir=workdir)
        idx = len(files)

    await message.answer(
        f"✅ 已处理第 <b>{idx}</b> 个文件：<code>{raw_name}</code>\n"
        f"　本文件：<b>{len(phones):,}</b> 个手机号\n"
        f"　累计：{idx} 个文件 / <b>{total:,}</b> 个手机号\n\n"
        "可继续发送，全部发完点「✅ 完成上传」。",
        reply_markup=finish_upload_kb("extract"),
    )


@router.callback_query(ExtractStates.waiting_file, F.data == "extract:done")
async def extract_done_cb(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _ask_dedup(cq.message, state)


@router.message(ExtractStates.waiting_file, F.text)
async def extract_done_text(message: Message, state: FSMContext):
    if (message.text or "").strip() in ("完成", "done", "ok", "OK", "好"):
        await _ask_dedup(message, state)


async def _ask_dedup(event, state: FSMContext):
    data = await state.get_data()
    files = list(data.get("files") or [])
    if not files:
        await event.answer(
            "⚠️ 还没收到文件，请先发送。",
            reply_markup=finish_upload_kb("extract"),
        )
        return

    total = sum(f["count"] for f in files)
    shown = "\n".join(f"· {f['name']}（{f['count']:,} 个）" for f in files[:8])
    if len(files) > 8:
        shown += f"\n… 还有 {len(files) - 8} 个文件"

    await state.set_state(ExtractStates.waiting_dedup)
    await event.answer(
        f"✅ 共 <b>{len(files)}</b> 个文件，提取到 <b>{total:,}</b> 个手机号\n"
        f"{shown}\n\n"
        "要不要去除重复手机号？（跨所有文件一起去重）",
        reply_markup=extract_dedupe_kb(),
    )


@router.callback_query(ExtractStates.waiting_dedup, F.data.startswith("extract:dedup:"))
async def extract_dedup_choice(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    files = list(data.get("files") or [])
    workdir = data["workdir"]
    dedup = cq.data.endswith(":yes")

    phones: list[str] = []
    for f in files:
        try:
            with open(f["path"], encoding="utf-8") as fh:
                phones.extend(line.strip() for line in fh if line.strip())
        except Exception:
            continue

    if not phones:
        await cq.message.edit_text("⚠️ 没有可输出的手机号，请重新上传。")
        await cleanup_state(state)
        return

    if dedup:
        seen = set()
        unique = []
        for p in phones:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        removed = len(phones) - len(unique)
        output_phones = unique
    else:
        output_phones = phones
        removed = 0

    out_path = os.path.join(workdir, "提取手机号.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_phones))

    dedup_note = f"（已去重，去除重复 {removed:,} 个）" if dedup else "（未去重）"
    await cq.message.edit_text(
        f"✅ <b>提取完成</b>{dedup_note}\n\n"
        f"文件数：{len(files)} 个\n"
        f"提取总数：{len(phones):,} 个\n"
        f"输出数量：<b>{len(output_phones):,}</b> 个"
    )
    await cq.message.answer_document(FSInputFile(out_path, filename="提取手机号.txt"))
    await maybe_drop(cq.message, "提取手机号")
    await cq.message.answer("✅ 完成！", reply_markup=menu_kb(cq.from_user.id))
    await cleanup_state(state)
