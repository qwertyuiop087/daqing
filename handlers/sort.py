"""催收分包：批量上传 TXT → 识别行内字段顺序 → 按用户指定顺序批量重排，自动清理多余字段。"""
import html
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from services.file_ops import (
    analyze_record_order,
    decode_bytes,
    detect_delimiter,
    detect_field_type,
    parse_desired_order,
    read_file,
    reorder_file_lines,
    sanitize_filename,
    split_record,
)
from states import SortStates
from utils.helpers import cleanup_state, ensure_member, get_workdir, send_files_in_batches
from utils.drops import maybe_drop
from utils.keyboards import cancel_kb, finish_upload_kb, menu_kb

router = Router(name="sort")

_SUPPORTED_HINT = "支持字段：<b>手机号、银行卡、身份证、金额、时间、名字</b>（用空格或顿号分隔）"


@router.callback_query(F.data == "menu:sort")
async def menu_sort(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id), files=[])
    await state.set_state(SortStates.waiting_files)
    await cq.answer()
    await cq.message.edit_text(
        "📋 <b>催收分包（批量字段调序 + 清理多余字段）</b>\n\n"
        "上传 <b>多个 TXT</b> 文件，每个文件里<b>每一行是一条记录</b>，"
        "一行中包含手机号、银行卡、身份证、金额、时间、名字等字段。\n\n"
        "我会自动识别每行里各字段的顺序，按你指定的顺序<b>批量调换位置</b>，"
        "同时自动清理多余的日期、银行名、地址等杂项字段。\n\n"
        f"请发送 TXT 文件（可多选，最多 {settings.MAX_UPLOAD_FILES} 个），"
        "上传完成后点击「✅ 完成上传」。",
        reply_markup=finish_upload_kb("sort"),
    )


@router.message(SortStates.waiting_files, F.document)
async def sort_got_file(message: Message, state: FSMContext, bot: Bot):
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
    files.append((name, path))
    await state.update_data(files=files)
    await message.answer(f"✅ 已接收 <b>{len(files)}</b> 个文件，可继续发送，或点击「✅ 完成上传」。")


@router.callback_query(SortStates.waiting_files, F.data == "sort:done")
async def sort_done(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    files: list[tuple[str, str]] = data.get("files", [])
    if not files:
        await cq.message.edit_text("⚠️ 请先上传文件。", reply_markup=finish_upload_kb("sort"))
        return

    # 汇总所有文件的行，用于检测分隔符与字段顺序
    all_lines: list[str] = []
    for _, path in files:
        all_lines.extend(
            ln for ln in decode_bytes(read_file(path)).splitlines() if ln.strip()
        )
    if not all_lines:
        await cq.message.edit_text("⚠️ 文件内容均为空。", reply_markup=finish_upload_kb("sort"))
        return

    delim = detect_delimiter(all_lines)
    delim_name = {"\t": "制表符", "|": "竖线 |", " ": "空格", ",": "逗号 ,", "，": "中文逗号 ，", ";": "分号 ;", "；": "中文分号 ；"}.get(delim, "空格")

    # 不规则6位数字当银行卡的规则永远开启，不影响完整银行卡
    treat_6digit_as_bank = True
    await state.update_data(treat_6digit_as_bank=treat_6digit_as_bank)
    _, counts, variants = analyze_record_order(all_lines, delim, treat_6digit_as_bank)

    head = f"✅ 共 <b>{len(files)}</b> 个文件，已识别：\n\n"
    head += f"🔧 字段分隔符：<b>{delim_name}</b>\n\n"
    if variants and len(variants) == 1:
        head += "📋 <b>当前字段顺序：</b>" + " → ".join(variants[0]) + "\n\n"
    elif variants:
        head += f"📋 行内顺序<b>不完全一致</b>（检测到 {len(variants)} 种，会按每条记录自身识别后统一调序）：\n"
        for v in variants[:6]:
            head += "· " + " → ".join(v) + "\n"
        head += "\n"
    else:
        head += "⚠️ 未识别到已知字段（手机号/银行卡/身份证/金额/时间/名字），请检查文件格式。\n\n"
    head += "<b>各类字段数量：</b>\n"
    for k in ("手机号", "银行卡", "身份证", "金额", "时间", "名字"):
        head += f"· {k}：<b>{counts.get(k, 0)}</b>\n"

    # 统计多余字段（"其他"类型）的数量，包括带特殊符号的
    other_count = sum(counts.values()) - sum(counts.get(k, 0) for k in ("手机号", "银行卡", "身份证", "金额", "时间", "名字"))

    # 收集多余字段的样本，给用户看例子
    other_samples = []
    for ln in all_lines[:100]:  # 只看前100行，避免太慢
        tokens = split_record(ln, delim)
        for t in tokens:
            if detect_field_type(t) == "其他" and t.strip() and t.strip() not in other_samples:
                other_samples.append(t.strip())
                if len(other_samples) >= 5:
                    break
        if len(other_samples) >= 5:
            break

    if other_count > 0:
        head += f"\n⚠️ <b>检测到 {other_count} 个多余字段</b>，例如：\n"
        for s in other_samples:
            head += f"  · <code>{html.escape(s[:30])}</code>\n"
        head += "这些一般是日期/银行名/地址/@符号/备注等无用内容。\n"

    # 加按钮：是否删除多余字段
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑️ 删除多余字段", callback_data="sort:del_other:yes")
    kb.button(text="✅ 保留所有字段", callback_data="sort:del_other:no")
    kb.button(text="❌ 取消", callback_data="cancel")
    kb.adjust(2, 1)

    await cq.message.edit_text(
        head + "\n请发送你想要的字段顺序，例如：\n<code>手机号 银行卡 身份证 金额 时间</code>\n\n" + _SUPPORTED_HINT,
        reply_markup=kb.as_markup(),
    )
    await state.update_data(delim=delim, counts=counts, variants=variants, del_other=False)
    await state.set_state(SortStates.waiting_order)


@router.callback_query(SortStates.waiting_order, F.data.startswith("sort:del_other:"))
async def sort_del_other(cq: CallbackQuery, state: FSMContext):
    choice = cq.data.split(":")[2]
    await cq.answer()
    data = await state.get_data()
    del_other = (choice == "yes")
    await state.update_data(del_other=del_other)

    action = "已选择删除多余字段" if del_other else "已选择保留所有字段"
    await cq.message.answer(f"✅ {action}。\n\n请发送你想要的字段顺序，例如：\n<code>手机号 银行卡 身份证 金额 时间</code>")
    await state.set_state(SortStates.waiting_order)


@router.message(SortStates.waiting_order, F.text)
async def sort_got_order(message: Message, state: FSMContext):
    desired, bad = parse_desired_order(message.text or "")
    if bad is not None:
        await message.answer(
            f"❌ 无法识别字段名「<b>{html.escape(bad)}</b>」。\n\n" + _SUPPORTED_HINT + "，请重新发送。"
        )
        return
    if not desired:
        await message.answer("请输入至少一个字段名。")
        return

    data = await state.get_data()
    delim = data["delim"]
    files: list[tuple[str, str]] = data["files"]
    variants = data.get("variants", [])
    del_other = data.get("del_other", False)
    treat_6digit_as_bank = data.get("treat_6digit_as_bank", False)

    outputs: list[tuple[str, str]] = []
    out_dir = os.path.join(data["workdir"], "out")
    os.makedirs(out_dir, exist_ok=True)
    for name, path in files:
        lines = [ln for ln in decode_bytes(read_file(path)).splitlines() if ln.strip()]
        new_lines = reorder_file_lines(lines, delim, desired, del_other, treat_6digit_as_bank)
        # 输出保持与原文件相同的文件名
        out_path = os.path.join(out_dir, name)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))
        outputs.append((out_path, name))

    old_str = " → ".join(variants[0]) if len(variants) == 1 else (
        " / ".join(" → ".join(v) for v in variants[:3]) if variants else "（未识别）"
    )
    new_str = " → ".join(desired)
    await message.answer(
        f"✅ <b>批量调序完成</b>（{len(outputs)} 个文件）\n\n"
        f"原顺序：{old_str}\n"
        f"新顺序：{new_str}\n"
        f"未提到的字段会按原顺序跟在后面，无法识别的字段放行尾。\n\n开始发送："
    )
    await send_files_in_batches(message, outputs)
    await maybe_drop(message, "催收分包")
    await message.answer("✅ 完成！", reply_markup=menu_kb(message.from_user.id))
    await cleanup_state(state)
