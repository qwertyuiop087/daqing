"""xlsx 转 txt：批量上传 Excel → 内容原样转成制表符分隔的 TXT。"""
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from services.file_ops import read_file, sanitize_filename, xlsx_to_lines
from states import XlsxStates
from utils.helpers import cleanup_state, ensure_member, get_workdir, send_files_in_batches
from utils.drops import maybe_drop
from utils.keyboards import finish_upload_kb, menu_kb

router = Router(name="xlsx_convert")


@router.callback_query(F.data == "menu:xlsx")
async def menu_xlsx(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[])
    await state.set_state(XlsxStates.waiting_files)
    await cq.answer()
    await cq.message.edit_text(
        "📊 <b>Excel 转 TXT</b>\n\n"
        "上传 <b>XLSX / XLS</b> 文件，我会把每个表格按行转成 TXT：\n"
        "· 每行是一条记录，各列用<b>制表符</b>分隔\n"
        "· 单元格内容<b>原样保留</b>：数字不变成科学计数法、不四舍五入，"
        "文本、日期、空单元格都不动\n"
        "· 只转第一个工作表，输出 TXT 与原文件同名\n\n"
        f"请发送 Excel 文件（可多选，最多 {settings.MAX_UPLOAD_FILES} 个），"
        "上传完成后点击「✅ 完成上传」。",
        reply_markup=finish_upload_kb("xlsx"),
    )


@router.message(XlsxStates.waiting_files, F.document)
async def xlsx_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    filename = (doc.file_name or "").lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        await message.answer("⚠️ 只接受 XLSX / XLS 文件。")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(f"⚠️ 单个文件过大，最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。")
        return

    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    if len(files) >= settings.MAX_UPLOAD_FILES:
        await message.answer(f"⚠️ 最多上传 {settings.MAX_UPLOAD_FILES} 个文件，请先点击「✅ 完成上传」。")
        return

    name = sanitize_filename(doc.file_name or f"file_{len(files) + 1}.xlsx")
    path = os.path.join(data["workdir"], name)
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=path)
    files.append((name, path))
    await state.update_data(files=files)
    await message.answer(f"✅ 已接收 <b>{len(files)}</b> 个文件，可继续发送，或点击「✅ 完成上传」。")


@router.callback_query(XlsxStates.waiting_files, F.data == "xlsx:done")
async def xlsx_done(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    if not files:
        await cq.message.edit_text("⚠️ 请先上传文件。", reply_markup=finish_upload_kb("xlsx"))
        return

    await cq.message.edit_text("⏳ 正在转换…")
    outputs: list[tuple[str, str]] = []
    errors: list[str] = []
    out_dir = os.path.join(data["workdir"], "out")
    os.makedirs(out_dir, exist_ok=True)
    for name, path in files:
        try:
            lines = xlsx_to_lines(read_file(path))
        except Exception as e:  # noqa: BLE001 - 单个文件失败不影响其他文件
            err_str = str(e)
            if "Bad magic number" in err_str or "zip" in err_str.lower():
                friendly = "文件损坏，请用Excel打开重新保存"
            else:
                friendly = err_str[:100]
            errors.append(f"❌ {name}：{friendly}")
            continue
        txt_name = os.path.splitext(name)[0] + ".txt"
        out_path = os.path.join(out_dir, txt_name)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        outputs.append((out_path, txt_name))

    if outputs:
        await cq.message.answer(
            f"✅ <b>Excel 转 TXT 完成</b>（{len(outputs)} 个文件）\n"
            "内容保持原样，字段顺序不变。开始发送："
        )
        await send_files_in_batches(cq.message, outputs)
        await maybe_drop(cq.message, "Excel转txt")
    if errors:
        await cq.message.answer("\n".join(errors))
    await cq.message.answer("✅ 完成！", reply_markup=menu_kb(cq.from_user.id))
    await cleanup_state(state)
