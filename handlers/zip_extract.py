"""解压 ZIP 提取 TXT（支持一次上传多个 ZIP）。"""
import os
import time
import zipfile

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from services.file_ops import extract_txt_from_zip, read_file, sanitize_filename
from states import ZipStates
from utils.drops import maybe_drop
from utils.helpers import (
    cleanup_state,
    ensure_member,
    get_workdir,
    send_files_in_batches,
    user_lock,
)
from utils.keyboards import finish_upload_kb, menu_kb

router = Router(name="zip_extract")


def _unique_name(name: str, used: set) -> str:
    """同一批里重名的 TXT 自动加序号，避免互相覆盖。"""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while f"{stem}_{i}{ext}" in used:
        i += 1
    final = f"{stem}_{i}{ext}"
    used.add(final)
    return final


@router.callback_query(F.data == "menu:zip")
async def menu_zip(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), zips=[], total_txt=0)
    await state.set_state(ZipStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🗜️ <b>解压Zip提取TXT（支持多文件）</b>\n\n"
        f"把 ZIP 一次性发过来（单个 ≤{settings.MAX_ZIP_SIZE // 1024 // 1024}MB），"
        f"自动提取里面的 TXT（每个包最多 {settings.MAX_ZIP_TXT_FILES} 个）。\n\n"
        "全部发完点「✅ 完成上传」，我会把提取到的 TXT 一起发给你。",
        reply_markup=finish_upload_kb("zip"),
    )


@router.message(ZipStates.waiting_file, F.document)
async def zip_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = sanitize_filename(doc.file_name or "upload.zip")
    if not name.lower().endswith(".zip"):
        await message.answer("⚠️ 请发送 <b>.zip</b> 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_ZIP_SIZE:
        await message.answer(
            f"⚠️ <code>{name}</code> 过大，单个 ZIP 最大 {settings.MAX_ZIP_SIZE // 1024 // 1024}MB。"
        )
        return

    data = await state.get_data()
    zips_now = list(data.get("zips") or [])
    if len(zips_now) >= settings.MAX_UPLOAD_FILES:
        await message.answer(
            f"⚠️ 最多 {settings.MAX_UPLOAD_FILES} 个文件，请先点「✅ 完成上传」。",
            reply_markup=finish_upload_kb("zip"),
        )
        return

    workdir = data.get("workdir") or get_workdir(message.from_user.id)
    # 序号前缀：多个 zip 之间不会互相覆盖（用时间戳兜底并发）
    zip_path = os.path.join(workdir, f"zip_{len(zips_now) + 1}_{int(time.time() * 1000)}_{name}")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=zip_path)
        raw = read_file(zip_path)
    except Exception as e:
        await message.answer(f"⚠️ <code>{name}</code> 下载失败：{str(e)[:100]}")
        return

    try:
        extracted = extract_txt_from_zip(
            raw,
            max_txt=settings.MAX_ZIP_TXT_FILES,
            max_size=settings.MAX_ZIP_SIZE,
            max_total_size=settings.MAX_ZIP_SIZE * 2,
        )
    except RuntimeError as e:
        # 命中安全限制（压缩包炸弹 / 条目过多 / 解压总量超限）
        await message.answer(
            f"⚠️ <code>{name}</code> 已被安全拦截：{str(e)[:120]}",
            reply_markup=finish_upload_kb("zip"),
        )
        return
    except (zipfile.BadZipFile, NotImplementedError):
        await message.answer(
            f"⚠️ <code>{name}</code> 无法解析（可能损坏或被加密），已跳过。",
            reply_markup=finish_upload_kb("zip"),
        )
        return

    if not extracted:
        await message.answer(
            f"⚠️ <code>{name}</code> 里没有 TXT 文件，已跳过。",
            reply_markup=finish_upload_kb("zip"),
        )
        return

    # 每个 zip 单独一个子目录，避免不同包内同名 txt 互相覆盖
    out_dir = os.path.join(
        workdir, "out", f"z{len(zips_now) + 1}_{int(time.time() * 1000)}"
    )
    os.makedirs(out_dir, exist_ok=True)
    files: list[dict] = []
    used: set = set()
    for inner_name, text in extracted:
        safe = _unique_name(sanitize_filename(inner_name or "文件.txt"), used)
        path = os.path.join(out_dir, safe)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        files.append({"name": safe, "path": path, "lines": len(text.splitlines())})

    async with user_lock(message.from_user.id):
        data = await state.get_data()
        zips = list(data.get("zips") or [])
        zips.append({"name": name, "files": files})
        total_txt = sum(len(z["files"]) for z in zips)
        await state.update_data(zips=zips, total_txt=total_txt, workdir=workdir)
        idx, total_zips = len(zips), total_txt

    await message.answer(
        f"✅ 已解压第 <b>{idx}</b> 个压缩包：<code>{name}</code>\n"
        f"　本包提取：<b>{len(files)}</b> 个 TXT\n"
        f"　累计：{idx} 个 ZIP / <b>{total_zips}</b> 个 TXT\n\n"
        "可继续发送，全部发完点「✅ 完成上传」。",
        reply_markup=finish_upload_kb("zip"),
    )


@router.callback_query(ZipStates.waiting_file, F.data == "zip:done")
async def zip_done_cb(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _do_send(cq.message, state)


@router.message(ZipStates.waiting_file, F.text)
async def zip_done_text(message: Message, state: FSMContext):
    if (message.text or "").strip() in ("完成", "done", "ok", "OK", "好"):
        await _do_send(message, state)


async def _do_send(event, state: FSMContext):
    data = await state.get_data()
    zips = list(data.get("zips") or [])
    if not zips:
        await event.answer(
            "⚠️ 还没收到 ZIP 文件，请先发送。",
            reply_markup=finish_upload_kb("zip"),
        )
        return

    out: list[tuple[str, str]] = []
    for z in zips:
        for f in z["files"]:
            out.append((f["path"], f["name"]))

    total_lines = sum(f["lines"] for z in zips for f in z["files"])
    await event.answer(
        f"✅ 共 <b>{len(zips)}</b> 个压缩包，提取到 <b>{len(out)}</b> 个 TXT"
        f"（共 {total_lines:,} 行），开始发送："
    )
    await send_files_in_batches(event, out)
    await maybe_drop(event, "解压Zip提取TXT")
    await event.answer("✅ 完成！", reply_markup=menu_kb(event.from_user.id))
    await cleanup_state(state)
