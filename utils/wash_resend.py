"""洗数据结果补发：每分钟扫一次洗数据群最近 30 分钟的消息。

背景：结果文件已经发到洗数据群，但机器人那一刻掉线/异常，文件就没转给用户。
实时处理器（handlers/wash.py::alt_user_reply）负责「收到就转发」；
这里负责兜底——把群里「还没送到用户手上」的结果文件补发过去，并照常结算。

安全约束：
1. 只处理带 7 位随机码前缀的文件，且该随机码还有任务记录（wash_pending 还在，
   已结清的任务也会保留记录到过期，避免「刚好结清那一刻漏发的文件」补不回来）；
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
    """有任务记录（含已结清未过期）的随机码：{随机码: 剩余份数}。"""
    import time as _time

    from database import WASH_PENDING_TTL_SECONDS

    out = {}
    now = _time.time()
    with SessionLocal() as session:
        for row in session.scalars(
            select(AppConfig).where(AppConfig.key.like("wash_pending:%"))
        ):
            parts = (row.value or "").split(":", 4)
            if len(parts) < 3:
                continue
            # 超过 TTL 的等退款逻辑处理，不再补发
            if len(parts) >= 4 and parts[3].isdigit():
                if now - int(parts[3]) > WASH_PENDING_TTL_SECONDS:
                    continue
            remaining = 1
            if len(parts) >= 5:
                try:
                    fm = json.loads(parts[4])
                    remaining = len(fm.get("files") or [])
                except Exception:
                    remaining = 1
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


async def _scan(bot, only_code=None, dry_run=False, require_pending=True) -> dict:
    """扫描一轮。

    dry_run：只统计不发送（诊断用）；
    require_pending=False：只要随机码能查到用户就补发（管理员手动补救时用），
    这样即使任务记录已经被结清删除，之前漏发的文件也能捞回来。
    """
    from handlers import wash as wash_handlers
    from utils import telethon_client

    stat = {
        "connected": False,
        "chat": "",
        "pending_codes": 0,
        "scanned": 0,
        "prefixed": 0,
        "src_skipped": 0,
        "too_new": 0,
        "delivered_skipped": 0,
        "sent": 0,
        "candidates": [],
        "error": "",
        "dry_run": bool(dry_run),
    }

    userbot = telethon_client.userbot  # 运行期取值：启动时才会被赋值
    if userbot is not None:
        try:
            stat["connected"] = bool(userbot.is_connected())
        except Exception:
            stat["connected"] = False
    if not stat["connected"]:
        stat["error"] = "小号未连接（Telethon 没登录或掉线）"
        return stat

    with SessionLocal() as session:
        chat_raw = get_config(session, "wash_chat_id", "")
    stat["chat"] = chat_raw
    if not chat_raw:
        stat["error"] = "没有绑定洗数据群（wash_chat_id 为空）"
        return stat
    try:
        chat_id = int(chat_raw)
    except Exception:
        stat["error"] = f"洗数据群 ID 不是数字：{chat_raw}"
        return stat

    codes = _pending_codes()
    stat["pending_codes"] = len(codes)
    if not codes and require_pending:
        stat["error"] = "没有任何未过期的洗数据任务记录（wash_pending 为空）"
        return stat

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=SCAN_WINDOW_MINUTES)
    oldest_ok = now - timedelta(seconds=MIN_AGE_SECONDS)

    sent = 0
    sent_per_code = {}
    try:
        async for msg in userbot.iter_messages(chat_id, limit=MAX_MSGS_PER_SCAN):
            if msg.date is None or msg.date < cutoff:
                break  # 已经翻到窗口外
            stat["scanned"] += 1
            if sent >= MAX_SEND_PER_SCAN:
                break
            if not getattr(msg, "document", None):
                continue

            file_name = (msg.file.name if msg.file else "") or ""
            caption = (msg.message or "").strip()
            if caption == "im":
                stat["src_skipped"] += 1
                continue  # 我们自己发给小号的源文件

            m = _CODE_RE.match(file_name)
            if not m:
                continue
            stat["prefixed"] += 1
            code = m.group(1)
            if only_code and code != only_code:
                continue
            if require_pending:
                # 只补「还没回传的份数」：结清（done）的任务剩余 0，不再补，
                # 避免把之前已经成功发过的历史文件又发一遍
                remain = codes.get(code, 0)
                if remain <= 0:
                    continue
                if sent_per_code.get(code, 0) >= remain:
                    continue
            if "号码检测" in file_name:
                continue  # 空号检测结果走另一套流程
            if file_name in _source_names(code):
                stat["src_skipped"] += 1
                continue
            if msg.date > oldest_ok:
                stat["too_new"] += 1
                continue  # 太新，交给实时处理器

            user_id = _user_id_of(code)
            if not user_id:
                continue

            size = getattr(msg.document, "size", 0) or 0
            dkey = f"{file_name}|{size}"
            if wash_handlers._already_delivered(code, dkey):
                stat["delivered_skipped"] += 1
                continue

            if len(stat["candidates"]) < 20:
                stat["candidates"].append(f"{file_name}（用户 {user_id}）")
            if dry_run:
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
            sent_per_code[code] = sent_per_code.get(code, 0) + 1
            stat["sent"] += 1
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
        stat["error"] = f"扫描异常：{e}"
        print(f"[wash-resend] 扫描异常: {e}")
    return stat


async def _scan_once(bot) -> int:
    """扫一次，返回本轮补发的文件数。"""
    return (await _scan(bot))["sent"]


def format_report(stat: dict) -> str:
    """把扫描统计整理成给管理员看的说明。"""
    lines = [
        "<b>洗数据补发检查</b>",
        "",
        f"小号：{'已连接' if stat['connected'] else '未连接'}",
        f"洗数据群：<code>{stat['chat'] or '未绑定'}</code>",
        f"任务记录：{stat['pending_codes']} 个",
        f"近 30 分钟扫到消息：{stat['scanned']} 条，带随机码文件：{stat['prefixed']} 个",
        f"跳过：源文件 {stat['src_skipped']} · 太新 {stat['too_new']} · 已发过 {stat['delivered_skipped']}",
        f"本轮补发：{stat['sent']} 个",
    ]
    if stat.get("dry_run"):
        lines += ["", "（这是检查模式，没有发送任何文件。要真的补发：<code>/resend 随机码</code>）"]
    if stat["error"]:
        lines += ["", f"⚠️ {stat['error']}"]
    if stat["candidates"]:
        lines += ["", "<b>可补发文件：</b>"]
        lines += [f"· {c}" for c in stat["candidates"][:10]]
    return "\n".join(lines)


async def start_wash_resend(bot) -> None:
    """后台任务：每分钟扫一次洗数据群，补发漏掉的结果文件。"""
    await asyncio.sleep(90)  # 启动后先等一会，让小号客户端连上
    while True:
        try:
            with SessionLocal() as session:
                enabled = get_config(session, "wash_resend_enabled", "1")
            if enabled != "0":
                stat = await _scan(bot)
                if stat["sent"]:
                    print(f"[wash-resend] 本轮补发 {stat['sent']} 个结果文件")
        except Exception as e:
            print(f"[wash-resend] 循环异常: {e}")
        await asyncio.sleep(SCAN_INTERVAL)
