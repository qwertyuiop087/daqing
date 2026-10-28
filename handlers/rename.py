"""批量重命名 TXT 文件（前缀 + 序号）。"""
import os
import shutil

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from services.file_ops import sanitize_filename
from states import RenameStates
from utils.helpers import cleanup_state, ensure_member, get_workdir, send_files_in_batches
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, finish_upload_kb, menu_kb

router = Router(name="rename")

_ILLEGAL = set('/\\:*?"<>|')


@router.callback_query(F.data == "menu:rename")
async def menu_rename(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[])
    await state.set_state(RenameStates.waiting_files)
    await cq.answer()
    await cq.message.edit_text(
        f"✏️ <b>批量重命名</b>\n\n"
        f"请发送要重命名的 TXT 文件（可多选，最多 {settings.MAX_UPLOAD_FILES} 个），"
        f"上传完成后点击「✅ 完成上传」。",
        reply_markup=finish_upload_kb("rename"),
    )


@router.message(RenameStates.waiting_files, F.document)
async def rename_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".txt", ".text")):
        await message.answer("⚠️ 只接受 TXT 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(f"⚠️ 单个文件过大，最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。")
        return

    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    if len(files) >= settings.MAX_UPLOAD_FILES:
        await message.answer(f"⚠️ 最多上传 {settings.MAX_UPLOAD_FILES} 个文件，请先点击「✅ 完成上传」。")
        return

    name = sanitize_filename(doc.file_name or f"file_{len(files) + 1}.txt")
    path = os.path.join(data["workdir"], name)
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=path)
    files.append((name, path))
    await state.update_data(files=files)
    await message.answer(f"✅ 已接收 <b>{len(files)}</b> 个文件，可继续发送，或点击「✅ 完成上传」。")


@router.callback_query(RenameStates.waiting_files, F.data == "rename:done")
async def rename_done(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    if not files:
        await cq.message.edit_text("⚠️ 请先上传文件。", reply_markup=finish_upload_kb("rename"))
        return
    await cq.message.edit_text(
        "请输入<b>新文件名前缀</b>（例如：名单 → 生成 名单_1.txt、名单_2.txt …）：",
        reply_markup=cancel_kb(),
    )
    await state.set_state(RenameStates.waiting_name)


@router.message(RenameStates.waiting_name, F.text)
async def rename_get_prefix(message: Message, state: FSMContext):
    base = (message.text or "").strip()
    if not base:
        await message.answer("前缀不能为空。")
        return
    if any(ch in _ILLEGAL for ch in base):
        await message.answer("⚠️ 前缀不能包含 / \\ : * ? \" ＜＞ | 等字符。")
        return

    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    out: list[tuple[str, str]] = []
    for i, (_, path) in enumerate(files, 1):
        new_name = f"{base}_{i}.txt"
        new_path = os.path.join(data["workdir"], new_name)
        shutil.copy2(path, new_path)
        out.append((new_path, new_name))

    await message.answer(f"✅ 已重命名为「{base}_序号.txt」，开始发送：")
    await send_files_in_batches(message, out)
    await maybe_drop(message, "批量重命名")
    await message.answer(f"✅ 完成！共 {len(out)} 个文件。", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
