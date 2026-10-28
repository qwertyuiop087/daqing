"""合并多个 TXT 文件。"""
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from services.file_ops import decode_bytes, merge_texts, read_file, sanitize_filename
from states import MergeStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, finish_upload_kb, menu_kb, merge_skip_filename_kb

router = Router(name="merge")


@router.callback_query(F.data == "menu:merge")
async def menu_merge(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[], total_lines=0)
    await state.set_state(MergeStates.waiting_files)
    await cq.answer()
    await cq.message.edit_text(
        f"📚 <b>合并文本</b>\n\n"
        f"请发送要合并的 TXT 文件（可多选，最多 {settings.MAX_UPLOAD_FILES} 个），\n"
        f"上传完成后点击「✅ 完成上传」。\n\n"
        f"每发一个文件会自动显示行数；上传完可自定义导出文件名。",
        reply_markup=finish_upload_kb("merge"),
    )


@router.message(MergeStates.waiting_files, F.document)
async def merge_got_file(message: Message, state: FSMContext, bot: Bot):
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

    # 读取当前文件并统计行数
    content = decode_bytes(read_file(path))
    lines = len(content.splitlines())

    files.append((name, path))

    # 重新统计所有文件的总行数（保证准确）
    total_lines = 0
    for f_name, f_path in files:
        f_content = decode_bytes(read_file(f_path))
        total_lines += len(f_content.splitlines())

    await state.update_data(files=files, total_lines=total_lines)

    await message.answer(
        f"✅ 已接收第 <b>{len(files)}</b> 个文件：<code>{name}</code>\n"
        f"　本文件：<b>{lines}</b> 行\n"
        f"　累计：{len(files)} 个文件 / <b>{total_lines}</b> 行\n\n"
        f"可继续发送，或点击「✅ 完成上传」，\n"
        f"也可以直接发文字「完成」开始合并。"
    )


@router.callback_query(MergeStates.waiting_files, F.data == "merge:done")
async def merge_done(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _ask_merge_filename(cq.message, state)


@router.message(MergeStates.waiting_files, F.text)
async def merge_text_done(message: Message, state: FSMContext):
    """用户直接发文字「完成」也能触发合并。"""
    text = (message.text or "").strip()
    if text in ("完成", "合并", "done", "ok", "OK", "好"):
        await _ask_merge_filename(message, state)


async def _ask_merge_filename(message: Message, state: FSMContext):
    """文件收齐后询问导出文件名。"""
    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    if len(files) < 2:
        await message.answer(
            "⚠️ 至少需要 2 个文件才能合并，请继续上传。",
            reply_markup=finish_upload_kb("merge"),
        )
        return
    await state.set_state(MergeStates.waiting_filename)
    await message.answer(
        "📚 文件已收齐，请发送<b>导出文件名</b>：\n\n"
        "• 例如发「名单」→ 生成 <b>名单.txt</b>\n"
        "• 发「合并结果.txt」→ 就用你给的名字\n"
        "• 想直接用默认名，点下方「跳过」按钮",
        reply_markup=merge_skip_filename_kb(),
    )


@router.callback_query(MergeStates.waiting_filename, F.data == "merge:name:skip")
async def merge_filename_skip(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _do_merge(cq.message, state, filename="合并结果.txt")


@router.message(MergeStates.waiting_filename, F.text)
async def merge_filename_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("文件名不能为空，请重新发送。")
        return
    name = sanitize_filename(raw)
    if not name.lower().endswith((".txt", ".text")):
        name += ".txt"
    await _do_merge(message, state, filename=name)


async def _do_merge(message: Message, state: FSMContext, filename: str = "合并结果.txt"):
    """实际执行合并：直接拼接，导出为用户指定的文件名。"""
    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])

    if len(files) < 2:
        await message.answer("⚠️ 至少需要 2 个文件才能合并，请继续上传。", reply_markup=finish_upload_kb("merge"))
        return

    await message.answer(f"⏳ 正在合并 {len(files)} 个文件…")

    contents = [(name, decode_bytes(read_file(path))) for name, path in files]
    merged = merge_texts(contents, with_headers=False)

    # 输出到独立子目录，避免和用户上传的同名文件冲突
    out_dir = os.path.join(data["workdir"], "out")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(merged)

    merged_lines = len(merged.splitlines())
    await message.answer_document(FSInputFile(out_path, filename=filename))
    await maybe_drop(message, "合并文本")
    await message.answer(
        f"✅ 完成！\n"
        f"文件数：{len(files)} 个\n"
        f"总行数：{merged_lines} 行",
        reply_markup=menu_kb(message.from_user.id),
    )
    await cleanup_state(state)
