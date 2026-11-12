"""加区号：给 txt 里的手机号前面加 +86（支持一次上传多个文件）。"""
import asyncio
import os
import time
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from services.file_ops import decode_bytes, read_file, sanitize_filename
from states import AddAreaCodeStates
from utils.drops import maybe_drop
from utils.helpers import cleanup_state, ensure_member, get_workdir, user_lock
from utils.keyboards import cancel_kb, finish_upload_kb, menu_kb

router = Router(name="add_area_code")

_SUPPORTED = (".txt", ".text", ".csv")
_PHONE_RE = re.compile(r"(?<![\d+])(1[3-9]\d{9})(?![\d])")


def add_plus86(text: str) -> tuple[str, int]:
    """给每行里的手机号加 +86 前缀，已有 86/+86 的跳过。

    返回 (处理后的全文, 有改动的行数)。空行丢弃（与原行为一致）。
    """
    out_lines = []
    changed_lines = 0
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        pieces = []
        last = 0
        changed = False
        for m in _PHONE_RE.finditer(raw):
            phone = m.group(1)
            start = m.start(1)
            if start < last:
                continue
            prefix = raw[:start]
            if prefix.endswith("+86") or prefix.endswith("86") or prefix.endswith("86 "):
                continue
            pieces.append(raw[last:start])
            pieces.append(f"+86{phone}")
            last = start + len(phone)
            changed = True
        pieces.append(raw[last:])
        out_lines.append("".join(pieces))
        if changed:
            changed_lines += 1
    return "\n".join(out_lines), changed_lines


def _out_name(name: str, used: set) -> str:
    """输出文件名：原名 + _加区号.txt，重名自动加序号。"""
    base = os.path.splitext(name)[0] or "结果"
    candidate = f"{base}_加区号.txt"
    i = 2
    while candidate in used:
        candidate = f"{base}_加区号_{i}.txt"
        i += 1
    used.add(candidate)
    return candidate


def _status_text(files: list, total_lines: int) -> str:
    shown = "\n".join(
        f"· {name}（{lines:,} 行）" for name, _path, lines in files[:10]
    )
    if len(files) > 10:
        shown += f"\n… 还有 {len(files) - 10} 个文件"
    return (
        f"🇨🇳 已收到 <b>{len(files)}</b> 个文件，共 <b>{total_lines:,}</b> 行\n"
        f"{shown}\n\n"
        "继续发文件，全部发完点「✅ 完成上传」。\n"
        f"（单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB，最多 {settings.MAX_UPLOAD_FILES} 个）"
    )


@router.callback_query(F.data == "menu:addcode")
async def menu_add_code(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[], total_lines=0)
    await state.set_state(AddAreaCodeStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🇨🇳 <b>加区号（支持多文件）</b>\n\n"
        "把要处理的文件一次性发过来，自动给里面的手机号加 <b>+86</b>。\n"
        "可以一次发多个，全部发完点「✅ 完成上传」。\n\n"
        "支持格式：<b>.txt / .csv</b>\n"
        f"单个文件最大 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。",
        reply_markup=finish_upload_kb("addcode"),
    )


@router.message(AddAreaCodeStates.waiting_file, F.document)
async def add_code_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    fname = (doc.file_name or "").lower()

    if not fname.endswith(_SUPPORTED):
        await message.answer("⚠️ 只支持 txt / csv 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ 文件太大了！单个文件最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。"
        )
        return

    data = await state.get_data()
    files = list(data.get("files") or [])
    if len(files) >= settings.MAX_UPLOAD_FILES:
        await message.answer(
            f"⚠️ 最多 {settings.MAX_UPLOAD_FILES} 个文件，请先点「✅ 完成上传」。",
            reply_markup=finish_upload_kb("addcode"),
        )
        return

    workdir = data.get("workdir") or get_workdir(message.from_user.id)
    name = doc.file_name or f"file{len(files) + 1}.txt"
    safe = sanitize_filename(name)
    path = os.path.join(workdir, f"ac_{len(files) + 1}_{int(time.time() * 1000)}_{safe}")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=path)
        lines = len(decode_bytes(read_file(path)).splitlines())
    except Exception as e:
        await message.answer(f"⚠️ 文件接收失败：{str(e)[:100]}")
        return

    async with user_lock(message.from_user.id):
        data = await state.get_data()
        files = list(data.get("files") or [])
        files.append((name, path, lines))
        total_lines = sum(f[2] for f in files)
        await state.update_data(files=files, total_lines=total_lines, workdir=workdir)
        idx = len(files)

    await message.answer(
        f"✅ 已接收第 <b>{idx}</b> 个文件：<code>{name}</code>\n"
        f"　本文件：<b>{lines:,}</b> 行\n"
        f"　累计：{idx} 个文件 / <b>{total_lines:,}</b> 行\n\n"
        "可继续发送，全部发完点「✅ 完成上传」。",
        reply_markup=finish_upload_kb("addcode"),
    )


@router.callback_query(AddAreaCodeStates.waiting_file, F.data == "addcode:done")
async def add_code_done_cb(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    await _do_add_code(cq.message, state, bot)


@router.message(AddAreaCodeStates.waiting_file, F.text)
async def add_code_done_text(message: Message, state: FSMContext, bot: Bot):
    """发文字「完成」也能触发处理。"""
    if (message.text or "").strip() in ("完成", "done", "ok", "OK", "好"):
        await _do_add_code(message, state, bot)


async def _do_add_code(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    files = list(data.get("files") or [])
    workdir = data.get("workdir") or get_workdir(message.from_user.id)

    if not files:
        await message.answer(
            "⚠️ 还没收到文件，请先发送要处理的 txt / csv。",
            reply_markup=finish_upload_kb("addcode"),
        )
        return

    progress = await message.answer(f"⏳ 正在给 {len(files)} 个文件加区号…")

    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)

    used_names: set = set()
    sent = 0
    skipped = 0
    failed = 0
    total_lines = 0
    total_added = 0

    for name, path, _lines in files:
        try:
            text = decode_bytes(read_file(path))
        except Exception as e:
            failed += 1
            await message.answer(f"⚠️ <code>{name}</code> 读取失败：{str(e)[:80]}")
            continue

        new_text, added = add_plus86(text)
        had_lines = len(new_text.splitlines())
        total_lines += had_lines
        total_added += added

        if added == 0:
            skipped += 1
            await message.answer(
                f"⚠️ <code>{name}</code>：没找到需要加区号的手机号（可能已经是 +86），已跳过。"
            )
            continue

        out_name = _out_name(name, used_names)
        out_path = os.path.join(out_dir, out_name)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(new_text)
            await message.answer_document(
                FSInputFile(out_path, filename=out_name),
                caption=f"✅ {name}\n{had_lines:,} 行 · 加区号 {added:,} 个",
            )
            sent += 1
        except Exception as e:
            failed += 1
            await message.answer(f"⚠️ <code>{name}</code> 输出失败：{str(e)[:80]}")
        await asyncio.sleep(0.5)  # 连发多个文件，间隔一下避免限流

    summary = (
        f"✅ <b>加区号完成</b>\n\n"
        f"文件数：<b>{len(files)}</b> 个（成功 {sent} · 跳过 {skipped} · 失败 {failed}）\n"
        f"总行数：<b>{total_lines:,}</b> 行\n"
        f"加了区号：<b>{total_added:,}</b> 行"
    )
    try:
        await progress.edit_text(summary)
    except Exception:
        await message.answer(summary)

    if sent:
        await maybe_drop(message, "加区号")
    await message.answer("完成，结果已发送。", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
