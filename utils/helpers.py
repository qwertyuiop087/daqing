"""共享辅助函数：临时目录、清理、会员门槛、批量发文件。"""
import asyncio
import os
import shutil
import tempfile

from aiogram import exceptions
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaDocument, Message

from config import settings
from database import SessionLocal, User, get_or_create_user, is_member
from utils.keyboards import buy_prompt_kb, menu_kb


def get_workdir(tg_id: int) -> str:
    """为单个用户操作创建独立临时目录。"""
    return tempfile.mkdtemp(prefix=f"bot_{tg_id}_")


async def cleanup_state(state: FSMContext) -> None:
    """清除状态并删除该操作产生的临时文件。"""
    data = await state.get_data()
    workdir = data.get("workdir")
    if workdir and os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    await state.clear()


async def reply_event(event, text: str, reply_markup=None):
    """统一回复 Message / CallbackQuery。"""
    if isinstance(event, CallbackQuery):
        await event.answer()
        return await event.message.answer(text, reply_markup=reply_markup)
    return await event.answer(text, reply_markup=reply_markup)


async def ensure_member(event) -> User | None:
    """会员门槛：未开通/已到期则提示并返回 None。"""
    with SessionLocal() as session:
        user = get_or_create_user(
            session,
            event.from_user.id,
            event.from_user.username or "",
            event.from_user.first_name or "",
        )
        if is_member(user):
            return user

    text = (
        "❌ <b>未开通会员</b>\n\n"
        "您还未开通会员或会员已到期。\n"
        "购买会员后即可使用全部功能（已有卡密可点下方兑换）。"
    )
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(text, reply_markup=buy_prompt_kb())
    else:
        await event.answer(text, reply_markup=buy_prompt_kb())
    return None


async def _send_group_retry(message: Message, group: list) -> None:
    """发送媒体组，遇到限流自动等待重试。"""
    for _ in range(4):
        try:
            await message.answer_media_group(group)
            return
        except exceptions.TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    # 多次重试仍失败，向上抛出让调用方退化为逐个发送
    raise RuntimeError("media group send failed")


async def _send_document_retry(message: Message, path: str, name: str) -> None:
    """发送单个文件，遇到限流自动等待重试。"""
    for _ in range(4):
        try:
            await message.answer_document(FSInputFile(path, filename=name))
            return
        except exceptions.TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    raise RuntimeError("document send failed")


async def send_files_in_batches(
    message: Message, files: list[tuple[str, str]], batch: int | None = None
) -> None:
    """批量发送文件：files 为 [(本地路径, 展示文件名)]。

    每 batch 个（默认 10，Telegram 媒体组上限）一起发送，
    批次之间停顿规避限流；遇到「Too Many Requests」自动等待重试。
    媒体组整体失败时退化为逐个发送，避免整批丢失。
    """
    if not files:
        return
    batch = batch or settings.MAX_BATCH_FILES
    if len(files) == 1:
        path, name = files[0]
        try:
            await _send_document_retry(message, path, name)
        except Exception:
            pass
        return
    for i in range(0, len(files), batch):
        group = [
            InputMediaDocument(media=FSInputFile(path, filename=name))
            for path, name in files[i:i + batch]
        ]
        try:
            await _send_group_retry(message, group)
        except Exception:
            # 媒体组整体失败（如个别文件异常）：退化为逐个发送
            for path, name in files[i:i + batch]:
                try:
                    await _send_document_retry(message, path, name)
                except Exception:
                    pass
        # 批次之间停顿，规避 Telegram 限流（关键修复：超过 10 份时第二组不会再被限流打断）
        if i + batch < len(files):
            await asyncio.sleep(2.5)


async def send_menu(message: Message, text: str) -> None:
    """发送主菜单。"""
    await message.answer(text, reply_markup=menu_kb(message.from_user.id))
