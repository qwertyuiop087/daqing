"""解压 ZIP 提取 TXT 文件。"""
import os
import zipfile

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from services.file_ops import extract_txt_from_zip, read_file
from states import ZipStates
from utils.helpers import cleanup_state, ensure_member, get_workdir, send_files_in_batches
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb

router = Router(name="zip_extract")


@router.callback_query(F.data == "menu:zip")
async def menu_zip(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(ZipStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        f"🗜️ <b>解压Zip提取TXT</b>\n\n"
        f"请发送 ZIP 文件（≤{settings.MAX_ZIP_SIZE // 1024 // 1024}MB），"
        f"将自动提取其中的 TXT 文件（最多 {settings.MAX_ZIP_TXT_FILES} 个）。",
        reply_markup=cancel_kb(),
    )


@router.message(ZipStates.waiting_file, F.document)
async def zip_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        await message.answer("⚠️ 请发送 <b>.zip</b> 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_ZIP_SIZE:
        await message.answer(f"⚠️ ZIP 过大，最大支持 {settings.MAX_ZIP_SIZE // 1024 // 1024}MB。")
        return

    data = await state.get_data()
    zip_path = os.path.join(data["workdir"], "upload.zip")
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=zip_path)

    raw = read_file(zip_path)
    try:
        extracted = extract_txt_from_zip(
            raw,
            max_txt=settings.MAX_ZIP_TXT_FILES,
            max_size=settings.MAX_ZIP_SIZE,
            max_total_size=settings.MAX_ZIP_SIZE * 2,
        )
    except (zipfile.BadZipFile, NotImplementedError):
        await message.answer("⚠️ 无法解析该 ZIP 文件。")
        return
    if not extracted:
        await message.answer("⚠️ 压缩包内没有 TXT 文件。")
        return

    out: list[tuple[str, str]] = []
    for name, text in extracted:
        p = os.path.join(data["workdir"], name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        out.append((p, name))

    await message.answer(f"✅ 提取到 <b>{len(out)}</b> 个 TXT 文件，开始发送：")
    await send_files_in_batches(message, out)
    await maybe_drop(message, "解压Zip提取TXT")
    await message.answer("✅ 完成！", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
