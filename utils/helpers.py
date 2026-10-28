"""共享辅助函数：临时目录、清理、会员门槛、批量发文件。"""
import os
import shutil
import tempfile

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


async def send_files_in_batches(
    message: Message, files: list[tuple[str, str]], batch: int | None = None
) -> None:
    """批量发送文件：files 为 [(本地路径, 展示文件名)]。

    每 batch 个（默认 10，Telegram 媒体组上限）一起发送，
    不足 batch 个时全部一起发送。
    """
    if not files:
        return
    batch = batch or settings.MAX_BATCH_FILES
    if len(files) == 1:
        path, name = files[0]
        await message.answer_document(FSInputFile(path, filename=name))
        return
    for i in range(0, len(files), batch):
        group = [
            InputMediaDocument(media=FSInputFile(path, filename=name))
            for path, name in files[i:i + batch]
        ]
        await message.answer_media_group(group)


async def send_menu(message: Message, text: str) -> None:
    """发送主菜单。"""
    await message.answer(text, reply_markup=menu_kb(message.from_user.id))
