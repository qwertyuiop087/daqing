"""空号检测：上传号码 → 批量检测 → 分成活跃号/空号/沉默号/风险号导出。"""
import asyncio
import os

import requests
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from database import SessionLocal, get_config, set_config
from services.file_ops import decode_bytes
from states import PhoneCheckStates
from utils.helpers import cleanup_state, ensure_member, get_workdir, send_files_in_batches
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb
from utils.phone_db import extract_phones

router = Router(name="phone_check")


@router.callback_query(F.data == "menu:phonecheck")
async def menu_phone_check(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return

    # 检查小号是否登录
    with SessionLocal() as session:
        alt_user_id = get_config(session, "alt_user_id", "")

    if not alt_user_id:
        await cq.answer()
        await cq.message.edit_text(
            "🔍 <b>空号检测</b>\n\n"
            "⚠️ 小号未登录，无法使用空号检测。\n\n"
            "请联系管理员在管理面板登录小号。",
            reply_markup=menu_kb(cq.from_user.id),
        )
        return

    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(PhoneCheckStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🔍 <b>空号检测</b>\n\n"
        "请发送号码列表，<b>一行一个</b>（可直接发文本消息，或上传 TXT 文件）。\n\n"
        "系统会自动通过小号提交到空号检测系统，检测完成后自动把结果发给你。\n\n"
        f"单次最多处理 <b>{settings.MAX_PHONES}</b> 个号码。",
        reply_markup=cancel_kb(),
    )


@router.message(PhoneCheckStates.waiting_file)
async def phone_check_got(message: Message, state: FSMContext, bot: Bot):
    text = None
    if message.text:
        text = message.text
    elif message.document and (message.document.file_name or "").lower().endswith((".txt", ".text")):
        if message.document.file_size and message.document.file_size > settings.MAX_FILE_SIZE:
            await message.answer(
                f"⚠️ 文件太大了！TXT 文件最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。"
            )
            return
        data = await state.get_data()
        tmp = os.path.join(data["workdir"], "input.txt")
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, destination=tmp)
        text = decode_bytes(open(tmp, "rb").read())
    else:
        await message.answer("请发送号码文本（一行一个）或 TXT 文件。")
        return

    phones = extract_phones(text)
    if not phones:
        await message.answer("未识别到有效手机号，请重新发送。")
        return

    if len(phones) > settings.MAX_PHONES:
        await message.answer(
            f"号码数量过多（{len(phones)} 个），单次最多处理 {settings.MAX_PHONES} 个，请拆分后重试。"
        )
        return

    # 生成临时文件
    import random
    import string
    random_code = ''.join(random.choices(string.ascii_letters + string.digits, k=7))
    data = await state.get_data()
    out_path = os.path.join(data["workdir"], f"{random_code}_号码检测.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(phones))

    # 存映射：随机码 → 用户ID
    with SessionLocal() as session:
        set_config(session, f"phone_check_code:{random_code}", str(message.from_user.id))

    # 查小号ID
    with SessionLocal() as session:
        alt_user_id = get_config(session, "alt_user_id", "")

    if not alt_user_id:
        await message.answer("⚠️ 小号未登录，请联系管理员在管理面板登录小号。")
        await cleanup_state(state)
        return

    # 发给小号，caption 标记是 phone_check
    progress_msg = await message.answer(f"⏳ 正在提交 {len(phones)} 个号码到空号检测系统…")
    try:
        await bot.send_document(
            chat_id=int(alt_user_id),
            document=FSInputFile(out_path),
            caption="phone_check",
        )
    except Exception as e:
        await progress_msg.edit_text(f"⚠️ 提交失败：{e}")
        await cleanup_state(state)
        return

    await progress_msg.edit_text(
        f"✅ 已提交 {len(phones)} 个号码到空号检测系统，\n"
        "检测完成后会自动把结果发给你，请稍等。"
    )
    await maybe_drop(message, "空号检测")
    await cleanup_state(state)
