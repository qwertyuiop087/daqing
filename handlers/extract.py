"""提取手机号：支持 TXT / CSV / XLSX / XLS，可选择是否去除重复。
照抄老版本逻辑。"""
import os
import zipfile

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services.file_ops import decode_bytes, xlsx_to_lines
from states import ExtractStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb

router = Router(name="extract")

_SUPPORTED = (".txt", ".text", ".csv", ".xlsx", ".xls")


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
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(ExtractStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "📱 <b>提取手机号</b>\n\n"
        "上传文件，自动提取里面所有手机号（11 位、1 开头，可带 +86/空格/横杠）。\n\n"
        "支持格式：<b>.txt / .csv / .xlsx / .xls</b>\n\n"
        f"请发送文件，单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。",
        reply_markup=cancel_kb(),
    )


@router.message(ExtractStates.waiting_file, F.document)
async def extract_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    fname = (doc.file_name or "").lower()

    # 支持的格式
    if not fname.endswith(_SUPPORTED):
        await message.answer("⚠️ 只支持 txt / csv / xlsx / xls 文件。")
        return

    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ 文件太大了！单个文件最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。"
        )
        return

    data = await state.get_data()
    workdir = data["workdir"]
    tmp_path = os.path.join(workdir, doc.file_name or "input")

    progress_msg = await message.answer(f"⏳ 正在下载文件（{(doc.file_size or 0)/1024/1024:.1f}MB）…")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
    except Exception as e:
        await progress_msg.edit_text(f"⚠️ 文件下载失败：{str(e)[:100]}")
        return

    await progress_msg.edit_text("⏳ 正在提取手机号…")

    # 提取手机号，照抄老版本逻辑
    import re
    _RE_PHONE = re.compile(r"(?<![\d])(?:\+?86)?[ -]?1[3-9](?:[ -]?\d){9}(?![\d])")

    def extract_phone_numbers(text: str) -> list[str]:
        result = []
        for m in _RE_PHONE.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) > 11:
                digits = digits[-11:]
            if len(digits) == 11 and digits[0] == "1" and digits[1] in "3456789":
                result.append(digits)
        return result

    phones = []
    try:
        if fname.endswith((".xlsx", ".xls")):
            with open(tmp_path, "rb") as f:
                content = f.read()
            lines = xlsx_to_lines(content)
            text = "\n".join(lines)
        else:
            with open(tmp_path, "rb") as f:
                content = f.read()
            text = decode_bytes(content)

        phones = extract_phone_numbers(text)
    except Exception as e:
        err_str = str(e)
        if "Bad magic number" in err_str or "zip" in err_str.lower():
            friendly = "⚠️ Excel文件损坏了，请用Excel打开重新保存一下再上传。"
        elif "xlrd" in err_str.lower() or "xls" in err_str.lower():
            friendly = "⚠️ 无法读取Excel文件，请确认文件没有损坏，或者另存为xlsx格式后再上传。"
        else:
            friendly = f"⚠️ 提取失败：{err_str[:200]}"
        await progress_msg.edit_text(friendly)
        await cleanup_state(state)
        return

    if not phones:
        await progress_msg.edit_text("未在文件中找到有效手机号。")
        await cleanup_state(state)
        return

    await state.update_data(phones=phones)

    total = len(phones)
    await progress_msg.edit_text(
        f"✅ <b>提取完成</b>\n\n"
        f"共提取到：<b>{total:,}</b> 个手机号\n\n"
        f"要不要去除重复手机号？",
        reply_markup=extract_dedupe_kb(),
    )
    await state.set_state(ExtractStates.waiting_dedup)


@router.callback_query(ExtractStates.waiting_dedup, F.data.startswith("extract:dedup:"))
async def extract_dedup_choice(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    phones = data.get("phones", [])
    workdir = data["workdir"]

    dedup = cq.data.endswith(":yes")

    if dedup:
        # 去重，保留首次出现
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

    bar = "█" * 20
    dedup_note = f"（已去重，去除重复 {removed:,} 个）" if dedup else "（未去重）"
    await cq.message.edit_text(
        f"✅ <b>提取完成</b>{dedup_note}\n\n"
        f"提取总数：{len(phones):,} 个\n"
        f"输出数量：<b>{len(output_phones):,}</b> 个\n"
        f"{bar} 100%"
    )
    await cq.message.answer_document(FSInputFile(out_path, filename="提取手机号.txt"))
    await maybe_drop(cq.message, "提取手机号")
    await cq.message.answer("✅ 完成！", reply_markup=menu_kb(cq.from_user.id))
    await cleanup_state(state)
