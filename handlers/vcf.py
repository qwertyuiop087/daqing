"""号码列表转 VCF 通讯录文件。"""
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from services.file_ops import decode_bytes, numbers_to_vcf, parse_numbers
from states import VcfStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, menu_kb

router = Router(name="vcf")


@router.callback_query(F.data == "menu:vcf")
async def menu_vcf(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(VcfStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        f"📇 <b>号码转VCF</b>\n\n"
        f"请发送号码列表，<b>一行一个</b>（可直接发文本消息，或上传 TXT 文件）。\n\n"
        f"最多支持 {settings.MAX_PHONES} 个。",
        reply_markup=cancel_kb(),
    )


@router.message(VcfStates.waiting_file)
async def vcf_got(message: Message, state: FSMContext, bot: Bot):
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
        await message.answer("请发送号码文本（一行一个）或 TXT 文件。")
        return

    numbers = parse_numbers(text)
    if not numbers:
        await message.answer("未识别到号码，请重新发送。")
        return
    truncated = len(numbers) > settings.MAX_PHONES
    numbers = numbers[: settings.MAX_PHONES]

    vcf = numbers_to_vcf(numbers)
    data = await state.get_data()
    out_path = os.path.join(data["workdir"], "通讯录.vcf")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(vcf)

    await message.answer(f"✅ 共 <b>{len(numbers)}</b> 个号码" + ("（超出上限部分已忽略）" if truncated else "") + "，已生成 VCF：")
    await message.answer_document(FSInputFile(out_path, filename="通讯录.vcf"))
    await maybe_drop(message, "号码转VCF")
    await message.answer("✅ 完成！", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
