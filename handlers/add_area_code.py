"""加区号：给txt里的手机号前面加+86。"""
import os
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from services.file_ops import decode_bytes
from states import AddAreaCodeStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb

router = Router(name="add_area_code")

_SUPPORTED = (".txt", ".text", ".csv")


@router.callback_query(F.data == "menu:addcode")
async def menu_add_code(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(AddAreaCodeStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🇨🇳 <b>加区号</b>\n\n"
        "上传TXT文件，自动给里面的手机号前面加 <b>+86</b>。\n\n"
        "支持格式：<b>.txt / .csv</b>\n\n"
        f"请发送文件，单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。",
        reply_markup=cancel_kb(),
    )


@router.message(AddAreaCodeStates.waiting_file, F.document)
async def add_code_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    fname = (doc.file_name or "").lower()

    # 支持的格式
    if not fname.endswith(_SUPPORTED):
        await message.answer("⚠️ 只支持 txt / csv 文件。")
        return

    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ 文件太大了！单个文件最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。"
        )
        return

    data = await state.get_data()
    workdir = data["workdir"]
    tmp_path = os.path.join(workdir, doc.file_name or "input.txt")

    progress_msg = await message.answer(f"⏳ 正在下载文件（{(doc.file_size or 0)/1024/1024:.1f}MB）…")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
    except Exception as e:
        await progress_msg.edit_text(f"⚠️ 文件下载失败：{str(e)[:100]}")
        return

    await progress_msg.edit_text("⏳ 正在加区号…")

    # 读取文件
    try:
        with open(tmp_path, "rb") as f:
            content = f.read()
        text = decode_bytes(content)
    except Exception as e:
        await progress_msg.edit_text(f"⚠️ 文件读取失败：{str(e)[:200]}")
        await cleanup_state(state)
        return

    # 逐行处理，给手机号前面加+86
    lines = text.splitlines()
    output_lines = []
    added_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 找手机号
        m = re.search(r"(?<![\d+])(1[3-9]\d{9})(?![\d])", line)
        if m:
            phone = m.group(1)
            # 如果已经有+86就不加
            if "+86" not in line:
                new_line = line.replace(phone, f"+86{phone}")
                output_lines.append(new_line)
                added_count += 1
            else:
                output_lines.append(line)
        else:
            # 不是手机号的行，直接保留
            output_lines.append(line)

    if added_count == 0:
        await progress_msg.edit_text("⚠️ 文件里没找到需要加区号的手机号（可能已经有+86了）。")
        await cleanup_state(state)
        return

    # 输出文件
    out_path = os.path.join(workdir, "加区号后.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))

    bar = "█" * 20
    await progress_msg.edit_text(
        f"✅ <b>加区号完成</b>\n\n"
        f"共处理：<b>{len(lines):,}</b> 行\n"
        f"加了区号：<b>{added_count:,}</b> 个\n"
        f"{bar} 100%"
    )
    await message.answer_document(FSInputFile(out_path, filename="加区号后.txt"))
    await maybe_drop(message, "加区号")
    await message.answer("✅ 完成！", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
