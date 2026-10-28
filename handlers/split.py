"""文件切割 + 可选随机插入手机号 + 插入报告。"""
import html
import os
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from services.file_ops import (
    decode_bytes,
    insert_phones_per_file,
    parse_numbers,
    read_file,
    sanitize_filename,
    split_lines,
)
from states import SplitStates
from utils.helpers import cleanup_state, ensure_member, get_workdir, reply_event, send_files_in_batches
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb, split_insert_kb, split_lines_kb

router = Router(name="split")


def split_skip_filename_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ 跳过，用原文件名", callback_data="split:filename:skip")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "menu:split")
async def menu_split(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(SplitStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        f"📄 <b>文件切割</b>\n\n请发送要切割的 <b>TXT 文件</b>（≤{settings.MAX_FILE_SIZE // 1024 // 1024}MB）。",
        reply_markup=cancel_kb(),
    )


@router.message(SplitStates.waiting_file, F.document)
async def split_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".txt", ".text")):
        await message.answer("⚠️ 请发送 <b>.txt</b> 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(f"⚠️ 文件过大，最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。")
        return

    data = await state.get_data()
    name = sanitize_filename(doc.file_name or "input.txt")
    path = os.path.join(data["workdir"], name)
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=path)

    content = decode_bytes(read_file(path))
    lines_count = len(content.splitlines())
    if lines_count == 0:
        await message.answer("⚠️ 文件内容为空，无法切割。")
        return

    await state.update_data(src_path=path)
    await state.set_state(SplitStates.waiting_lines)
    await message.answer(
        f"✅ 已收到 <code>{name}</code>，共 <b>{lines_count}</b> 行。\n\n每份要多少行？",
        reply_markup=split_lines_kb(),
    )


@router.callback_query(SplitStates.waiting_lines, F.data.startswith("split:lines:"))
async def split_lines_pick(cq: CallbackQuery, state: FSMContext):
    val = cq.data.split(":")[-1]
    await cq.answer()
    if val == "custom":
        await cq.message.edit_text(
            f"请输入每份行数（正整数，不超过 {settings.MAX_SPLIT_LINES}）：",
            reply_markup=cancel_kb(),
        )
        return
    try:
        n = int(val)
    except ValueError:
        return
    await do_split(cq, state, n)


@router.message(SplitStates.waiting_lines, F.text)
async def split_lines_custom(message: Message, state: FSMContext):
    m = re.fullmatch(r"\s*(\d+)\s*", message.text or "")
    if not m:
        await message.answer("请输入正整数，例如 <code>1000</code>。")
        return
    n = int(m.group(1))
    if not (1 <= n <= settings.MAX_SPLIT_LINES):
        await message.answer(f"每份行数需在 1 ~ {settings.MAX_SPLIT_LINES} 之间。")
        return
    await do_split(message, state, n)


async def do_split(event, state: FSMContext, lines_per: int):
    data = await state.get_data()
    src = data.get("src_path")
    workdir = data["workdir"]
    content = decode_bytes(read_file(src))
    chunks = split_lines(content, lines_per)
    if not chunks:
        await reply_event(event, "⚠️ 文件为空，无法切割。")
        await cleanup_state(state)
        return
    if len(chunks) > settings.MAX_SPLIT_FILES:
        await reply_event(
            event,
            f"⚠️ 将生成 <b>{len(chunks)}</b> 份，超过单次上限 {settings.MAX_SPLIT_FILES} 份，请调大每份行数。",
        )
        return

    stem = os.path.splitext(os.path.basename(src))[0]
    # 缓存原文件名前缀，等用户输入新前缀后再生成最终文件名
    await state.update_data(chunks=chunks, stem=stem)

    # 先问要不要自定义文件名前缀
    await state.set_state(SplitStates.waiting_filename)
    await reply_event(
        event,
        f"✅ 已切割为 <b>{len(chunks)}</b> 份。\n\n"
        f"原文件名前缀：<code>{stem}</code>\n\n"
        f"要不要改成自定义前缀？\n"
        f"• 直接发文字 = 自定义前缀（比如「北京」→ 北京_01.txt）\n"
        f"• 点「跳过」= 用原文件名",
        reply_markup=split_skip_filename_kb(),
    )


async def _finish_split_with_prefix(event, state: FSMContext, prefix: str):
    """根据前缀生成最终文件，然后进插入手机号步骤。"""
    data = await state.get_data()
    chunks = data.get("chunks", [])
    workdir = data["workdir"]

    final_stem = prefix if prefix else data.get("stem", "output")
    chunk_dir = os.path.join(workdir, "chunks")
    os.makedirs(chunk_dir, exist_ok=True)
    paths: list[tuple[str, str]] = []
    for i, chunk in enumerate(chunks, 1):
        fname = f"{final_stem}_{i:02d}.txt"
        p = os.path.join(chunk_dir, fname)
        with open(p, "w", encoding="utf-8") as f:
            f.write(chunk)
        paths.append((p, fname))

    await state.update_data(chunk_paths=paths, final_stem=final_stem)
    await state.set_state(SplitStates.waiting_insert)
    await reply_event(
        event,
        f"✅ 文件名前缀：<code>{html.escape(final_stem)}</code>\n\n是否需要<b>插入手机号</b>？",
        reply_markup=split_insert_kb(),
    )


@router.callback_query(SplitStates.waiting_filename, F.data == "split:filename:skip")
async def split_filename_skip(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _finish_split_with_prefix(cq.message, state, prefix="")


@router.message(SplitStates.waiting_filename, F.text)
async def split_filename_input(message: Message, state: FSMContext):
    prefix = (message.text or "").strip()
    # 简单清理非法字符
    prefix = prefix.replace("/", "_").replace("\\", "_").replace(":", "_")
    await _finish_split_with_prefix(message, state, prefix=prefix)


@router.callback_query(SplitStates.waiting_insert, F.data.in_({"split:insert:yes", "split:insert:no"}))
async def split_insert_choice(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    if cq.data == "split:insert:no":
        await finish_split(cq.message, state)
        return

    data = await state.get_data()
    file_count = len(data.get("chunk_paths", []))
    await cq.message.edit_text(
        "📱 <b>插入手机号</b>\n\n"
        f"共 <b>{file_count}</b> 份文件，每份要插入<b>多少个</b>手机号？\n"
        "（输入数字，例如 <code>3</code> = 每份随机插入 3 个手机号）",
        reply_markup=cancel_kb(),
    )
    await state.set_state(SplitStates.waiting_phones_count)


@router.message(SplitStates.waiting_phones_count, F.text)
async def split_phones_count(message: Message, state: FSMContext):
    m = re.fullmatch(r"\s*(\d+)\s*", message.text or "")
    if not m:
        await message.answer("请输入正整数，例如 <code>3</code>。")
        return
    n = int(m.group(1))
    if not (1 <= n <= settings.MAX_PHONES):
        await message.answer(f"每份插入数量需在 1 ~ {settings.MAX_PHONES} 之间。")
        return

    data = await state.get_data()
    file_count = len(data.get("chunk_paths", []))
    needed = file_count * n
    await state.update_data(per_file=n)
    await state.set_state(SplitStates.waiting_phones)
    await message.answer(
        f"✅ 每份将随机插入 <b>{n}</b> 个手机号，共需要 <b>{needed}</b> 个。\n\n"
        "请发送手机号，<b>一行一个</b>（可直接发文本消息，或上传 TXT 文件）。\n"
        f"最多支持 {settings.MAX_PHONES} 个；号码不够时我会自动循环使用。",
        reply_markup=cancel_kb(),
    )


@router.message(SplitStates.waiting_phones)
async def split_got_phones(message: Message, state: FSMContext, bot: Bot):
    text = None
    if message.text:
        text = message.text
    elif message.document and (message.document.file_name or "").lower().endswith((".txt", ".text")):
        data = await state.get_data()
        tmp = os.path.join(data["workdir"], "phones.txt")
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, destination=tmp)
        text = decode_bytes(open(tmp, "rb").read())
    else:
        await message.answer("请发送手机号文本（一行一个）或 TXT 文件。")
        return

    phones = parse_numbers(text)
    if not phones:
        await message.answer("未识别到手机号，请重新发送。")
        return

    truncated = False
    if len(phones) > settings.MAX_PHONES:
        phones = phones[: settings.MAX_PHONES]
        truncated = True

    data = await state.get_data()
    per_file = data.get("per_file", 0)
    paths: list[tuple[str, str]] = data.get("chunk_paths", [])

    if per_file <= 0 or not paths:
        await message.answer("⚠️ 会话异常，请重新开始文件切割。")
        await cleanup_state(state)
        return

    contents = [decode_bytes(read_file(p)) for p, _ in paths]
    result = insert_phones_per_file(contents, phones, per_file)
    if result is None:
        await message.answer("⚠️ 未识别到手机号，请重新发送。")
        return
    new_contents, report = result

    for (p, _), content in zip(paths, new_contents):
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    if report:
        report_path = os.path.join(data["workdir"], "插入报告.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report) + f"\n\n共插入 {len(report)} 个手机号。")

    await message.answer(
        f"✅ 已插入 <b>{len(report)}</b> 个手机号"
        + ("（超出上限部分已忽略）" if truncated else "")
        + "，开始发送文件："
    )
    await send_files_in_batches(message, paths)
    if report:
        await message.answer_document(
            FSInputFile(report_path, filename="插入报告.txt"),
            caption="📋 插入报告：每个号码插入的文件与行号",
        )
    await maybe_drop(message, "文件切割")
    await message.answer("✅ 全部完成！", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)


async def finish_split(message: Message, state: FSMContext):
    data = await state.get_data()
    paths: list[tuple[str, str]] = data.get("chunk_paths", [])
    if not paths:
        await message.answer("⚠️ 未找到切割文件，请重新开始。")
        await cleanup_state(state)
        return
    await message.answer(f"✅ 共 <b>{len(paths)}</b> 份，开始发送：")
    await send_files_in_batches(message, paths)
    await maybe_drop(message, "文件切割")
    await message.answer("✅ 全部完成！", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
