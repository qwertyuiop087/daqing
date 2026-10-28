"""号码去重：支持多文件批量上传，自动合并去重。"""
import asyncio
import html
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services.file_ops import decode_bytes, dedup_numbers, read_file
from states import DedupStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb

router = Router(name="dedup")


def dedup_finish_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="✅ 完成上传，开始去重", callback_data="dedup:done")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "menu:dedup")
async def menu_dedup(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), lines=[], file_count=0)
    await state.set_state(DedupStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🔄 <b>号码去重</b>\n\n"
        "请发送号码列表，<b>一行一个</b>（可直接发文本消息，或上传 TXT 文件）。\n\n"
        "💡 支持多个文件分批发：\n"
        "• 大文件拆成多个小文件（每个 20MB 以内）分开发\n"
        "• 全部发完后点「✅ 完成上传，开始去重」\n"
        "• 机器人自动合并所有内容，统一去重\n\n"
        "按纯数字判断是否重复（自动忽略空格、-、括号等），保留首次出现的号码。",
        reply_markup=cancel_kb(),
    )


@router.message(DedupStates.waiting_file, F.document)
async def dedup_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".txt", ".text")):
        await message.answer("⚠️ 只接受 TXT 文件。")
        return

    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ 文件太大了！单个 TXT 文件最大支持 20MB（Telegram 限制）。\n\n"
            f"当前文件：约 {doc.file_size // 1024 // 1024}MB\n\n"
            f"💡 解决办法：把大文件拆成多个小文件，分批发过来。"
        )
        return

    data = await state.get_data()
    file_count = data.get("file_count", 0) + 1
    workdir = data["workdir"]
    tmp = os.path.join(workdir, f"part_{file_count}.txt")

    # 下载中提示
    file_size_mb = (doc.file_size or 0) / 1024 / 1024
    progress_msg = await message.answer(f"⏳ 正在下载文件 {file_count}（{file_size_mb:.1f}MB）…")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=tmp)
    except Exception as e:
        await progress_msg.edit_text(f"⚠️ 文件下载失败：{str(e)[:100]}")
        return

    text = decode_bytes(read_file(tmp))
    new_lines = text.splitlines()

    all_lines = data.get("lines", [])
    all_lines.extend(new_lines)
    await state.update_data(lines=all_lines, file_count=file_count)

    await progress_msg.edit_text(
        f"✅ 已接收第 <b>{file_count}</b> 个文件：<code>{html.escape(doc.file_name or '')}</code>\n"
        f"　本行数：<b>{len(new_lines):,}</b> 行\n"
        f"　累计：{file_count} 个文件 / <b>{len(all_lines):,}</b> 行\n\n"
        f"可继续发送下一个文件，或点击「✅ 完成上传，开始去重」。",
        reply_markup=dedup_finish_kb(),
    )


@router.message(DedupStates.waiting_file, F.text)
async def dedup_got_text(message: Message, state: FSMContext):
    text = message.text or ""
    new_lines = text.splitlines()

    data = await state.get_data()
    all_lines = data.get("lines", [])
    all_lines.extend(new_lines)
    file_count = data.get("file_count", 0) + 1
    await state.update_data(lines=all_lines, file_count=file_count)

    await message.answer(
        f"✅ 已接收第 <b>{file_count}</b> 批文本\n"
        f"　本行数：<b>{len(new_lines):,}</b> 行\n"
        f"　累计：<b>{len(all_lines):,}</b> 行\n\n"
        f"可继续发送下一批，或点击「✅ 完成上传，开始去重」。",
        reply_markup=dedup_finish_kb(),
    )


@router.callback_query(DedupStates.waiting_file, F.data == "dedup:done")
async def dedup_finish(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    raw_lines = data.get("lines", [])
    file_count = data.get("file_count", 0)

    if not raw_lines:
        await cq.message.edit_text("⚠️ 还没有任何内容，请先发送号码。")
        return

    total = len(raw_lines)
    progress_msg = await cq.message.edit_text(
        f"⏳ <b>正在去重…</b>\n\n"
        f"文件数：{file_count} 个\n"
        f"总行数：{total:,} 行\n"
        f"正在处理，请稍候…"
    )

    await asyncio.sleep(0.1)

    unique, removed = dedup_numbers(raw_lines)
    if not unique:
        await progress_msg.edit_text("未识别到有效号码，请重新发送。")
        await cleanup_state(state)
        return
    skipped = total - len(unique) - removed

    workdir = data["workdir"]
    out_path = os.path.join(workdir, "去重结果.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(unique))

    extra = f"\n另有 {skipped:,} 行空行/非号码行已忽略。" if skipped else ""
    bar = "█" * 20
    await progress_msg.edit_text(
        f"✅ <b>去重完成</b>\n\n"
        f"输入文件：{file_count} 个\n"
        f"总行数：{total:,} 行\n"
        f"去除重复：<b>{removed:,}</b> 个\n"
        f"保留号码：<b>{len(unique):,}</b> 个{extra}\n"
        f"{bar} 100%"
    )
    await cq.message.answer_document(FSInputFile(out_path, filename="去重结果.txt"))
    await maybe_drop(cq.message, "号码去重")
    await cq.message.answer("✅ 完成！", reply_markup=menu_kb(cq.from_user.id))
    await cleanup_state(state)
