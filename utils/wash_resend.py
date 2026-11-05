"""洗数据结果补发：每分钟扫一次洗数据群最近 30 分钟的消息。

背景：结果文件已经发到洗数据群，但机器人那一刻掉线/异常，文件就没转给用户。
实时处理器（handlers/wash.py::alt_user_reply）负责「收到就转发」；
这里负责兜底——把群里「还没送到用户手上」的结果文件补发过去，并照常结算。

安全约束：
1. 只处理带 7 位随机码前缀、且该随机码还有未回传份额（wash_pending 仍在）的文件；
2. 跳过我们自己发给小号的源文件（提交时记在 wash_src:{码} 里，caption 是 im）；
3. 消息发出超过 60 秒才补发，避免和实时处理器抢同一个文件；
4. 已投递过的文件（wash_sent:{码}）不再发，重复扫描不会重复扣费。
"""
import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

from aiogram.types import FSInputFile
from sqlalchemy import select

from database import AppConfig, SessionLocal, get_config
from utils.helpers import get_workdir

SCAN_INTERVAL = 60          # 每 60 秒扫一次
SCAN_WINDOW_MINUTES = 30    # 只看最近 30 分钟的消息
MIN_AGE_SECONDS = 60        # 消息至少 60 秒后才补发（给实时处理器留出时间）
MAX_MSGS_PER_SCAN = 300     # 单次扫描读取的消息条数上限
MAX_SEND_PER_SCAN = 20      # 单次扫描最多补发多少个文件

_CODE_RE = re.compile(r"^([a-zA-Z0-9]{7})_")


def _pending_codes() -> dict:
    """还有未回传份额的任务：{随机码: 剩余份数}。"""
    out = {}
    from database import WASH_PENDING_TTL_SECONDS
    import time as _time

    now = _time.time()
    with SessionLocal() as session:
        for row in session.scalars(
            select(AppConfig).where(AppConfig.key.like("wash_pending:%"))
        ):
            parts = (row.value or "").split(":", 4)
            if len(parts) < 3:
                continue
            if len(parts) >= 4 and parts[3].isdigit():
                if now - int(parts[3]) > WASH_PENDING_TTL_SECONDS:
                    continue  # 已过期，等退款逻辑处理
            remaining = 1
            if len(parts) >= 5:
                try:
                    fm = json.loads(parts[4])
                    remaining = len(fm.get("files") or [])
                except Exception:
                    remaining = 1
            if remaining > 0:
                out[row.key.split(":", 1)[1]] = remaining
    return out


def _source_names(code: str) -> set:
    """我们发给小号的源文件名（带随机码前缀），补发时必须跳过。"""
    with SessionLocal() as session:
        raw = get_config(session, f"wash_src:{code}", "")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _user_id_of(code: str):
    with SessionLocal() as session:
        raw = get_config(session, f"wash_code:{code}", "")
    if not raw:
        return None
    try:
        return int(raw.split(":")[0])
    except Exception:
        return None


async def _scan_once(bot) -> int:
    """扫一次，返回本轮补发的文件数。"""
    from handlers import wash as wash_handlers
    from utils import telethon_client

    userbot = telethon_client.userbot  # 运行期取值：启动时才会被赋值
    if userbot is None or not userbot.is_connected():
        return 0

    with SessionLocal() as session:
        chat_raw = get_config(session, "wash_chat_id", "")
    if not chat_raw:
        return 0
    try:
        chat_id = int(chat_raw)
    except Exception:
        return 0

    codes = _pending_codes()
    if not codes:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=SCAN_WINDOW_MINUTES)
    oldest_ok = now - timedelta(seconds=MIN_AGE_SECONDS)

    sent = 0
    try:
        async for msg in userbot.iter_messages(chat_id, limit=MAX_MSGS_PER_SCAN):
            if msg.date is None or msg.date < cutoff:
                break  # 已经翻到窗口外
            if sent >= MAX_SEND_PER_SCAN:
                break
            if msg.date > oldest_ok:
                continue  # 太新，交给实时处理器
            if not getattr(msg, "document", None):
                continue

            file_name = (msg.file.name if msg.file else "") or ""
            caption = (msg.message or "").strip()
            if caption == "im":
                continue  # 我们自己发给小号的源文件

            m = _CODE_RE.match(file_name)
            if not m:
                continue
            code = m.group(1)
            if code not in codes:
                continue
            if "号码检测" in file_name:
                continue  # 空号检测结果走另一套流程

            user_id = _user_id_of(code)
            if not user_id:
                continue
            if file_name in _source_names(code):
                continue

            size = getattr(msg.document, "size", 0) or 0
            dkey = f"{file_name}|{size}"
            if wash_handlers._already_delivered(code, dkey):
                continue

            original_name = wash_handlers._washed_original_name(file_name)
            workdir = get_workdir(user_id)
            tmp_path = os.path.join(workdir, f"resend_{msg.id}_{original_name}")
            try:
                got = await msg.download_media(file=tmp_path)
            except Exception as e:
                print(f"[wash-resend] 下载失败 {file_name}: {e}")
                continue
            if not got:
                continue

            try:
                await bot.send_document(
                    chat_id=user_id,
                    document=FSInputFile(tmp_path, filename=original_name),
                    caption=f"✅ 你的洗数据已完成！\n原文件：{original_name}",
                )
            except Exception as e:
                print(f"[wash-resend] 发送失败 {file_name}: {e}")
                continue

            sent += 1
            wash_handlers._remember_delivery(code, dkey)
            try:
                wash_handlers._settle_wash_result(code, user_id, original_name)
            except Exception as e:
                print(f"[wash-resend] 结算失败 {code}: {e}")
            print(f"[wash-resend] 已补发 {file_name} → {user_id}")
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass
    except Exception as e:
        print(f"[wash-resend] 扫描异常: {e}")
    return sent


async def start_wash_resend(bot) -> None:
    """后台任务：每分钟扫一次洗数据群，补发漏掉的结果文件。"""
    await asyncio.sleep(90)  # 启动后先等一会，让小号客户端连上
    while True:
        try:
            with SessionLocal() as session:
                enabled = get_config(session, "wash_resend_enabled", "1")
            if enabled != "0":
                n = await _scan_once(bot)
                if n:
                    print(f"[wash-resend] 本轮补发 {n} 个结果文件")
        except Exception as e:
            print(f"[wash-resend] 循环异常: {e}")
        await asyncio.sleep(SCAN_INTERVAL)
