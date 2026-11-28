"""号码拆包：发一份名单 + 手机号，找到号码在第几行，把名单切成前段/后段发回。

和「文件切割」互补：那边按行数平均切，这边按**内容**切 ——
客户手上有一份大包，想在某个人（手机号）那里断开，就要这个功能。

流程：
1. 点「号码拆包」→ 发名单（txt / md / csv / log / dat）
2. 发手机号（一行一个，一次最多 5 个，接着处理下一个）
3. 机器人告诉你号码在第几行、命中行内容、前段多少条、后段多少条
4. 选前段 / 后段 / 两段都要 → 选原包名或自定义名字 → 发文件
"""
import html
import os

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import settings
from services import phone_split
from services.file_ops import read_file, sanitize_filename
from states import CutByPhoneStates
from utils.helpers import cleanup_state, ensure_member, get_workdir
from utils.keyboards import cancel_kb, cut_name_kb, cut_seg_kb

router = Router(name="cutbyphone")

# 认这些后缀：都是纯文本名单，md 里的表格会被压成制表符
CUT_EXTS = (".txt", ".text", ".md", ".markdown", ".csv", ".log", ".dat")
# 一次最多处理几个号码（多了怕刷屏，剩下的让用户再发）
MAX_PHONES_PER_BATCH = 5
SEG_LABEL = {"before": "前段", "after": "后段", "both": "两段都要"}


def _limit_mb() -> int:
    return settings.MAX_FILE_SIZE // 1024 // 1024


def _load_rows(data: dict) -> tuple[list, str]:
    """从磁盘重新读一遍名单（不在 FSM 里存几十万行，省内存）。"""
    text, _enc = phone_split.decode_with_name(read_file(data["src_path"]))
    rows, nl = phone_split.rows_of(text)
    is_md = data.get("md") or str(data.get("name", "")).lower().endswith((".md", ".markdown"))
    rows, _table, _cleaned = phone_split.normalize(rows, bool(is_md))
    return rows, nl


def _safe_hit(row: str) -> str:
    row = (row or "").strip()
    return row if len(row) <= 60 else row[:60] + "…"


@router.callback_query(F.data == "menu:cut")
async def menu_cut(cq: CallbackQuery, state: FSMContext):
    if not await ensure_member(cq):
        return
    await state.update_data(workdir=get_workdir(cq.from_user.id))
    await state.set_state(CutByPhoneStates.waiting_file)
    await cq.answer()
    await cq.message.edit_text(
        "🧩 <b>号码拆包</b>\n\n"
        f"把名单发给我（txt / md / csv，单个 ≤{_limit_mb()}MB），"
        "再发手机号（一行一个，一次最多 5 个）。\n\n"
        "我告诉你号码在第几行，然后按你要的那段切出来：\n"
        "· <b>前段</b>：第 1 行 ~ 号码那行（含）\n"
        "· <b>后段</b>：号码那行之后\n\n"
        "（md 里的表格会自动转成制表符分隔，输出的都是 .txt）",
        reply_markup=cancel_kb(),
    )


@router.message(CutByPhoneStates.waiting_file, F.document)
async def cut_got_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = sanitize_filename(doc.file_name or "input.txt")
    if not name.lower().endswith(CUT_EXTS):
        await message.answer("⚠️ 只认 txt / md / csv / log / dat 文件，换个文件试试。")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            f"⚠️ 文件太大，单个最大 {_limit_mb()}MB"
            "（Telegram 只给机器人下载 20MB 以内）。"
        )
        return

    data = await state.get_data()
    workdir = data.get("workdir") or get_workdir(message.from_user.id)
    path = os.path.join(workdir, name)
    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=path)
        text, enc = phone_split.decode_with_name(read_file(path))
    except Exception as e:
        await message.answer(f"⚠️ 读取失败：{html.escape(str(e)[:120])}")
        return

    rows, nl = phone_split.rows_of(text)
    if not rows:
        await message.answer("⚠️ 文件是空的，没法拆。")
        return

    is_md = name.lower().endswith((".md", ".markdown"))
    rows, table, cleaned = phone_split.normalize(rows, is_md)
    await state.update_data(
        workdir=workdir,
        src_path=path,
        name=name,
        enc=enc,
        md=is_md,
        queue=[],
        current=None,
        seg=None,
    )
    await state.set_state(CutByPhoneStates.waiting_phone)

    tip = (
        f"✅ 已收到 <code>{html.escape(name)}</code>\n"
        f"共 <b>{len(rows):,}</b> 行 ｜ 编码 {enc}"
    )
    if table:
        tip += "\n· 识别为 Markdown 表格，已转成制表符分隔"
    if cleaned:
        tip += "\n· 已去掉行尾多余空格"
    if is_md:
        tip += "\n· md 文件，切出来仍是 .txt"
    tip += "\n\n发手机号（一行一个，最多 5 个）"
    await message.answer(tip)


async def _announce_phone(message: Message, state: FSMContext, phone: str) -> None:
    """找号码在第几行；找到就让他选切哪段，没找到就提示并继续下一个。"""
    data = await state.get_data()
    if not data.get("src_path"):
        await message.answer("⚠️ 状态丢了，请重新点「🧩 号码拆包」再发文件。")
        await cleanup_state(state)
        return

    try:
        rows, _nl = _load_rows(data)
    except Exception as e:
        await message.answer(f"⚠️ 读取文件失败：{html.escape(str(e)[:120])}")
        return

    res = phone_split.split_by_phone(rows, phone)
    if res is None:
        await message.answer(
            f"⚠️ 手机号 <code>{html.escape(phone)}</code> 没找到。\n"
            f"文件共 {len(rows):,} 行，换个号码试试。"
        )
        await _next_or_wait(message, state)
        return

    await state.update_data(
        current={
            "phone": res.phone,
            "index": res.index,
            "total": res.total,
            "hit": res.hit_row,
        },
        seg=None,
    )
    await state.set_state(CutByPhoneStates.waiting_segment)
    await message.answer(
        f"📱 手机号 <code>{html.escape(res.phone)}</code> 在第 <b>{res.line_no:,}</b> 行\n"
        f"<code>{html.escape(_safe_hit(res.hit_row))}</code>\n\n"
        f"文件共 {res.total:,} 行｜前段 {res.before_count:,} 条，"
        f"后段 {res.after_count:,} 条\n\n要哪段？",
        reply_markup=cut_seg_kb(res.before_count, res.after_count),
    )


async def _next_or_wait(message: Message, state: FSMContext) -> None:
    """队列里还有号码就接着处理，没有就等用户再发。"""
    data = await state.get_data()
    queue = list(data.get("queue") or [])
    if queue:
        phone = queue.pop(0)
        await state.update_data(queue=queue)
        await _announce_phone(message, state, phone)
        return
    await state.update_data(current=None, seg=None)
    await state.set_state(CutByPhoneStates.waiting_phone)
    await message.answer(
        "这一批处理完了。\n要拆别的号码直接发号码，换文件就重新点「🧩 号码拆包」。"
    )


@router.message(CutByPhoneStates.waiting_phone, F.text)
async def cut_got_phone(message: Message, state: FSMContext):
    text = message.text or ""
    phones = phone_split.extract_phones(text)
    if not phones:
        await message.answer(
            "⚠️ 没识别到手机号。\n请发 11 位手机号（一行一个，最多 5 个）。"
        )
        return
    if len(phones) > MAX_PHONES_PER_BATCH:
        phones = phones[:MAX_PHONES_PER_BATCH]
        await message.answer(f"ℹ️ 一次最多 {MAX_PHONES_PER_BATCH} 个，先处理前 {MAX_PHONES_PER_BATCH} 个。")

    first, rest = phones[0], phones[1:]
    await state.update_data(queue=rest)
    await _announce_phone(message, state, first)


@router.callback_query(CutByPhoneStates.waiting_segment, F.data.startswith("cut:seg:"))
async def cut_pick_segment(cq: CallbackQuery, state: FSMContext):
    seg = (cq.data or "").split(":")[-1]
    if seg not in SEG_LABEL:
        await cq.answer("未知选项", show_alert=True)
        return
    data = await state.get_data()
    cur = data.get("current")
    if not cur:
        await cq.answer("这条按钮过期了，重新发个号码。", show_alert=True)
        return

    index, total = int(cur["index"]), int(cur["total"])
    before_count, after_count = index + 1, total - index - 1
    if seg in ("after", "both") and after_count <= 0:
        await cq.answer("这段没内容", show_alert=True)
        await cq.message.answer("这个号码在最后一行，后面没有内容，只能选前段。")
        return
    if seg == "before" and before_count <= 0:
        await cq.answer("这段没内容", show_alert=True)
        return

    await state.update_data(seg=seg)
    await state.set_state(CutByPhoneStates.waiting_name)
    await cq.answer()
    text = (
        f"手机号 <code>{html.escape(cur['phone'])}</code> 在第 {index + 1:,} 行"
        f"｜已选：{SEG_LABEL[seg]}\n\n文件名怎么定？"
    )
    try:
        await cq.message.edit_text(text, reply_markup=cut_name_kb())
    except TelegramBadRequest:
        await cq.message.answer(text, reply_markup=cut_name_kb())


@router.callback_query(CutByPhoneStates.waiting_name, F.data == "cut:name:orig")
async def cut_use_orig_name(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await _deliver(cq.message, state, custom=None)


@router.callback_query(CutByPhoneStates.waiting_name, F.data == "cut:name:custom")
async def cut_use_custom_name(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    try:
        await cq.message.edit_text(
            "✍️ 发个文件名给我（不用带 .txt）：", reply_markup=cancel_kb()
        )
    except TelegramBadRequest:
        await cq.message.answer("✍️ 发个文件名给我（不用带 .txt）：", reply_markup=cancel_kb())


@router.message(CutByPhoneStates.waiting_name, F.text)
async def cut_custom_name_input(message: Message, state: FSMContext):
    custom = (message.text or "").strip()
    if not custom or len(custom) > 60:
        await message.answer("⚠️ 名字不合法（1~60 个字），再发一次：")
        return
    await _deliver(message, state, custom=custom)


async def _deliver(message: Message, state: FSMContext, custom: str | None) -> None:
    """按选好的段切出来发文件。"""
    data = await state.get_data()
    cur, seg = data.get("current"), data.get("seg")
    if not cur or not seg:
        await message.answer("⚠️ 状态丢了，重新发一次手机号。")
        await cleanup_state(state)
        return

    try:
        rows, nl = _load_rows(data)
    except Exception as e:
        await message.answer(f"⚠️ 读取文件失败：{html.escape(str(e)[:120])}")
        return

    index = int(cur["index"])
    if index >= len(rows):
        await message.answer("⚠️ 文件内容变了，重新发一次号码。")
        return
    before_rows, after_rows = rows[: index + 1], rows[index + 1:]

    names = phone_split.build_names(data.get("name", "结果.txt"), custom, seg)
    plan = []
    if "after" in names:
        plan.append(
            (
                names["after"],
                after_rows,
                f"后段：第 {index + 2:,}–{len(rows):,} 行，共 {len(after_rows):,} 条",
            )
        )
    if "before" in names:
        plan.append(
            (
                names["before"],
                before_rows,
                f"前段：第 1–{index + 1:,} 行，共 {len(before_rows):,} 条",
            )
        )

    workdir = data.get("workdir") or get_workdir(message.from_user.id)
    os.makedirs(workdir, exist_ok=True)
    sent = 0
    for fname, seg_rows, cap in plan:
        path = os.path.join(workdir, fname)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(phone_split.render(seg_rows, nl))
            await message.answer_document(
                FSInputFile(path, filename=fname),
                caption=f"{cap}\n文件：{fname}",
            )
            sent += 1
        except Exception as e:
            print(f"[cut] 发送 {fname} 失败：{e}")
            await message.answer(f"⚠️ 文件 {html.escape(fname)} 发送失败，请重试。")

    if sent:
        print(f"[cut] 用户 {message.from_user.id} 按 {cur['phone']} 拆包，发出 {sent} 个文件")
    await _next_or_wait(message, state)
