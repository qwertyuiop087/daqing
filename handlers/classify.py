"""号码分类：按运营商 / 按省份 / 按城市批量分组。"""
import asyncio
import html
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaDocument, Message

from config import settings
from states import ClassifyStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.drops import maybe_drop
from utils.keyboards import (
    cancel_kb,
    classify_mode_kb,
    classify_skip_filename_kb,
    menu_kb,
)
from utils.phone_db import (
    classify_phones,
    classify_with_stats,
    extract_phones,
)

router = Router(name="classify")


@router.callback_query(F.data == "menu:classify")
async def menu_classify(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(ClassifyStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "📊 <b>号码分类</b>\n\n"
        "请发送号码列表，<b>一行一个</b>（可直接发文本消息，或上传 TXT 文件）。\n\n"
        "支持三种导出方式：\n"
        "• 📡 按运营商分（移动 / 联通 / 电信 / 虚拟号）\n"
        "• 🗺️ 按省份分（每个省一个文件）\n"
        "• 🏙️ 按城市分（每个市一个文件）\n\n"
        f"单次最多处理 <b>{settings.MAX_PHONES}</b> 个号码。",
        reply_markup=cancel_kb(),
    )


@router.message(ClassifyStates.waiting_file)
async def classify_got(message: Message, state: FSMContext, bot: Bot):
    text = None
    if message.text:
        text = message.text
    elif message.document and (message.document.file_name or "").lower().endswith((".txt", ".text")):
        # 检查文件大小（Telegram Bot API 下载上限 20MB）
        if message.document.file_size and message.document.file_size > settings.MAX_FILE_SIZE:
            await message.answer(
                f"⚠️ 文件太大了！TXT 文件最大支持 {settings.MAX_FILE_SIZE // 1024 // 1024}MB。\n\n"
                f"当前文件：约 {message.document.file_size // 1024 // 1024}MB\n\n"
                f"💡 解决办法：\n"
                f"• 把大文件拆成多个小文件，分批上传\n"
                f"• 或者直接把号码粘贴成文本消息发过来"
            )
            return
        data = await state.get_data()
        tmp = os.path.join(data["workdir"], "input.txt")
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, destination=tmp)
        from services.file_ops import decode_bytes, read_file
        text = decode_bytes(read_file(tmp))
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

    await message.answer(f"⏳ 正在分析 {len(phones)} 个号码…")

    # 批量统计
    stats = classify_with_stats(phones)
    carrier_counts = stats["carrier_counts"]
    top_provinces = stats["top_provinces"]

    # 缓存号码到 state，等用户选了分组方式再生成文件
    await state.update_data(phones=phones)

    # 生成统计文本
    lines = [
        f"✅ <b>号码分析完成</b>",
        f"",
        f"📱 有效号码：<b>{stats['total']}</b> 个",
        f"",
        f"📡 运营商分布：",
        f"　• 中国移动：{carrier_counts['中国移动']} 个",
        f"　• 中国联通：{carrier_counts['中国联通']} 个",
        f"　• 中国电信：{carrier_counts['中国电信']} 个",
        f"　• 虚拟号：{carrier_counts['虚拟号']} 个",
    ]

    if top_provinces:
        lines.append(f"")
        lines.append(f"🗺️ 省份分布（Top 5）：")
        for prov, count in top_provinces[:5]:
            lines.append(f"　• {prov}：{count} 个")
        total_prov = len(stats["province_counts"])
        lines.append(f"　…共覆盖 {total_prov} 个省级地区")

    lines.append(f"")
    lines.append(f"请选择导出方式：")

    await state.set_state(ClassifyStates.waiting_choice)
    await message.answer("\n".join(lines), reply_markup=classify_mode_kb())


@router.callback_query(ClassifyStates.waiting_choice, F.data.startswith("classify:mode:"))
async def classify_mode_selected(cq: CallbackQuery, state: FSMContext):
    mode = cq.data.split(":")[-1]  # carrier / province / city
    data = await state.get_data()
    phones = data.get("phones", [])

    if not phones:
        await cq.answer("号码数据已过期，请重新发送。", show_alert=True)
        await cleanup_state(state)
        return

    mode_names = {
        "carrier": "📡 按运营商",
        "province": "🗺️ 按省份",
        "city": "🏙️ 按城市",
    }
    await cq.answer()
    mode_label = mode_names.get(mode, '')

    # 缓存分组方式
    await state.update_data(mode=mode, mode_label=mode_label)

    # 问要不要自定义文件名前缀
    await state.set_state(ClassifyStates.waiting_filename)
    await cq.message.answer(
        f"✅ 已选择：{mode_label}\n\n"
        f"现在请输入<b>文件名前缀</b>（可选）：\n"
        f"• 比如输入「北京」，生成的文件名就是：北京_中国移动_1234个.txt\n"
        f"• 不想要前缀？直接点下面「跳过」按钮。",
        reply_markup=classify_skip_filename_kb(),
    )


@router.callback_query(ClassifyStates.waiting_filename, F.data == "classify:filename:skip")
async def classify_filename_skip(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _do_export(cq.message, state, prefix="")


@router.message(ClassifyStates.waiting_filename, F.text)
async def classify_filename_input(message: Message, state: FSMContext):
    prefix = (message.text or "").strip()
    # 简单清理：去掉非法文件名字符
    prefix = prefix.replace("/", "_").replace("\\", "_").replace(":", "_")
    await _do_export(message, state, prefix=prefix)


async def _do_export(message: Message, state: FSMContext, prefix: str):
    """实际执行导出。"""
    data = await state.get_data()
    phones = data.get("phones", [])
    mode = data.get("mode", "carrier")
    mode_label = data.get("mode_label", "")
    out_dir = data["workdir"]

    if not phones:
        await message.answer("号码数据已过期，请重新发送。")
        await cleanup_state(state)
        return

    # 批量分组
    groups = classify_phones(phones, group_by=mode)

    # 过滤空组，按数量降序
    sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))
    non_empty = [(k, v) for k, v in sorted_groups if v]

    if not non_empty:
        await message.answer("没有可导出的号码。")
        await cleanup_state(state)
        return

    total_groups = len(non_empty)

    # 先发一条进度消息
    progress_msg = await message.answer(
        f"⏳ <b>{mode_label}导出中…</b>\n\n"
        f"文件总数：{total_groups} 个\n"
        f"已生成：0 / {total_groups}"
    )

    # 第一步：先把所有分组文件生成好
    out_files = []
    for group_name, num_list in non_empty:
        safe_name = group_name.replace("/", "_").replace("\\", "_").replace(":", "_")
        if prefix:
            filename = f"{prefix}_{safe_name}_{len(num_list)}个.txt"
        else:
            filename = f"{safe_name}_{len(num_list)}个.txt"
        out_path = os.path.join(out_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(num_list))
        out_files.append((filename, out_path))

    # 第二步：每10个文件打包成一组发送（Telegram 媒体组上限10个）
    BATCH_SIZE = 10
    BATCH_INTERVAL = 2
    sent_count = 0
    total_files = len(out_files)

    for batch_start in range(0, total_files, BATCH_SIZE):
        batch = out_files[batch_start : batch_start + BATCH_SIZE]

        media_group = []
        for filename, out_path in batch:
            media_group.append(
                InputMediaDocument(media=FSInputFile(out_path, filename=filename))
            )

        await message.answer_media_group(media_group)
        sent_count += len(batch)

        pct = int(sent_count / total_files * 100)
        bar_filled = int(pct / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        try:
            await progress_msg.edit_text(
                f"⏳ <b>{mode_label}导出中…</b>\n\n"
                f"文件总数：{total_files} 个\n"
                f"已发送：{sent_count} / {total_files}\n"
                f"{bar} {pct}%"
            )
        except Exception:
            pass

        if sent_count < total_files:
            await asyncio.sleep(BATCH_INTERVAL)

    # 最终结果
    total_phones = len(phones)
    bar = "█" * 20
    prefix_note = f"（前缀：{html.escape(prefix)}）" if prefix else ""
    await progress_msg.edit_text(
        f"✅ <b>{mode_label}导出完成</b>{prefix_note}\n\n"
        f"文件：<b>{sent_count}</b> 个\n"
        f"号码总数：<b>{total_phones}</b> 个\n"
        f"{bar} 100%"
    )
    await maybe_drop(message, "号码分类")
    await cleanup_state(state)
