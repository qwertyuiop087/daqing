"""洗数据：单独计费，按行数收费，转发到洗数据群处理。"""
import asyncio
import html
import json
import math
import os
import re
import secrets
import string
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import WASH_SERVICES, settings
from database import (
    PaymentOrder,
    SessionLocal,
    WashOrder,
    add_wash_balance,
    calc_wash_cost,
    calc_wash_service_cost,
    get_wash_svc_prices,
    get_config,
    get_usdt_rate_async,
    get_wash_balance,
    get_wash_quota,
    get_washed_total,
)
from states import AdminStates, WashStates
from services import im_prewash
from utils.dali_client import get_client
from utils.helpers import cleanup_state, get_workdir
from utils.keyboards import cancel_kb, menu_kb


class FromAltUserFilter(BaseFilter):
    """只匹配「小号」发来的消息。

    严格比较：alt_user_id 为空或不是纯数字时一律不匹配，
    避免配置异常（空值/带空格）时把普通用户的文件也当成回传结果吞掉。
    """
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        with SessionLocal() as session:
            alt_user_id = (get_config(session, "alt_user_id", "") or "").strip()
        if not alt_user_id.isdigit():
            return False
        return message.from_user.id == int(alt_user_id)


_RESULT_CODE_RE = re.compile(r"([a-zA-Z0-9]{7})_")


class KnownResultCodeFilter(BaseFilter):
    """只认「结果编号确实存在」的文件。

    回传逻辑必须排在普通文件处理之前，但绝不能把别人的文件截胡：
    只要文件名/说明里的 7 位编号在数据库里查不到（用户自己发的、
    转发多次的、编号已过期的），就放行给后面的普通流程处理。
    """
    async def __call__(self, message: Message) -> bool:
        doc = message.document
        if not doc:
            return False
        text = (doc.file_name or "") + " " + (message.caption or "")
        m = _RESULT_CODE_RE.search(text)
        if not m:
            return False
        code = m.group(1)
        try:
            with SessionLocal() as session:
                if get_config(session, f"wash_code:{code}", ""):
                    return True
                if get_config(session, f"phone_check_code:{code}", ""):
                    return True
        except Exception:
            # 数据库异常时保持旧行为：仍按回传结果处理
            return True
        return False

router = Router(name="wash")

MIN_RECHARGE = 5.0     # 单笔充值下限（USDT）
MAX_RECHARGE = 10000.0  # 单笔充值上限（USDT）


def gen_wash_code() -> str:
    """生成 7 位随机码（字母+数字），用于群内文件名的隐私保护。"""
    return "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(7)
    )
# 专门处理小号回传文件的路由，要放到最前面注册，避免被其他文件 handler 拦截
wash_reply_router = Router(name="wash_reply")


async def _send_document_retry(bot, chat_id: int, document, caption: str, attempts: int = 3) -> bool:
    """把结果文件发给用户，带重试。

    同一任务的多个结果文件几乎同时回传，连续发送偶尔会触发 Telegram
    限流（429）或网络抖动；这里自动等待重试，避免文件静默丢失。
    """
    from aiogram.exceptions import TelegramRetryAfter

    delay = 2.0
    for i in range(attempts):
        try:
            await bot.send_document(chat_id=chat_id, document=document, caption=caption)
            return True
        except TelegramRetryAfter as e:
            wait = float(getattr(e, "retry_after", 5)) + 1
            print(f"⏳ Telegram 限流，{wait:.0f}s 后重试发送给 {chat_id}")
            await asyncio.sleep(wait)
        except Exception as e:
            if i == attempts - 1:
                print(f"❌ 发送给 {chat_id} 最终失败: {e}")
                return False
            await asyncio.sleep(delay)
            delay *= 2
    return False


_last_unmatched_alert = 0.0


async def _alert_unmatched(bot, filename: str, code: str) -> None:
    """结果文件匹配不到用户时私聊管理员，避免静默丢文件（10 分钟节流，防止被刷屏）。"""
    global _last_unmatched_alert
    import time as _time
    now = _time.time()
    if now - _last_unmatched_alert < 600:
        return
    _last_unmatched_alert = now
    admin_ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
    if not admin_ids:
        return
    text = (
        "⚠️ <b>洗数据结果文件无法匹配用户</b>\n\n"
        f"文件：<code>{html.escape(filename)}</code>\n"
        f"随机码：<code>{html.escape(code)}</code>\n\n"
        "该文件没有转发给任何人，请检查 wash_code 映射是否丢失。"
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


def _washed_original_name(filename: str) -> str:
    """去掉 7 位随机码前缀，还原给用户看的原文件名。"""
    import re
    return re.sub(r"^[a-zA-Z0-9]{7}_", "", filename or "").strip() or (filename or "unnamed")


def _delivery_key(doc, filename: str) -> str:
    """结果文件的去重键。

    优先用 Telegram 的 file_unique_id（内容指纹）：同一批里两个同名同大小
    的输入文件，洗回来的结果文件名和大小可能完全一样，用「名字|大小」
    会把第二个当成重复投递丢掉，用户付了钱却拿不到文件。
    """
    unique = getattr(doc, "file_unique_id", "") or ""
    if unique:
        return f"{filename}|{unique}"
    return f"{filename}|{getattr(doc, 'file_size', 0) or 0}"


def _delivery_key_size(filename: str, size) -> str:
    """补发扫描（wash_resend）使用的去重键：文件名 + 字节数。

    实时投递和补发扫描必须用同一套键，否则「发过了」在另一边查不到，
    同一个文件就会被发两遍——这就是之前「洗很多文件时重复发」的原因。
    """
    return f"{filename}|{int(size or 0)}"


def _delivery_row_key(code: str, key: str) -> str:
    """每个去重键单独存一行配置，避免并发投递时读-改-写互相覆盖。"""
    import hashlib

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"wash_sent:{code}:{digest}"


def _delivery_keys(code: str) -> list:
    """旧格式：一个 JSON 列表。保留读取，兼容升级前已经记录过的任务。"""
    with SessionLocal() as session:
        raw = get_config(session, f"wash_sent:{code}", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _remember_delivery(code: str, key: str) -> None:
    from database import set_config

    row_key = _delivery_row_key(code, key)
    try:
        with SessionLocal() as session:
            if get_config(session, row_key, ""):
                return
            set_config(session, row_key, "1")
    except Exception:
        # 并发写同一个键时可能撞唯一约束，说明别的任务已经记过了，忽略即可
        pass


def _already_delivered(code: str, key: str) -> bool:
    if key in _delivery_keys(code):
        return True
    try:
        with SessionLocal() as session:
            return bool(get_config(session, _delivery_row_key(code, key), ""))
    except Exception:
        return False


# ---------------- 洗数据进度推送 ----------------
# 每个回传文件插一行 wash_prog:{码}:{hash}，用 COUNT 统计进度。
# 这样并发回传时不需要「读-改-写」计数器，不会丢数。

def _progress_mark_key(code: str, key: str) -> str:
    import hashlib

    return f"wash_prog:{code}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _progress_count(code: str) -> int:
    from sqlalchemy import func

    from database import AppConfig

    try:
        with SessionLocal() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(AppConfig)
                    .where(AppConfig.key.like(f"wash_prog:{code}:%"))
                )
                or 0
            )
    except Exception:
        return 0


def _progress_begin(code: str, total: int, chat_id: int, message_id: int) -> None:
    """登记本批任务的文件总数和进度消息位置。"""
    from database import set_config

    try:
        with SessionLocal() as session:
            set_config(session, f"wash_prog_total:{code}", str(int(total)))
            set_config(session, f"wash_prog_msg:{code}", f"{chat_id}:{message_id}")
    except Exception as e:
        print(f"[wash-progress] 登记进度失败: {e}")


def _progress_mark(code: str, key: str) -> None:
    from database import set_config

    row_key = _progress_mark_key(code, key)
    try:
        with SessionLocal() as session:
            if get_config(session, row_key, ""):
                return
            set_config(session, row_key, "1")
    except Exception:
        pass


async def _progress_tick(bot, code: str) -> None:
    """每回传一个文件刷新一次进度消息。任何异常都不能影响文件交付。"""
    try:
        with SessionLocal() as session:
            total = int(get_config(session, f"wash_prog_total:{code}", "0") or 0)
            meta = get_config(session, f"wash_prog_msg:{code}", "")
        if total <= 0 or ":" not in meta:
            # 升级前提交的老任务没有登记进度，打印一行方便排查
            print(f"[wash-progress] {code} 未登记进度（total={total}, meta={meta!r}）")
            return
        raw_done = _progress_count(code)
        done = min(raw_done, total)
        chat_id_s, msg_id_s = meta.split(":", 1)
        finished = raw_done >= total
        # 先安排报告（每来一个文件就重置计时），再编辑消息：
        # 编辑可能因为「内容没变」报错，不能让它挡住报告计时
        if finished:
            _schedule_report(bot, code)
            _schedule_im_finalize(bot, code)
        text = (
            f"{'✅' if finished else '⏳'} <b>洗数据{'已完成' if finished else '处理中'}</b>\n\n"
            f"进度：<b>{done}/{total}</b> "
            + ("全部结果已发送完毕。" if finished else "请耐心等待。")
        )
        try:
            await bot.edit_message_text(
                chat_id=int(chat_id_s), message_id=int(msg_id_s), text=text
            )
        except Exception as e:
            # 内容没变 / 消息被删 都无所谓，进度只是辅助信息
            print(f"[wash-progress] 编辑失败（不影响交付）：{e}")
        print(f"[wash-progress] {code} 进度 {done}/{total}")
    except Exception as e:
        print(f"[wash-progress] 刷新进度失败: {e}")


# ---------------- 苹果 iMessage 预撞库：安卓合计 ----------------
# 「🍎 苹果 iMessage」提交时先做本地撞库（services/im_prewash.py）：
# 只有苹果号带随机码送去群洗，安卓号留在本地；
# 群洗带回「洗完的安卓」，跟「撞库的安卓」相加，合成一份安卓合计发回。

# 结果文件是成批陆续到的，安静几秒再合并，避免漏掉后到的安卓结果
IM_FINALIZE_QUIET_SECONDS = 8

_im_finalize_tasks: dict = {}


async def _download_result_phones(bot, doc) -> list:
    """把群回传的安卓结果下载下来取出号码（下载失败返回空列表）。"""
    import tempfile

    from services.file_ops import decode_bytes, read_file
    from utils.phone_db import extract_phones

    workdir = tempfile.mkdtemp(prefix="impre_")
    try:
        file = await bot.get_file(doc.file_id)
        path = os.path.join(workdir, "android_result.txt")
        await bot.download_file(file.file_path, destination=path)
        return extract_phones(decode_bytes(read_file(path)))
    except Exception as e:
        print(f"[im-pre] 下载安卓结果失败：{e}")
        return []
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _schedule_im_finalize(bot, code: str) -> None:
    """安排一次安卓合计合并：期间每来一个结果文件就重新计时。"""
    if not im_prewash.state_exists(code):
        return
    old = _im_finalize_tasks.get(code)
    if old is not None and not old.done():
        old.cancel()
    _im_finalize_tasks[code] = asyncio.create_task(_im_finalize_later(bot, code))


async def _im_finalize_later(bot, code: str) -> None:
    me = asyncio.current_task()
    try:
        await asyncio.sleep(IM_FINALIZE_QUIET_SECONDS)
        await _send_im_android_total(bot, code)
    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"[im-pre] 安卓合计发送失败 {code}: {e}")
    finally:
        if _im_finalize_tasks.get(code) is me:
            _im_finalize_tasks.pop(code, None)


async def _send_im_android_total(bot, code: str) -> None:
    """把「撞库安卓 + 洗完安卓」合成一份发给用户（每个任务只发一次）。"""
    if im_prewash.already_sent(code):
        return
    state = im_prewash.load_state(code)
    if not state:
        return
    # 结果还没回传齐就等最后一份：否则合计会少掉后到的安卓结果
    try:
        with SessionLocal() as session:
            total = int(get_config(session, f"wash_prog_total:{code}", "0") or 0)
        if total > 0 and _progress_count(code) < total:
            print(f"[im-pre] {code} 结果还没回传齐（{_progress_count(code)}/{total}），安卓合计先不发")
            return
    except Exception:
        pass

    merged = await asyncio.to_thread(im_prewash.build_merged, code)
    if not merged:
        return
    user_id = int(state.get("user") or 0)
    if not user_id:
        return

    # 说明文字跟普通结果一模一样：客户只知道自己收到了「安卓」这份结果，
    # 看不出机器人内部做过撞库/合并（撞库数、群洗数只打在服务器日志里）。
    caption = (
        f"✅ 你的洗数据已完成！\n原文件：{html.escape(merged['name'])}"
    )
    ok = await _send_document_retry(
        bot, user_id, FSInputFile(merged["path"], filename=merged["name"]), caption
    )
    if not ok:
        await _alert_undelivered(bot, merged["name"], code, user_id)
        return
    im_prewash.mark_sent(code)
    print(
        f"[im-pre] 已发安卓 {merged['name']} → {user_id}"
        f"（撞库 {merged['pre']:,} + 群洗 {merged['post']:,} = {merged['total']:,}）"
    )


# ---------------- 洗好的苹果自动入库 ----------------

def _save_apple_library(pairs: list) -> int:
    """线程里跑：批量 upsert 到本地号码库（新号插入、老号改类型）。"""
    from database import SessionLocal, bulk_upsert_phone_profiles

    with SessionLocal() as session:
        return bulk_upsert_phone_profiles(session, pairs)


async def _remember_washed_apples(bot, doc, code: str, phones: list | None = None) -> None:
    """把群里判定成「苹果」的号码自动写进本地号码库。

    这是最准的撞库数据：洗一遍就多一批，下次别人送来同样的号，
    本地撞库直接命中（预撞库能命中、群洗就少花钱）。
    只认「苹果 iMessage」的任务 —— 支付宝/微信那些洗出来的也是手机号，
    但那是别家业务的结果，不能当苹果号入库。
    """
    with SessionLocal() as session:
        meta_raw = get_config(session, f"wash_meta:{code}", "")
        flag = (get_config(session, "im_autosave", "1") or "1").strip().lower()
    if flag in ("0", "false", "off", "no", "关", "关闭"):
        return
    service = ""
    try:
        service = str((json.loads(meta_raw) or {}).get("service") or "")
    except Exception:
        service = ""
    if "苹果" not in service:
        return

    if phones is None:
        phones = await _download_result_phones(bot, doc)
    if not phones:
        return
    pairs = [(p, "苹果") for p in dict.fromkeys(phones) if p]
    try:
        written = await asyncio.to_thread(_save_apple_library, pairs)
        print(f"[im-lib] {code} 洗好的苹果入库 {written:,} 个（去重后 {len(pairs):,}）")
    except Exception as e:
        print(f"[im-lib] 苹果入库失败（不影响交付）：{e}")


# ---------------- 洗数据结果统计报告 ----------------

def _report_begin(
    code: str, files: int, lines: int, phones: int, cost: float, service: str = ""
) -> None:
    """提交时先记下本批的输入数据，等全部回传后算报告。"""
    import time as _time

    from database import set_config

    try:
        with SessionLocal() as session:
            set_config(
                session,
                f"wash_meta:{code}",
                json.dumps(
                    {
                        "files": int(files),
                        "lines": int(lines),
                        "phones": int(phones),
                        "cost": float(cost),
                        "service": service or "",
                        "ts": _time.time(),
                    },
                    ensure_ascii=False,
                ),
            )
    except Exception as e:
        print(f"[wash-report] 登记报告数据失败: {e}")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


_report_sent: set[str] = set()
_report_tasks: dict[str, asyncio.Task] = {}

# 结果到齐后不立刻发报告：洗数据服务常常一个源文件回传多个结果文件，
# 而且是隔几秒陆续到的。安静几秒没有新文件就发，避免报告夹在两个文件中间。
REPORT_QUIET_SECONDS = 4


def _schedule_report(bot, code: str) -> None:
    """安排一次报告：期间每来一个新结果就重新计时。"""
    old = _report_tasks.get(code)
    if old is not None and not old.done():
        old.cancel()
    _report_tasks[code] = asyncio.create_task(_report_after_quiet(bot, code))


async def _report_after_quiet(bot, code: str) -> None:
    me = asyncio.current_task()
    try:
        await asyncio.sleep(REPORT_QUIET_SECONDS)
    except asyncio.CancelledError:
        return
    try:
        await _maybe_send_report(bot, code)
    finally:
        if _report_tasks.get(code) is me:
            _report_tasks.pop(code, None)


async def _maybe_send_report(bot, code: str) -> None:
    """安静期满后发一份统计报告，每个任务只发一次。"""
    import time as _time

    if code in _report_sent:
        return
    try:
        with SessionLocal() as session:
            total = int(get_config(session, f"wash_prog_total:{code}", "0") or 0)
            raw = get_config(session, f"wash_meta:{code}", "")
            user_id_str = get_config(session, f"wash_code:{code}", "")
        # 还没回传齐就别发，等下一个文件到达时会重新安排
        if total <= 0 or _progress_count(code) < total:
            return
        _report_sent.add(code)

        meta = json.loads(raw) if raw else {}
        if not meta or not user_id_str:
            return
        user_id = int(user_id_str.split(":")[0])

        delivered = _progress_count(code)
        elapsed = _time.time() - float(meta.get("ts") or _time.time())
        lines = int(meta.get("lines") or 0)
        speed = (lines / elapsed) if elapsed > 0 else 0

        text = (
            f"📊 <b>洗数据完成报告</b>\n"
            f"任务编号：<code>{html.escape(code)}</code>\n"
            + (
                f"数据类型：{html.escape(str(meta.get('service') or ''))}\n"
                if meta.get("service")
                else ""
            )
            + "────────────\n"
            f"提交文件　{int(meta.get('files') or 0)} 个\n"
            f"提交行数　{lines:,} 行\n"
            f"号码总数　{int(meta.get('phones') or 0):,} 个\n"
            f"结果文件　{delivered} 个（全部送达）\n"
            "────────────\n"
            f"耗时　　　{_fmt_duration(elapsed)}\n"
            f"费用　　　¥{float(meta.get('cost') or 0):.4f}\n"
            f"平均速度　{speed:.0f} 行/秒"
        )
        await bot.send_message(user_id, text, parse_mode="HTML")
        print(f"[wash-report] 已发送报告 {code}（{lines} 行 / {elapsed:.0f} 秒）")
    except Exception as e:
        print(f"[wash-report] 发送报告失败: {e}")


_last_undelivered_alert = 0.0


async def _alert_undelivered(bot, filename: str, code: str, user_id: int) -> None:
    """结果文件没能送达用户时私聊管理员（10 分钟节流），避免文件静默丢失。"""
    global _last_undelivered_alert
    import time as _time
    now = _time.time()
    if now - _last_undelivered_alert < 600:
        return
    _last_undelivered_alert = now
    admin_ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
    text = (
        "⚠️ <b>洗数据结果没能发给用户</b>\n\n"
        f"用户：<code>{user_id}</code>\n"
        f"文件：<code>{html.escape(filename)}</code>\n"
        f"随机码：<code>{html.escape(code)}</code>\n\n"
        "文件仍在洗数据群里，请手动转发给该用户；\n"
        "本次没有结算扣费，48 小时未回传会自动按比例退款。"
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def _settle_wash_result(
    random_code: str, user_id: int, original_name: str
) -> tuple:
    """回传成功后记账：批量任务每收到一份结算一份，全部回传才结清。

    返回 (结算金额, 结算行数, 是否已结清)。只在文件真正送达用户后调用，
    保证「没送达就不扣钱」。

    用按用户串行锁包住「读 pending → 扣一份 → 写回」：
    批量结果文件是同时到达的，不加锁会两个 handler 读到同一份列表，
    导致同一份结算两次、另一份永远不结算。
    """
    from utils.helpers import user_lock

    async with user_lock(user_id):
        return _settle_wash_result_locked(random_code, user_id, original_name)


def _settle_wash_result_locked(
    random_code: str, user_id: int, original_name: str
) -> tuple:
    with SessionLocal() as session:
        pending = get_config(session, f"wash_pending:{random_code}", "")
    if not pending:
        return (0.0, 0, True)

    parts = pending.split(":", 4)
    if len(parts) < 3:
        return (0.0, 0, True)
    line_count = int(parts[1])
    cost = float(parts[2])
    file_map = None
    if len(parts) >= 5:
        try:
            file_map = json.loads(parts[4])
        except Exception:
            file_map = None

    settle_cost = cost
    settle_lines = line_count
    done = True
    if file_map and file_map.get("done"):
        # 这个任务已经结清过（补发扫描可能还会命中它），不再重复扣费
        return (0.0, 0, True)
    if file_map and file_map.get("files"):
        rem = list(file_map["files"])
        charged = float(file_map.get("charged", 0.0))
        share = rem.pop(0)
        if rem:
            settle_cost = round(cost * share / line_count, 4) if line_count else 0.0
            settle_lines = share
            file_map["files"] = rem
            file_map["charged"] = round(charged + settle_cost, 4)
            done = False
        else:
            # 最后一个结果：把剩余金额一次结清，避免四舍五入差额
            settle_cost = round(cost - charged, 4)
            settle_lines = share

    if settle_cost > 0:
        with SessionLocal() as session:
            session.add(
                WashOrder(
                    user_id=user_id,
                    filename=original_name,
                    row_count=settle_lines,
                    cost=settle_cost,
                    type="deduct",
                )
            )
            session.commit()

    # 结清后不删记录，改成 done 标记：补发扫描在窗口内还能认出这个随机码，
    # 「刚结清那一刻没发出去的结果文件」才补得回来；到期由 TTL 统一清理。
    with SessionLocal() as session:
        from database import delete_config, set_config
        if done:
            if file_map is not None:
                file_map["files"] = []
                file_map["charged"] = cost
                file_map["done"] = 1
                set_config(
                    session,
                    f"wash_pending:{random_code}",
                    f"{user_id}:{line_count}:{cost}:{parts[3]}:"
                    f"{json.dumps(file_map, ensure_ascii=False)}",
                )
            else:
                delete_config(session, f"wash_pending:{random_code}")
        elif file_map is not None:
            set_config(
                session,
                f"wash_pending:{random_code}",
                f"{user_id}:{line_count}:{cost}:{parts[3]}:"
                f"{json.dumps(file_map, ensure_ascii=False)}",
            )
    return (settle_cost, settle_lines, done)


def wash_main_kb(balance: float) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="💳 充值洗数据", callback_data="wash:recharge")
    b.button(text="📤 上传文件洗数据", callback_data="wash:upload")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def wash_recharge_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="10 USDT", callback_data="wash:recharge:10")
    b.button(text="50 USDT", callback_data="wash:recharge:50")
    b.button(text="100 USDT", callback_data="wash:recharge:100")
    b.button(text="✏️ 自定义金额", callback_data="wash:recharge_custom")
    # OKPay 收款（配了商户号才显示）
    okpay_ready = False
    try:
        from utils.okaypay_client import is_configured as _okpay_ready
        okpay_ready = _okpay_ready()
    except Exception:
        okpay_ready = False
    if okpay_ready:
        b.button(text="🔵 OKPay 充值", callback_data="okpay:topup")
        b.button(text="🔙 返回", callback_data="menu:wash")
        b.adjust(3, 1, 1, 1)
        return b.as_markup()
    b.button(text="🔙 返回", callback_data="menu:wash")
    b.adjust(3, 1, 1)
    return b.as_markup()


def _tier_price_text(prices: list) -> str:
    """把阶梯价渲染成多行文本（苹果 iMessage 专用）。"""
    lines = []
    for i, p in enumerate(prices):
        min_rows = int(p["min_rows"])
        if i + 1 < len(prices):
            max_rows = int(prices[i + 1]["min_rows"]) - 1
            lines.append(
                f"  {min_rows:,} ~ {max_rows:,} 行：¥{p['price_per_row']:.4f}/行"
            )
        else:
            lines.append(f"  {min_rows:,} 行以上：¥{p['price_per_row']:.4f}/行")
    return "\n".join(lines)


@router.callback_query(F.data == "menu:wash")
async def menu_wash(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    with SessionLocal() as session:
        balance = get_wash_balance(session, cq.from_user.id)
        svc_prices = get_wash_svc_prices(session)

    # 各类型单价（元/万行）；苹果 iMessage 的价格点进去才看
    svc_items = []
    for code, label, default in WASH_SERVICES:
        if code == "im":
            continue
        price = float(svc_prices.get(code, default) or 0)
        if price <= 0:
            continue
        svc_items.append(f"{label} W{price:g}")
    svc_lines = [
        "  " + " · ".join(svc_items[i : i + 2])
        for i in range(0, len(svc_items), 2)
    ]
    svc_text = "\n".join(svc_lines)

    await cq.message.edit_text(
        "🧼 <b>洗数据</b>\n\n"
        f"当前余额：<b>¥{balance:.4f} 元</b>\n"
        "────────────\n"
        "<b>各类型单价（元/万行）：</b>\n"
        f"{svc_text}\n"
        "（🍎 苹果 iMessage 按行数阶梯计价，选类型后可看）\n\n"
        "洗完再扣费。",
        reply_markup=wash_main_kb(balance),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "wash:recharge")
async def wash_recharge(cq: CallbackQuery):
    await cq.answer()
    with SessionLocal() as session:
        rate = await get_usdt_rate_async(session)
    await cq.message.edit_text(
        "💳 <b>充值洗数据余额</b>\n\n"
        f"当前汇率：1 USDT = {rate} 元人民币\n\n"
        "选择充值金额（USDT）：\n"
        "支付方式：@dlqb 钱包 USDT\n"
        "充值后自动换算成人民币余额。",
        reply_markup=wash_recharge_kb(),
    )


@router.callback_query(F.data == "wash:recharge_custom")
async def wash_recharge_custom(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await cq.message.edit_text(
        "✏️ <b>自定义充值金额</b>\n\n"
        "请输入要充值的 USDT 金额（至少 5 USDT）：",
        reply_markup=cancel_kb(),
    )
    await state.set_state(WashStates.waiting_custom_amount)


@router.message(WashStates.waiting_custom_amount, F.text)
async def wash_recharge_custom_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
    except Exception:
        await message.answer("金额格式错误，请输入数字，例如：10")
        return

    if not math.isfinite(amount) or amount < 5:
        await message.answer("最少充值 5 USDT，请重新输入：")
        return

    await cleanup_state(state)

    # 创建订单
    client = get_client()
    if not client:
        await message.answer("支付系统未配置，请联系管理员。")
        return

    try:
        order_id = f"wash_{uuid.uuid4().hex[:16]}"
        user_id = message.from_user.id

        # 同步 HTTP 丢线程池，避免卡住事件循环
        result = await asyncio.to_thread(
            client.call,
            "/merchant/payLink",
            {
                "amount": amount,
                "coin": "usdt",
                "userOrder": order_id,
                "name": f"洗数据充值${amount}",
            },
        )
    except Exception as e:
        await message.answer(f"创建订单失败: {str(e)[:100]}")
        return

    if not result.get("ok"):
        await message.answer(f"创建订单失败: {result.get('msg', '未知错误')}")
        return

    pay_data = result["data"]
    pay_url = pay_data.get("pay_url", "")

    # 订单落库：保证「我已支付」查询时只能到账一次（幂等）
    with SessionLocal() as session:
        session.add(
            PaymentOrder(
                order_id=order_id,
                user_id=user_id,
                days=0,
                amount=str(amount),
                coin="usdt",
                status="pending",
                pay_url=pay_url,
            )
        )
        session.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 去支付", url=pay_url)
    kb.button(text="✅ 我已支付，查询状态", callback_data=f"wash:check:{order_id}")
    kb.button(text="🔙 返回", callback_data="wash:recharge")
    kb.adjust(1)

    await message.answer(
        f"🧼 <b>洗数据充值订单</b>\n\n"
        f"充值金额：${amount} USDT\n"
        f"订单号：{order_id}\n\n"
        "────────────\n"
        "点击「去支付」完成付款，付款后点击「我已完成支付」。",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("wash:recharge:"))
async def wash_create_order(cq: CallbackQuery):
    await cq.answer()
    try:
        amount = float(cq.data.split(":")[2])
    except Exception as e:
        await cq.answer(f"参数错误: {e}", show_alert=True)
        return

    # 金额来自按钮回调，客户端可以伪造：服务端只接受合理区间，
    # 防止 0 元 / 负数 / 天价订单污染渠道订单表
    if not (MIN_RECHARGE <= amount <= MAX_RECHARGE):
        await cq.answer(
            f"⚠️ 充值金额需在 {MIN_RECHARGE:g} ~ {MAX_RECHARGE:g} USDT 之间",
            show_alert=True,
        )
        return

    try:
        client = get_client()
    except Exception as e:
        await cq.answer(f"支付系统初始化失败: {e}", show_alert=True)
        return

    if not client:
        await cq.answer("支付系统未配置，请联系管理员。", show_alert=True)
        return

    try:
        order_id = f"wash_{uuid.uuid4().hex[:16]}"
        user_id = cq.from_user.id

        # 同步 HTTP 丢线程池，避免卡住事件循环
        result = await asyncio.to_thread(
            client.call,
            "/merchant/payLink",
            {
                "amount": amount,
                "coin": "usdt",
                "userOrder": order_id,
                "name": f"洗数据充值${amount}",
            },
        )
    except Exception as e:
        await cq.answer(f"创建订单失败: {str(e)[:100]}", show_alert=True)
        return

    if not result.get("ok"):
        await cq.answer(f"创建订单失败: {result.get('msg', '未知错误')}", show_alert=True)
        return

    pay_data = result["data"]
    pay_url = pay_data.get("pay_url", "")

    # 订单落库：保证「我已支付」查询时只能到账一次（幂等）
    with SessionLocal() as session:
        session.add(
            PaymentOrder(
                order_id=order_id,
                user_id=user_id,
                days=0,
                amount=str(amount),
                coin="usdt",
                status="pending",
                pay_url=pay_url,
            )
        )
        session.commit()

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 去支付", url=pay_url)
    kb.button(text="✅ 我已支付，查询状态", callback_data=f"wash:check:{order_id}")
    kb.button(text="🔙 返回", callback_data="wash:recharge")
    kb.adjust(1)

    await cq.message.edit_text(
        f"🧼 <b>洗数据充值订单</b>\n\n"
        f"充值金额：${amount} USDT\n"
        f"订单号：{order_id}\n\n"
        "────────────\n"
        "点击「去支付」完成付款，付款后点击「我已完成支付」。",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("wash:check:"))
async def wash_check_order(cq: CallbackQuery):
    order_id = cq.data.split(":")[2]

    client = get_client()
    if not client:
        await cq.answer("支付系统未配置。", show_alert=True)
        return

    with SessionLocal() as session:
        order = session.scalar(
            select(PaymentOrder).where(PaymentOrder.order_id == order_id)
        )
        if order is None:
            await cq.answer("订单不存在。", show_alert=True)
            return
        if order.user_id != cq.from_user.id:
            await cq.answer("这不是你的订单。", show_alert=True)
            return
        if order.status == "paid":
            # 幂等：已到账的订单无论点多少次都不再加余额
            await cq.answer("✅ 该订单已到账，请勿重复点击。", show_alert=True)
            return

    # 同步 HTTP 丢线程池，避免卡住事件循环
    result = await asyncio.to_thread(
        client.call, "/merchant/checkDeposit", {"userOrder": order_id}
    )
    if not result.get("ok"):
        await cq.answer("查询失败，请稍后再试。", show_alert=True)
        return

    data = result.get("data", {})
    statusText = data.get("statusText", "")
    amount = float(data.get("amount", 0) or 0)

    if statusText != "paid":
        statusTextCN = data.get("statusTextCN", "")
        await cq.answer(f"当前状态：{statusTextCN}，请完成支付后再查询。", show_alert=True)
        return

    # 支付成功：行锁 + 二次确认，与自动到账扫描互斥，防止同一订单被重复入账
    with SessionLocal() as session:
        locked = session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.order_id == order_id)
            .with_for_update()
        )
        if locked is None or locked.status == "paid":
            await cq.answer("该订单已到账。", show_alert=True)
            return
        locked.status = "paid"
        locked.txid = data.get("txid", locked.txid)

        rate = await get_usdt_rate_async(session)
        rmb_amount = amount * rate
        add_wash_balance(session, cq.from_user.id, rmb_amount)
        balance = get_wash_balance(session, cq.from_user.id)
        # 记录充值账单
        session.add(
            WashOrder(
                user_id=cq.from_user.id,
                filename=f"充值 ${amount} USDT",
                row_count=0,
                cost=rmb_amount,
                type="recharge",
            )
        )
        session.commit()

    await cq.answer("✅ 充值成功！", show_alert=True)
    await cq.message.edit_text(
        f"✅ <b>充值成功</b>\n\n"
        f"充值金额：${amount} USDT\n"
        f"换算汇率：1 USDT = {rate} 元\n"
        f"到账余额：¥{rmb_amount:.4f} 元\n"
        f"当前余额：¥{balance:.4f} 元",
        reply_markup=wash_main_kb(balance),
    )


# 一次洗数据任务最多收集的文件数（防止消息刷屏与发送限流）
WASH_MAX_FILES = 20

# 同一批文件的到达间隔很短（媒体组会几乎同时推送），
# 渲染时先等一下把这一批合并成一次编辑，避免疯狂编辑/触发限流
_UPLOAD_RENDER_DELAY = 0.8

# 每个用户一条上传会话：lock 串行化「追加文件 + 写状态」，
# dirty/task 保证同一时间只有一个渲染循环。
# aiogram 默认把每个 update 丢进独立 task 并发处理，
# 所以 8 个文件是 8 个 task 同时跑，光看 state 里的 status_msg_id 挡不住并发。
_UPLOAD_SESSIONS: dict = {}
_UPLOAD_SESSION_MAX = 500


def _upload_session(user_id: int) -> dict:
    sess = _UPLOAD_SESSIONS.get(user_id)
    if sess is None:
        if len(_UPLOAD_SESSIONS) >= _UPLOAD_SESSION_MAX:
            # 淘汰空闲会话，避免长期运行后字典无限增长
            for uid in [u for u, s in _UPLOAD_SESSIONS.items() if s["task"] is None]:
                _UPLOAD_SESSIONS.pop(uid, None)
                if len(_UPLOAD_SESSIONS) < _UPLOAD_SESSION_MAX:
                    break
        sess = {"lock": asyncio.Lock(), "dirty": False, "task": None}
        _UPLOAD_SESSIONS[user_id] = sess
    return sess


def wash_service_kb(prices: dict | None = None) -> InlineKeyboardMarkup:
    """洗数据类型选择键盘（两列，取消单独一行）。

    价格直接写在按钮上，例如「💙 支付宝 · W50」；
    苹果 iMessage 的单价是阶梯价，按钮上不显示，点进去才看得到。
    """
    prices = prices or {}
    b = InlineKeyboardBuilder()
    for code, label, default in WASH_SERVICES:
        price = float(prices.get(code, default) or 0)
        text = f"{label} · W{price:g}" if price > 0 else label
        b.button(text=text, callback_data=f"wash:svc:{code}")
    b.adjust(2)
    rows = b.export()
    rows.append([InlineKeyboardButton(text="❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wash_upload_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 开始洗数据", callback_data="wash:submit")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


def _upload_status_text(files: list, total: int, cost: float) -> str:
    """上传状态文本：文件清单 + 总行数 + 当前单价 + 预估费用。"""
    per = cost / total if total else 0
    shown = "\n".join(f"· {html.escape(f['name'])}（{f['lines']:,} 行）" for f in files[:10])
    if len(files) > 10:
        shown += f"\n… 还有 {len(files) - 10} 个文件"
    return (
        f"📥 已收到 <b>{len(files)}</b> 个文件，共 <b>{total:,}</b> 行\n"
        f"{shown}\n\n"
        f"苹果阶梯价估算：¥{per:.6f}/行 · 约 ¥{cost:.4f}\n"
        "（其它类型的单价见下一步的按钮，以所选类型为准）\n\n"
        "继续发文件，或点「🚀 开始洗数据」。\n"
        f"（只收 TXT，一次最多 {WASH_MAX_FILES} 个）"
    )


def _edit_gone(err: Exception) -> bool:
    """判断这次编辑失败是不是「消息已经不能改了」，只有这种情况才需要重发一条。"""
    text = str(err).lower()
    if "not modified" in text:  # 内容没变化，不是错误
        return False
    return any(
        key in text
        for key in (
            "message to edit not found",
            "message_id_invalid",
            "message can't be edited",
            "message cannot be edited",
            "message identifier is not specified",
            "topic_deleted",
        )
    )


async def _render_upload_status(bot: Bot, state: FSMContext, chat_id: int, user_id: int) -> None:
    """请求刷新上传状态；同一批更新会合并成一次编辑。"""
    sess = _upload_session(user_id)
    sess["dirty"] = True
    task = sess.get("task")
    if task is not None and not task.done():
        return
    sess["task"] = asyncio.create_task(_upload_render_loop(bot, state, chat_id, user_id))


async def _upload_render_loop(bot: Bot, state: FSMContext, chat_id: int, user_id: int) -> None:
    """唯一的渲染循环：编辑同一条状态消息，绝不新建。"""
    sess = _upload_session(user_id)
    try:
        while sess["dirty"]:
            sess["dirty"] = False
            await asyncio.sleep(_UPLOAD_RENDER_DELAY)  # 等这一批文件都到齐
            if await state.get_state() != WashStates.waiting_file.state:
                return
            data = await state.get_data()
            files = list(data.get("files") or [])
            if not files:
                continue
            total = sum(f["lines"] for f in files)
            with SessionLocal() as session:
                cost = calc_wash_cost(session, total)
            text = _upload_status_text(files, total, cost)

            status_id = data.get("status_msg_id")
            rendered = False
            if status_id:
                try:
                    await bot.edit_message_text(
                        text, chat_id=int(data.get("status_chat_id") or chat_id),
                        message_id=int(status_id),
                        reply_markup=wash_upload_kb(), parse_mode="HTML",
                    )
                    rendered = True
                except Exception as e:
                    rendered = not _edit_gone(e)
                    if not rendered:
                        print(f"[wash-upload] status message not editable, sending a new one: {e}")
            if not rendered:
                sent = await bot.send_message(
                    chat_id, text, reply_markup=wash_upload_kb(), parse_mode="HTML"
                )
                await state.update_data(status_msg_id=sent.message_id, status_chat_id=chat_id)
    except Exception as e:
        print(f"[wash-upload] render loop failed: {e}")
    finally:
        sess["task"] = None
        if sess["dirty"]:
            # 渲染期间又来了新文件，继续一轮
            await _render_upload_status(bot, state, chat_id, user_id)


async def _stop_upload_render(user_id: int) -> None:
    """结束上传状态渲染循环，避免和「提交」抢同一条消息。"""
    sess = _UPLOAD_SESSIONS.get(user_id)
    if not sess:
        return
    sess["dirty"] = False
    task = sess.get("task")
    if task is not None and not task.done():
        # 直接取消刷新任务，最多等 0.5 秒：不能让「开始洗数据」卡在这里
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except Exception:
            pass


async def _edit_or_answer(cq: CallbackQuery, text: str, reply_markup=None) -> None:
    """优先把结果写回点按钮的那条消息，改不动再发新消息。"""
    try:
        await cq.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            await cq.message.answer(text, reply_markup=reply_markup)
        except Exception as e:
            print(f"⚠️ 洗数据结果消息发送失败：{e}")


def _refund_full_batch(user_id: int, total_lines: int, cost: float, code: str, reason: str) -> None:
    """退回整单预扣费用并作废任务（仅在文件还没转发给小号之前失败时调用）。"""
    with SessionLocal() as session:
        from database import WashOrder, add_wash_balance, delete_config
        add_wash_balance(session, user_id, cost)
        session.add(
            WashOrder(
                user_id=user_id,
                filename=reason,
                row_count=-total_lines,
                cost=-cost,
                type="refund",
            )
        )
        delete_config(session, f"wash_pending:{code}")
        session.commit()


@router.callback_query(F.data == "wash:upload")
async def wash_upload(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    # 检查有没有绑定洗数据群
    with SessionLocal() as session:
        wash_chat_id = get_config(session, "wash_chat_id", "")
    if not wash_chat_id:
        await cq.message.edit_text(
            "⚠️ 洗数据功能未配置，请联系管理员。",
            reply_markup=cancel_kb(),
        )
        return

    # 这条说明消息本身就是「上传状态消息」：后面每收到一个文件都编辑它，
    # 一次发 8 个文件也只有这一条消息在滚动更新，不会刷屏。
    await state.update_data(
        workdir=get_workdir(cq.from_user.id),
        files=[],
        status_msg_id=cq.message.message_id,
        status_chat_id=cq.message.chat.id,
    )
    await state.set_state(WashStates.waiting_file)
    await cq.message.edit_text(
        "📤 <b>上传文件洗数据（支持多文件）</b>\n\n"
        "把要洗的 TXT 文件全部发给我，可以一次发多个。\n\n"
        "费用按<b>全部文件的总行数</b>计算：\n"
        "文件越多、总行数越大，档位越高、单价越便宜。\n\n"
        "全部发完后点「🚀 开始洗数据」。",
        reply_markup=wash_upload_kb(),
    )


@router.message(WashStates.waiting_file, F.document)
async def wash_got_file(message: Message, state: FSMContext, bot: Bot):
    """收集文件：每收到一个 TXT 就存起来并更新汇总，提交时按总行数计费。

    一次发多个文件时它们是并发到达的（每个 update 一个 task）：
    下载并发做，只有「追加到列表 + 刷新状态消息」这一步串行化，
    保证始终只有一条状态消息在滚动更新。
    """
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".txt", ".text")):
        await message.answer("⚠️ 只接受 TXT 文件。")
        return

    data = await state.get_data()
    if len(list(data.get("files") or [])) >= WASH_MAX_FILES:
        await message.answer(
            f"⚠️ 一次最多 {WASH_MAX_FILES} 个文件。请点「🚀 开始洗数据」提交当前这批。",
            reply_markup=wash_upload_kb(),
        )
        return

    workdir = data.get("workdir") or get_workdir(message.from_user.id)

    # Telegram 机器人 API 只能下载 20MB 以内的文件，正好 20.0MB 会被服务端直接拒
    # （Bad Request: file is too big），所以把话说清楚，别让用户只看到一行英文报错。
    limit_mb = settings.MAX_FILE_SIZE // 1024 // 1024

    def _too_big_tip(name: str, size) -> str:
        size_txt = f"（{size / 1024 / 1024:.1f}MB）" if size else ""
        return (
            f"⚠️ 文件 <code>{html.escape(name)}</code>{size_txt} 太大，"
            f"机器人能下载的上限是 {limit_mb}MB。\n\n"
            "用「📄 文件切割」把它切成更小的几份（每份行数填小一点，"
            f"控制在 {limit_mb}MB 以内），再重新上传就行。"
        )

    # 下载文件（大小超限直接提示）。临时文件名带 uuid：
    # 并发下载时不能再用「列表长度+1」当序号，否则同一批文件会写到同一个路径。
    try:
        file = await bot.get_file(doc.file_id)
        if (file.file_size or 0) > settings.MAX_FILE_SIZE:
            await message.answer(_too_big_tip(doc.file_name or "", file.file_size))
            return
        from services.file_ops import sanitize_filename
        safe = sanitize_filename(doc.file_name or "input.txt")
        tmp_path = os.path.join(workdir, f"in_{uuid.uuid4().hex[:8]}_{safe}")
        await bot.download_file(file.file_path, destination=tmp_path)
    except Exception as e:
        err = str(e)
        if "too big" in err.lower() or "file is too large" in err.lower():
            # 服务端拒绝下载：按文件大小给出可操作的提示，而不是干巴巴一行英文
            await message.answer(_too_big_tip(doc.file_name or "", doc.file_size))
            return
        await message.answer(f"⚠️ 下载失败：{err[:100]}")
        return

    # 统计行数与手机号数量
    try:
        from services.file_ops import decode_bytes, read_file
        from utils.phone_db import extract_phones
        text = decode_bytes(read_file(tmp_path))
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        phones = 0
        for ln in lines:
            phones += len(extract_phones(ln))
    except Exception as e:
        await message.answer(f"⚠️ 读取文件失败：{str(e)[:100]}")
        return

    sess = _upload_session(message.from_user.id)
    async with sess["lock"]:
        # 锁内重新读：并发到达的其它文件可能刚写进列表
        cur = await state.get_data()
        files = list(cur.get("files") or [])
        if len(files) >= WASH_MAX_FILES:
            await message.answer(
                f"⚠️ 一次最多 {WASH_MAX_FILES} 个文件，这个文件没有加入本批。\n"
                "请点「🚀 开始洗数据」提交当前这批。",
                reply_markup=wash_upload_kb(),
            )
            return
        if not cur.get("status_chat_id"):
            cur["status_chat_id"] = message.chat.id
        files.append(
            {
                "name": doc.file_name or "input.txt",
                "path": tmp_path,
                "lines": len(lines),
                "phones": phones,
            }
        )
        await state.update_data(
            files=files,
            workdir=workdir,
            status_chat_id=cur["status_chat_id"],
        )

    await _render_upload_status(bot, state, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "wash:submit")
async def wash_submit(cq: CallbackQuery, state: FSMContext, bot: Bot):
    """第一步：先让用户选要洗的数据类型。"""
    data = await state.get_data()
    files = list(data.get("files") or [])
    if not files:
        await cq.answer(
            "没找到上传的文件（可能是机器人刚重启）。\n请重新发一次文件再点开始洗数据。",
            show_alert=True,
        )
        return
    await cq.answer()
    with SessionLocal() as session:
        prices = get_wash_svc_prices(session)
    await _edit_or_answer(
        cq,
        f"🧼 <b>选择要洗的数据类型</b>\n\n"
        f"本批 {len(files)} 个文件，共 "
        f"{sum(int(f.get('lines') or 0) for f in files):,} 行",
        reply_markup=wash_service_kb(prices),
    )


@router.callback_query(F.data.startswith("wash:svc:"))
async def wash_submit_service(cq: CallbackQuery, state: FSMContext, bot: Bot):
    """第二步：选中类型。

    苹果 iMessage 是阶梯价，先展示价格明细让用户确认；
    其它类型价格已经写在按钮上，直接提交。
    """
    code = (cq.data or "").split(":", 2)[-1]
    labels = {code: label for code, label, _p in WASH_SERVICES}
    label = labels.get(code)
    if not label:
        await cq.answer("未知的数据类型，请重新选择。", show_alert=True)
        return

    # 苹果：先给价格明细 + 预计费用，再确认扣费
    if code == "im":
        data = await state.get_data()
        files = list(data.get("files") or [])
        if not files:
            await cq.answer("没找到上传的文件，请重新上传。", show_alert=True)
            return
        total_lines = sum(int(f.get("lines") or 0) for f in files)
        with SessionLocal() as session:
            from database import get_wash_prices

            prices = get_wash_prices(session)
            estimate = calc_wash_cost(session, total_lines)
            balance = get_wash_balance(session, cq.from_user.id)
        await cq.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ 确认扣费并提交", callback_data="wash:svcok:im")
        kb.button(text="❌ 取消", callback_data="cancel")
        kb.adjust(1)
        await _edit_or_answer(
            cq,
            "🍎 <b>苹果 iMessage</b>\n\n"
            "<b>阶梯定价（每行价格）：</b>\n"
            f"{_tier_price_text(prices)}\n"
            "────────────\n"
            f"本批：<b>{total_lines:,}</b> 行（{len(files)} 个文件）\n"
            f"预计费用：<b>¥{estimate:.4f}</b>\n"
            f"当前余额：¥{balance:.4f}\n\n"
            "确认后才会扣费并转发。",
            reply_markup=kb.as_markup(),
        )
        return

    await cq.answer(f"已选择：{label}")
    try:
        await _wash_submit_impl(cq, state, bot, svc_code=code, svc_label=label)
    except Exception as e:
        print(f"[wash] submit failed: {e}")
        try:
            await _edit_or_answer(
                cq,
                "⚠️ 提交失败：" + html.escape(str(e)[:150])
                + "\n\n请再点一次「🚀 开始洗数据」；如果还是不行，重新上传一次文件。",
                reply_markup=menu_kb(cq.from_user.id),
            )
        except Exception:
            pass


@router.callback_query(F.data == "wash:svcok:im")
async def wash_submit_confirm_im(cq: CallbackQuery, state: FSMContext, bot: Bot):
    """苹果 iMessage：用户确认阶梯价后再真正提交。"""
    await cq.answer("正在提交…")
    label = {c: l for c, l, _p in WASH_SERVICES}.get("im", "🍎 苹果 iMessage")
    try:
        await _wash_submit_impl(cq, state, bot, svc_code="im", svc_label=label)
    except Exception as e:
        print(f"[wash] submit failed: {e}")
        try:
            await _edit_or_answer(
                cq,
                "⚠️ 提交失败：" + html.escape(str(e)[:150])
                + "\n\n请再点一次「🚀 开始洗数据」。",
                reply_markup=menu_kb(cq.from_user.id),
            )
        except Exception:
            pass


async def _run_collision(cq: CallbackQuery, state: FSMContext, bot: Bot, files: list) -> None:
    """苹果iM撞库：逐文件查本地号码库，输出「原名_苹果_N.txt / 原名_安卓_N.txt」。"""
    from services.file_ops import decode_bytes, read_file, sanitize_filename
    from utils.phone_db import extract_phones

    user_id = cq.from_user.id

    # 1) 逐个文件读号码（每个文件单独出结果）
    batches: list[tuple[str, list[str]]] = []
    for f in files:
        name = f.get("name") or "input.txt"
        try:
            text = decode_bytes(read_file(f["path"]))
            phones = extract_phones(text)
        except Exception as e:
            print(f"[imdb] 读取失败 {name}：{e}")
            continue
        uniq = list(dict.fromkeys(p for p in phones if p))
        if uniq:
            batches.append((name, uniq))

    if not batches:
        await _edit_or_answer(
            cq, "⚠️ 文件里没有识别到手机号。", reply_markup=menu_kb(user_id)
        )
        await cleanup_state(state)
        return

    all_phones = [p for _name, lst in batches for p in lst]

    from database import lookup_phone_profiles

    with SessionLocal() as session:
        kinds = lookup_phone_profiles(session, all_phones)
        per_wan = float(get_wash_svc_prices(session).get("IMDB", 20.0) or 0)
        balance = get_wash_balance(session, user_id)

    apple_total = sum(1 for p in all_phones if p in kinds)
    android_total = len(all_phones) - apple_total
    # 按「提交的号码数」计费，命中与否同价
    submitted = len(all_phones)
    cost = round(per_wan * submitted / 10000.0, 4)

    if cost > 0 and balance < cost:
        await _edit_or_answer(
            cq,
            "⚠️ 余额不足！\n\n"
            f"提交号码：{submitted:,} 个\n"
            f"本次费用：¥{cost:.4f} 元\n当前余额：¥{balance:.4f} 元\n\n"
            "撞库按提交数量计费。",
            reply_markup=wash_main_kb(balance),
        )
        await cleanup_state(state)
        return

    if cost > 0:
        from database import WashOrder, deduct_wash_balance

        with SessionLocal() as session:
            if not deduct_wash_balance(session, user_id, cost):
                await _edit_or_answer(
                    cq, "⚠️ 扣费失败（余额不足），请充值后重试。",
                    reply_markup=wash_main_kb(balance),
                )
                await cleanup_state(state)
                return
            session.add(
                WashOrder(
                    user_id=user_id,
                    filename=f"苹果iM撞库 {len(batches)}个文件 {submitted}个号码",
                    row_count=submitted,
                    cost=cost,
                    type="deduct",
                )
            )
            session.commit()

    workdir = (await state.get_data()).get("workdir") or get_workdir(user_id)
    os.makedirs(workdir, exist_ok=True)

    # 2) 每个源文件出两个结果：原名_苹果_N / 原名_安卓_N
    for name, uniq in batches:
        stem = (sanitize_filename(os.path.splitext(name)[0]) or "result")[:50]
        apple = [p for p in uniq if p in kinds]
        android = [p for p in uniq if p not in kinds]
        for label, rows in (("苹果", apple), ("安卓", android)):
            if not rows:
                continue
            fname = f"{stem}_{label}_{len(rows)}.txt"
            path = os.path.join(workdir, fname)
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(rows))
                await bot.send_document(
                    chat_id=cq.message.chat.id,
                    document=FSInputFile(path, filename=fname),
                    caption=f"{label} {len(rows):,} 个",
                )
            except Exception as e:
                print(f"[imdb] 发送 {fname} 失败：{e}")

    total = len(all_phones)
    rate = (apple_total / total * 100) if total else 0
    await _edit_or_answer(
        cq,
        "🍎 <b>苹果iM撞库完成</b>\n\n"
        f"提交文件：{len(batches)} 个 / 号码 {total:,} 个\n"
        f"苹果：{apple_total:,} 个\n"
        f"安卓：{android_total:,} 个\n"
        f"苹果占比：{rate:.1f}%\n\n"
        f"费用：¥{cost:.4f}（按提交 {submitted:,} 个计）\n\n"
        "文件已按「原名_苹果_数量 / 原名_安卓_数量」发出。",
        reply_markup=menu_kb(user_id),
    )
    await cleanup_state(state)


# 导入必须串行：多个文件同时写同一张表会抢锁、拖死数据库
_IMPORT_LOCK: asyncio.Lock | None = None


def _import_lock() -> asyncio.Lock:
    global _IMPORT_LOCK
    if _IMPORT_LOCK is None:
        _IMPORT_LOCK = asyncio.Lock()
    return _IMPORT_LOCK


def _parse_phone_library(text: str) -> tuple[list, int]:
    """从文本里解析号码（全部按苹果），返回 (pairs, 无法识别行数)。"""
    from database import normalize_profile_phone

    pairs = []
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        phone = normalize_profile_phone(line)
        if not phone:
            bad += 1
            continue
        pairs.append((phone, "苹果"))
        if len(pairs) >= 5000000:
            break
    return pairs, bad


def _write_phone_library(pairs: list) -> int:
    """批量写库，返回提交行数。"""
    from database import bulk_add_phone_profiles

    written = 0
    for i in range(0, len(pairs), 20000):
        with SessionLocal() as session:
            written += bulk_add_phone_profiles(session, pairs[i : i + 20000])
        print(f"[imdb] 已写入 {written:,} / {len(pairs):,}", flush=True)
    return written


def _import_text_into_db(text: str) -> dict:
    """解析 + 写库（供链接导入等同步场景复用）。"""
    pairs, bad = _parse_phone_library(text)
    written = _write_phone_library(pairs)
    return {"rows": len(pairs), "bad": bad, "added": written, "updated": 0}


def _profile_total() -> int:
    from sqlalchemy import func

    from database import PhoneProfile

    try:
        with SessionLocal() as session:
            return int(
                session.scalar(select(func.count()).select_from(PhoneProfile)) or 0
            )
    except Exception:
        return 0


def _import_report(stat: dict) -> str:
    return (
        "✅ <b>导入完成</b>\n\n"
        f"有效行：{stat['rows']:,}\n"
        f"已写入：{stat['added']:,}（重复自动跳过）\n"
        f"无法识别：{stat['bad']:,}\n\n"
        f"库内号码总数：<b>{_profile_total():,}</b>"
    )


async def _import_from_url(message: Message, url: str) -> None:
    """从链接导入（不受 Telegram 20MB 限制）。"""
    import aiohttp
    from services.file_ops import decode_bytes, read_file

    max_bytes = 500 * 1024 * 1024
    workdir = get_workdir(message.from_user.id)
    path = os.path.join(workdir, "import_url.txt")
    tip = await message.answer("⏳ 正在从链接下载（上限 500MB）…")

    size = 0
    try:
        timeout = aiohttp.ClientTimeout(total=1800)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    await tip.edit_text(f"⚠️ 下载失败：HTTP {resp.status}")
                    return
                with open(path, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            await tip.edit_text("⚠️ 文件超过 500MB，请拆分后再导入。")
                            return
                        fh.write(chunk)
    except Exception as e:
        await tip.edit_text(f"⚠️ 下载失败：{str(e)[:150]}")
        return

    await tip.edit_text(
        f"⏳ 下载完成（{size / 1024 / 1024:.1f}MB），正在导入，请稍等…"
    )
    try:
        text = decode_bytes(read_file(path))
        stat = await asyncio.to_thread(_import_text_into_db, text)
    except Exception as e:
        await tip.edit_text(f"⚠️ 导入失败：{str(e)[:150]}")
        return

    await tip.edit_text(_import_report(stat), parse_mode="HTML")


@router.message(Command("导入库", "imdb_import"))
async def imdb_import_cmd(message: Message, state: FSMContext):
    """管理员：导入撞库号码库。支持直接发文件、或 /导入库 <链接>。"""
    from utils.roles import is_admin

    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg.startswith(("http://", "https://")):
        await _import_from_url(message, arg)
        return

    await state.set_state(AdminStates.imdb_import)
    await message.answer(
        "📥 <b>导入撞库号码库</b>\n\n"
        "库里的号码<b>全部当作苹果号</b>，每行一个号码即可（带其它字段也行）。\n\n"
        "<b>三种方式：</b>\n"
        "1️⃣ 直接发文件（≤19MB，Telegram 限制，正好 20.0MB 会传不上去）\n"
        "2️⃣ <code>/导入库 https://.../库.txt</code>（大文件走链接，≤500MB）\n"
        "3️⃣ 分多次发文件，发完发 <code>/完成导入</code>\n\n"
        "每行示例：\n"
        "<code>13800138000</code>\n"
        "<code>13800138000,蓝号</code>\n"
        "<code>+8613900139000</code>\n\n"
        "要清空整个库：<code>/清空库</code>（管理面板里也有「🗑 清空撞库库」按钮）",
        reply_markup=cancel_kb(),
    )


@router.message(Command("完成导入"))
async def imdb_import_done(message: Message, state: FSMContext):
    from utils.roles import is_admin

    if not is_admin(message.from_user.id):
        return
    if await state.get_state() == AdminStates.imdb_import.state:
        await cleanup_state(state)
    await message.answer(
        f"✅ 已结束导入。\n\n库内号码总数：<b>{_profile_total():,}</b>",
        parse_mode="HTML",
    )


# ---------------- 预撞库 / 自动入库 一键开关 ----------------

def _im_autosave_on() -> bool:
    """洗好的苹果是否自动入库（默认开启）。"""
    from database import get_config

    try:
        with SessionLocal() as session:
            raw = (get_config(session, "im_autosave", "1") or "").strip().lower()
    except Exception:
        return True
    return raw not in ("0", "false", "off", "no", "关", "关闭")


def _im_prewash_status_text(on: bool, autosave: bool | None = None) -> str:
    if autosave is None:
        autosave = _im_autosave_on()
    body = (
        "当前状态：<b>🟢 已开启</b>\n\n"
        "苹果 iMessage 洗数据会先用本地库拆成苹果 / 安卓：\n"
        "· 苹果号 → 送群洗，洗完发回\n"
        "· 安卓号 → 不送群，跟群里判定的安卓合成一份"
        " <code>原名_安卓_N.txt</code> 发回"
        if on
        else "当前状态：<b>🔴 已关闭</b>\n\n"
        "苹果 iMessage 洗数据会把整份文件直接送群洗，"
        "不预分苹果/安卓，也不会有单独的安卓结果。"
    )
    save_line = (
        "🟢 已开启：群里判定成苹果的号会自动写进本地号码库"
        if autosave
        else "🔴 已关闭：洗完的苹果不写库"
    )
    return (
        f"⚙️ <b>苹果 iM 预撞库</b>\n\n{body}\n\n点下面的按钮一键切换。\n\n"
        "────────────\n"
        f"🤖 <b>洗好的苹果自动入库</b>\n{save_line}"
    )


def _im_prewash_switch_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🟢 开启预撞库", callback_data="wash:im_prewash:on")
    b.button(text="🔴 关闭预撞库", callback_data="wash:im_prewash:off")
    b.button(text="🟢 开启自动入库", callback_data="wash:im_autosave:on")
    b.button(text="🔴 关闭自动入库", callback_data="wash:im_autosave:off")
    b.adjust(2, 2)
    return b.as_markup()


async def _show_im_prewash_switch(target, cq=None) -> None:
    with SessionLocal() as session:
        on = im_prewash.enabled(session)
    text = _im_prewash_status_text(on)
    markup = _im_prewash_switch_kb()
    if cq is not None:
        await _edit_or_answer(cq, text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


@router.message(Command("预撞库", "im_prewash"))
async def im_prewash_switch_cmd(message: Message):
    """管理员：查看/切换预撞库开关（也可以点管理面板那个按钮）。"""
    from utils.roles import is_admin

    if not is_admin(message.from_user.id):
        await message.answer(
            "⚠️ 这个命令只有管理员能用。\n"
            f"你的 Telegram ID：<code>{message.from_user.id}</code>",
            parse_mode="HTML",
        )
        return
    await _show_im_prewash_switch(message)


async def _set_im_prewash(on: bool) -> None:
    from database import set_config

    with SessionLocal() as session:
        set_config(session, "im_prewash", "1" if on else "0")


@router.callback_query(F.data.in_({"wash:im_prewash:on", "wash:im_prewash:off"}))
async def im_prewash_switch_callback(cq: CallbackQuery):
    from utils.roles import is_admin

    if not is_admin(cq.from_user.id):
        await cq.answer("没有权限。", show_alert=True)
        return
    on = (cq.data or "").endswith(":on")
    await _set_im_prewash(on)
    print(f"[im-pre] 管理员 {cq.from_user.id} {'开启' if on else '关闭'}预撞库")
    await cq.answer("已开启预撞库" if on else "已关闭预撞库")
    with SessionLocal() as session:
        still_on = im_prewash.enabled(session)
    await _edit_or_answer(
        cq, _im_prewash_status_text(still_on), reply_markup=_im_prewash_switch_kb()
    )


@router.callback_query(F.data.in_({"wash:im_autosave:on", "wash:im_autosave:off"}))
async def im_autosave_switch_callback(cq: CallbackQuery):
    """洗完的苹果要不要自动写进本地号码库。"""
    from database import set_config
    from utils.roles import is_admin

    if not is_admin(cq.from_user.id):
        await cq.answer("没有权限。", show_alert=True)
        return
    on = (cq.data or "").endswith(":on")
    with SessionLocal() as session:
        set_config(session, "im_autosave", "1" if on else "0")
        prewash_on = im_prewash.enabled(session)
    print(f"[im-lib] 管理员 {cq.from_user.id} {'开启' if on else '关闭'}自动入库")
    await cq.answer("已开启自动入库" if on else "已关闭自动入库")
    await _edit_or_answer(
        cq,
        _im_prewash_status_text(prewash_on, autosave=on),
        reply_markup=_im_prewash_switch_kb(),
    )


@router.callback_query(F.data == "admin:im_prewash")
async def im_prewash_toggle_from_panel(cq: CallbackQuery):
    """管理面板那个「🟢 预撞库：开」按钮：点一下直接切换，并刷新按钮文字。"""
    from utils.keyboards import admin_kb
    from utils.roles import is_admin, is_super_admin

    if not is_admin(cq.from_user.id):
        await cq.answer("没有权限。", show_alert=True)
        return
    with SessionLocal() as session:
        now_on = im_prewash.enabled(session)
    await _set_im_prewash(not now_on)
    print(
        f"[im-pre] 管理员 {cq.from_user.id} 面板切换预撞库："
        f"{'开→关' if now_on else '关→开'}"
    )
    await cq.answer("🔴 已关闭预撞库" if now_on else "🟢 已开启预撞库")
    try:
        await cq.message.edit_reply_markup(
            reply_markup=admin_kb(is_super=is_super_admin(cq.from_user.id))
        )
    except Exception as e:
        print(f"[im-pre] 刷新管理面板按钮失败：{e}")


# ---------------- 清空撞库号码库 ----------------

# 清空是重活（几千万行），同一时间只允许跑一个，避免连点触发两次 TRUNCATE
_IMDB_CLEAR_RUNNING = False


async def _imdb_clear_page() -> tuple:
    """号码库清空确认页（命令和管理面板共用同一份文案）。"""
    from database import count_phone_profiles

    def _read() -> dict:
        with SessionLocal() as session:
            return count_phone_profiles(session)

    # 3000 万行 count(*) 也要几秒，丢线程池，别把整个机器人卡住
    stat = await asyncio.to_thread(_read)
    if stat["total"] <= 0:
        return ("📭 撞库号码库现在是空的，不用清。", None)
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🗑 确认清空 {stat['total']:,} 条", callback_data="wash:imdb_clear:yes"
    )
    kb.button(text="❌ 取消", callback_data="cancel")
    kb.adjust(1)
    text = (
        "⚠️ <b>清空撞库号码库</b>\n\n"
        f"当前库内：<b>{stat['total']:,}</b> 条"
        f"（苹果 {stat['apple']:,} / 安卓 {stat['android']:,}）\n\n"
        "清空以后：\n"
        "· 「🍎 苹果iM撞库」查不到任何号，会全部按安卓出结果；\n"
        "· 「🍎 苹果 iMessage」洗数据会暂时跳过本地预撞库，整份文件送群洗。\n\n"
        "<b>删除不可恢复</b>，确认请点下面的按钮（要重新导入用 /导入库）。"
    )
    return (text, kb.as_markup())


async def _show_imdb_clear(target, cq: CallbackQuery | None = None) -> None:
    """把清空确认页发出去：命令走 message.answer，面板按钮走回调查询。"""
    text, markup = await _imdb_clear_page()
    if cq is not None:
        if markup is None:
            # 库是空的：弹一句就行，别把管理面板那条消息改成提示
            await cq.answer("撞库号码库现在是空的，不用清。", show_alert=True)
            return
        await _edit_or_answer(cq, text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


@router.message(Command("清空库", "imdb_clear", "清空撞库库"))
async def imdb_clear_cmd(message: Message, state: FSMContext):
    """管理员：清空撞库号码库（走二次确认，不可恢复）。"""
    from utils.roles import is_admin

    if not is_admin(message.from_user.id):
        await message.answer(
            "⚠️ 这个命令只有管理员能用。\n"
            f"你的 Telegram ID：<code>{message.from_user.id}</code>",
            parse_mode="HTML",
        )
        return
    # 正在导入文件时先结束那个会话，否则后续发的文件还会被当成导入
    if await state.get_state() == AdminStates.imdb_import.state:
        await cleanup_state(state)
    await _show_imdb_clear(message)


@router.callback_query(F.data == "admin:imdb_clear")
async def imdb_clear_from_panel(cq: CallbackQuery):
    from utils.roles import is_admin

    if not is_admin(cq.from_user.id):
        await cq.answer("没有权限。", show_alert=True)
        return
    await _show_imdb_clear(cq.message, cq)


async def _safe_cq_edit(cq: CallbackQuery, text: str) -> None:
    """编辑消息失败（消息被删/内容没变）不能影响主流程，也不能静默失败。"""
    try:
        await cq.message.edit_text(text)
    except Exception as e:
        print(f"[imdb] 编辑消息失败：{e}")


@router.callback_query(F.data == "wash:imdb_clear:yes")
async def imdb_clear_confirm(cq: CallbackQuery):
    """真正执行清空：只有点了确认按钮才会删。

    注意顺序：先 answer + 先把消息改成「正在清空」，再丢线程池去删。
    3000 万行删除可能要几十秒，如果先删再应答，Telegram 那边早就转圈超时、
    删除又卡住事件循环，用户看到的就是「点了没反应」。
    """
    global _IMDB_CLEAR_RUNNING

    from database import clear_phone_profiles
    from utils.roles import is_admin

    if not is_admin(cq.from_user.id):
        await cq.answer("没有权限。", show_alert=True)
        return
    if _IMDB_CLEAR_RUNNING:
        await cq.answer("已经在清空中，请稍等…", show_alert=True)
        return

    # ① 立刻应答，转圈马上停
    await cq.answer("正在清空…")
    _IMDB_CLEAR_RUNNING = True
    # ② 先把消息改成进行中：就算后面删很久，用户也知道在动
    await _safe_cq_edit(
        cq,
        "⏳ <b>正在清空撞库号码库…</b>\n\n"
        "库大的话要几十秒（几千万行走 TRUNCATE，一般是秒级），请勿重复点击。",
    )

    def _run_clear() -> int:
        # 线程里自己开 session：SQLAlchemy 的 session 不能跨线程共用
        with SessionLocal() as session:
            return clear_phone_profiles(session)

    try:
        removed = await asyncio.to_thread(_run_clear)
    except Exception as e:
        print(f"[imdb] 清空号码库失败：{e}")
        await _safe_cq_edit(
            cq,
            "⚠️ <b>清空失败</b>\n\n"
            f"错误：<code>{html.escape(str(e)[:200])}</code>\n\n"
            "数据没有被清掉（或只清了一部分），可以再点一次；"
            "一直失败把这条错误发给技术。",
        )
        return
    finally:
        _IMDB_CLEAR_RUNNING = False

    print(f"[imdb] 管理员 {cq.from_user.id} 清空号码库，删除 {removed:,} 条")
    await _safe_cq_edit(
        cq,
        f"🗑 <b>撞库号码库已清空</b>\n\n共删除 <b>{removed:,}</b> 条。\n\n"
        "现在「苹果iM撞库」没有可查的号；苹果 iMessage 洗数据会自动跳过"
        "本地预撞库（整份送群洗）。\n"
        "要重新导入用 /导入库。",
    )


@router.message(AdminStates.imdb_import, F.document)
async def imdb_import_file(message: Message, state: FSMContext, bot: Bot):
    from services.file_ops import decode_bytes, read_file, sanitize_filename
    from utils.roles import is_admin

    if not is_admin(message.from_user.id):
        return
    doc = message.document
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        await message.answer(
            "⚠️ 文件超过 19MB（Telegram 只给机器人下载 20MB 以内），改用链接方式：\n"
            "把文件传到任意直链后发 <code>/导入库 https://...</code>",
            parse_mode="HTML",
        )
        return

    workdir = get_workdir(message.from_user.id)
    name = sanitize_filename(doc.file_name or "import.txt")
    path = os.path.join(workdir, name)
    size_mb = (doc.file_size or 0) / 1024 / 1024
    lock = _import_lock()
    if lock.locked():
        tip = await message.answer(
            f"⏳ 已有文件在导入，这个（{size_mb:.1f}MB）排队等待…"
        )
    else:
        tip = await message.answer(f"⏳ 开始下载（{size_mb:.1f}MB）…")

    async with lock:
        await _do_import_file(message, bot, tip, doc, path, size_mb)


async def _do_import_file(message, bot, tip, doc, path, size_mb) -> None:
    """真正的导入逻辑，由 _import_lock 保证同一时间只跑一个。"""
    from services.file_ops import decode_bytes, read_file

    name = os.path.basename(path)


    def _say(text: str) -> None:
        """尽量编辑提示消息；失败就另发一条，保证用户看得到。"""
        async def _run():
            try:
                await tip.edit_text(text, parse_mode="HTML")
            except Exception:
                try:
                    await message.answer(text, parse_mode="HTML")
                except Exception:
                    pass
        return _run()

    try:
        print(f"[imdb] 下载开始 {name} {size_mb:.1f}MB", flush=True)
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=path, timeout=600)
        text = decode_bytes(read_file(path))
        print(f"[imdb] 下载完成 {name}", flush=True)
    except Exception as e:
        print(f"[imdb] 下载失败 {name}: {e}", flush=True)
        await _say(f"⚠️ 下载失败：{str(e)[:150]}")
        return

    await _say(
        f"⏳ 下载完成（{size_mb:.1f}MB），正在解析号码…"
    )
    try:
        pairs, bad = await asyncio.to_thread(_parse_phone_library, text)
    except Exception as e:
        print(f"[imdb] 解析失败: {e}", flush=True)
        await _say(f"⚠️ 解析失败：{str(e)[:150]}")
        return

    if not pairs:
        await _say("⚠️ 这个文件里没有识别到手机号。")
        return

    await _say(
        f"⏳ 解析到 {len(pairs):,} 个号码，正在写入数据库（可能需要 1~5 分钟）…"
    )
    try:
        written = await asyncio.to_thread(_write_phone_library, pairs)
    except Exception as e:
        print(f"[imdb] 写库失败: {e}", flush=True)
        await _say(f"⚠️ 写库失败：{str(e)[:150]}")
        return

    await _say(
        _import_report(
            {"rows": len(pairs), "bad": bad, "added": written, "updated": 0}
        )
        + "\n\n（可以继续发文件；全部发完发 <code>/完成导入</code>）"
    )


async def _send_android_only(
    cq: CallbackQuery, state: FSMContext, bot: Bot, prewash: dict
) -> None:
    """整批都不是苹果号：不送群洗（不产生费用），把结果直接发回给用户。

    给客户看的话术不能出现「撞库 / 预分 / 送群」这些内部词，
    否则一眼就看出来机器人背后做了本地库比对。
    """
    user_id = cq.from_user.id
    sent = 0
    for f in prewash.get("android_files") or []:
        try:
            await bot.send_document(
                chat_id=cq.message.chat.id,
                document=FSInputFile(f["path"], filename=f["name"]),
                caption=f"✅ 你的洗数据已完成！\n原文件：{f['name']}",
            )
            sent += 1
        except Exception as e:
            print(f"[im-pre] 发送安卓文件失败 {f['name']}：{e}")

    stats = prewash["stats"]
    warn = "" if sent else "\n⚠️ 文件发送失败，请重新提交；如果一直失败请联系管理员。"
    await _edit_or_answer(
        cq,
        "🍎 <b>苹果 iMessage</b>\n\n"
        f"本批共 {stats['phones']:,} 个号码，处理完成。\n"
        "这批里没有可用的苹果号，<b>本次没有扣费</b>。\n\n"
        f"结果已按「原名_安卓_数量」发出（{sent} 个文件）。"
        + warn,
        reply_markup=menu_kb(user_id),
    )
    await cleanup_state(state)


async def _wash_submit_impl(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    svc_code: str = "im",
    svc_label: str = "🍎 苹果 iMessage",
) -> None:
    """提交整批文件：按总行数计费（档位越高单价越低），预扣费后转发到洗数据群。"""
    data = await state.get_data()
    files = list(data.get("files") or [])
    if not files:
        # 常见原因：机器人刚重启，内存里的上传状态丢了
        await cq.answer(
            "没找到上传的文件（可能是机器人刚重启）。\n请重新发一次文件再点开始洗数据。",
            show_alert=True,
        )
        return

    # 防重复提交：预撞库要读一遍文件，提交比原来慢，
    # 用户连点两次「确认扣费」会重复扣两次钱 —— 这段时间内直接拒掉。
    # 用时间戳而不是布尔标记：万一中途抛异常也不会把人永久锁住。
    import time as _time

    if _time.time() < float(data.get("submitting_until") or 0):
        await cq.answer(
            "这批文件正在提交中，请稍等，不要重复点击。", show_alert=True
        )
        return
    await state.update_data(submitting_until=_time.time() + 120)

    await _stop_upload_render(cq.from_user.id)

    # 苹果iM撞库：只查本地号码库，不经过小号，所以不走后面的转发流程
    if svc_code == "IMDB":
        await _run_collision(cq, state, bot, files)
        return

    # 小号没连上时直接拒绝提交。
    # 原因：bot.send_document 走的是 Bot API，小号在不在线都会成功，
    # 但这时没有任何客户端在监听，文件不会被转发到洗数据群 ——
    # 用户会看到「提交成功」，钱也扣了，实际却一点没洗。
    from utils import telethon_client as _tc

    if not (_tc.userbot and _tc.userbot.is_connected()):
        await cq.answer(
            "洗数据小号还没连上（正在重连或未登录），暂时不能提交。\n"
            "请等 1~2 分钟后再点「🚀 开始洗数据」；\n"
            "还没恢复的话，去「小号状态」点「🔑 强制接管并重连」。",
            show_alert=True,
        )
        return

    total_lines = sum(f["lines"] for f in files)
    total_phones = sum(f["phones"] for f in files)

    # 行数和手机号数量不一致，说明文件里不只有手机号
    if total_lines != total_phones:
        await _edit_or_answer(
            cq,
            f"⚠️ 文件里不只有手机号！\n\n"
            f"文件总行数：{total_lines:,} 行\n"
            f"提取到手机号：{total_phones:,} 个\n\n"
            "请先用「提取手机号」功能，把文件里的手机号提取出来，"
            "再用提取后的文件来洗数据。",
            reply_markup=menu_kb(cq.from_user.id),
        )
        await cleanup_state(state)
        return

    # 🍎 苹果 iMessage：先本地撞库拆分 —— 只把苹果号送去群洗，安卓号留在本地。
    # 安卓号送群洗是白花钱（本地库已经能判掉），而且用户要的苹果结果
    # 跟安卓结果本来就分开发；撞库安卓等群洗回来后一起合成「安卓合计」。
    submit_files = files
    prewash = None
    input_lines = total_lines  # 上传的原始行数（收费口径的基准）
    if svc_code == "im":
        with SessionLocal() as session:
            do_prewash = im_prewash.enabled(session)
            # 本地库一个苹果号都没有时不能预分：那样会把整批都判成安卓、
            # 免费放走而且根本送不进群。这种情况退回「整批送群洗」的老流程。
            if do_prewash and im_prewash.library_size(session) <= 0:
                do_prewash = False
                print("[im-pre] 本地库没有苹果号，本次跳过预撞库（整批送群洗）")
        if do_prewash:
            workdir = (await state.get_data()).get("workdir") or get_workdir(
                cq.from_user.id
            )
            try:
                prewash = await asyncio.to_thread(
                    im_prewash.split_files, files, workdir
                )
            except Exception as e:
                print(f"[im-pre] 本地撞库预处理失败：{e}")
                await _edit_or_answer(
                    cq,
                    "⚠️ 文件预处理失败，这次<b>没有扣费</b>。\n\n"
                    "请重新点「🚀 开始洗数据」再试一次；还是不行就重新发一次文件。",
                    reply_markup=menu_kb(cq.from_user.id),
                )
                await cleanup_state(state)
                return

            stats = prewash["stats"]
            print(
                f"[im-pre] 拆分完成：输入 {stats['phones']:,} · "
                f"苹果 {stats['apple']:,} · 安卓 {stats['android']:,}"
            )
            if not prewash["apple_files"]:
                if stats["phones"] <= 0:
                    # 空文件/全是认不出的内容：没什么可洗的，别让用户以为在排队
                    await _edit_or_answer(
                        cq,
                        "⚠️ 这批文件里没有识别到手机号，没有可洗的数据。\n\n"
                        "请确认文件是一行一个手机号（可用「提取手机号」先整理一遍），"
                        "再重新上传。",
                        reply_markup=menu_kb(cq.from_user.id),
                    )
                    await cleanup_state(state)
                    return
                # 一个苹果号都没有：不用送群洗，也就没有费用
                await _send_android_only(cq, state, bot, prewash)
                return
            submit_files = prewash["apple_files"]
            # 收费按正常口径：用户上传多少行就收多少行（安卓号照样占行数，
            # 不因为只把苹果送去群洗而降档/少收）。
            total_lines = input_lines
            # 整份都是安卓、没送去群洗的那个文件，行数摊到第一个送群文件上：
            # 这样每个结果文件的分摊比例加起来仍然等于总行数，
            # 钱一分不少、配额也按真实行数累计。
            submitted_bill = sum(
                int(f.get("bill_lines") or f["lines"]) for f in submit_files
            )
            gap = max(0, total_lines - submitted_bill)
            if gap and submit_files:
                first = submit_files[0]
                first["bill_lines"] = int(
                    first.get("bill_lines") or first["lines"]
                ) + gap

    # 检查累计洗数据配额
    with SessionLocal() as session:
        quota = get_wash_quota(session)
        washed = get_washed_total(session)
    if quota > 0 and washed + total_lines > quota:
        await _edit_or_answer(
            cq,
            f"⚠️ 已达累计洗数据上限，无法继续洗！\n\n"
            f"累计已洗：{washed:,} 行\n"
            f"本次文件：{total_lines:,} 行\n"
            f"总配额：{quota:,} 行（剩余 {max(0, quota - washed):,} 行）\n\n"
            "超出配额的部分暂不能洗，请联系管理员。",
            reply_markup=menu_kb(cq.from_user.id),
        )
        await cleanup_state(state)
        return

    # 按「所选类型」计费：im 走阶梯价，其它类型按元/万行
    with SessionLocal() as session:
        cost = calc_wash_service_cost(session, svc_code, total_lines)
        balance = get_wash_balance(session, cq.from_user.id)
    if balance < cost:
        await _edit_or_answer(
            cq,
            f"⚠️ 余额不足！\n\n"
            f"总行数：{total_lines:,} 行\n"
            f"本次费用：¥{cost:.4f} 元\n"
            f"当前余额：¥{balance:.4f} 元\n"
            f"还差：¥{cost - balance:.4f} 元\n\n"
            "请先充值。",
            reply_markup=wash_main_kb(balance),
        )
        await cleanup_state(state)
        return

    # 提交时预扣费用：防止并发任务透支余额；发送失败会退回
    with SessionLocal() as session:
        from database import deduct_wash_balance
        if not deduct_wash_balance(session, cq.from_user.id, cost):
            await _edit_or_answer(
                cq,
                f"⚠️ 余额不足！\n\n本次费用：¥{cost:.4f} 元\n当前余额：¥{balance:.4f} 元\n\n请先充值。",
                reply_markup=wash_main_kb(balance),
            )
            await cleanup_state(state)
            return

    user_id = cq.from_user.id

    # 生成7位随机码（字母+数字），保护用户隐私；整批文件共用同一个随机码
    random_code = gen_wash_code()

    with SessionLocal() as session:
        from database import set_config
        import time
        # 批量任务：把每个文件的行数存进 pending（第 5 段 JSON），
        # 回传时按“收到几个结果结算几份”，一直没回传的部分超时按比例退款。
        # 行数用计费口径（bill_lines = 源文件原始行数），退款才跟收费对得上。
        batch_map = json.dumps(
            {
                "files": [int(f.get("bill_lines") or f["lines"]) for f in submit_files],
                "charged": 0.0,
            },
            ensure_ascii=False,
        )
        set_config(
            session,
            f"wash_pending:{random_code}",
            f"{user_id}:{total_lines}:{cost}:{int(time.time())}:{batch_map}",
        )
        set_config(
            session,
            f"wash_code:{random_code}",
            f"{user_id}:{int(time.time())}",
        )

    # 读取小号信息
    with SessionLocal() as session:
        wash_chat_id = get_config(session, "wash_chat_id", "")
        alt_username = get_config(session, "alt_username", "")
        alt_user_id = get_config(session, "alt_user_id", "")
        alt_session = get_config(session, "alt_session", "")

    if (not alt_username or not alt_user_id) and alt_session:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            from config import settings as _settings
            temp_client = TelegramClient(StringSession(alt_session), _settings.TELETHON_API_ID, _settings.TELETHON_API_HASH)
            await temp_client.connect()
            me = await temp_client.get_me()
            alt_username = me.username or ""
            alt_user_id = str(me.id)
            with SessionLocal() as session:
                from database import set_config
                set_config(session, "alt_username", alt_username)
                set_config(session, "alt_user_id", alt_user_id)
            await temp_client.disconnect()
        except Exception as e:
            _refund_full_batch(user_id, total_lines, cost, random_code, "获取小号信息失败退款")
            await _edit_or_answer(cq, f"⚠️ 获取小号信息失败：{str(e)[:100]}\n\n费用已原路退回。")
            await cleanup_state(state)
            return

    if not alt_user_id:
        _refund_full_batch(user_id, total_lines, cost, random_code, "小号未登录退款")
        await _edit_or_answer(cq, "⚠️ 小号未登录，请先在管理面板登录小号。\n\n费用已原路退回。")
        await cleanup_state(state)
        return

    # 逐个转发给小号（文件名加随机码前缀，回传时靠它识别）
    sent_ok = 0
    send_error = None
    sent_names = []
    try:
        for f in submit_files:
            new_filename = f"{random_code}_{f['name']}"
            await bot.send_document(
                chat_id=int(alt_user_id),
                document=FSInputFile(f["path"], filename=new_filename),
                caption=svc_code,
            )
            sent_ok += 1
            sent_names.append(new_filename)
            await asyncio.sleep(0.6)  # 连发多个文件，稍作间隔避免限流
        # 记下我们自己发出的源文件名：补发扫描要跳过它们（否则会把原文件当结果再发一遍）
        if sent_names:
            with SessionLocal() as session:
                from database import set_config
                set_config(
                    session,
                    f"wash_src:{random_code}",
                    json.dumps(sent_names, ensure_ascii=False),
                )
    except Exception as e:
        send_error = str(e)
        if sent_ok == 0:
            # 一个都没发出去：整单退款并作废
            _refund_full_batch(user_id, total_lines, cost, random_code, "发送失败退款")
            if "chat not found" in send_error.lower():
                tip = (
                    "⚠️ 小号还没和我们的机器人建立会话！\n\n"
                    "请用小号给我们的机器人发一条消息（比如发个 /start），然后再试。"
                )
            else:
                tip = f"⚠️ 转发给小号失败：{send_error[:100]}"
            await _edit_or_answer(cq, f"{tip}\n\n费用已原路退回。", reply_markup=menu_kb(user_id))
            await cleanup_state(state)
            return
        # 部分已发出：只退回未发出部分的费用，已发出的继续等回传结算
        sent_files = submit_files[:sent_ok]
        unsent_files = submit_files[sent_ok:]
        sent_total = sum(int(f.get("bill_lines") or f["lines"]) for f in sent_files)
        unsent_lines = sum(int(f.get("bill_lines") or f["lines"]) for f in unsent_files)
        refund = round(cost * unsent_lines / total_lines, 4)
        remain_cost = round(cost - refund, 4)
        with SessionLocal() as session:
            from database import WashOrder, add_wash_balance, set_config
            add_wash_balance(session, user_id, refund)
            session.add(
                WashOrder(
                    user_id=user_id,
                    filename="部分发送失败退款",
                    row_count=-unsent_lines,
                    cost=-refund,
                    type="refund",
                )
            )
            # 更新 pending：行数与费用都改为「已发出部分」，结算按已发出文件的比例分摊
            remain_map = json.dumps(
                {
                    "files": [
                        int(f.get("bill_lines") or f["lines"]) for f in sent_files
                    ],
                    "charged": 0.0,
                },
                ensure_ascii=False,
            )
            set_config(
                session,
                f"wash_pending:{random_code}",
                f"{user_id}:{sent_total}:{remain_cost}:{int(time.time())}:{remain_map}",
            )
            session.commit()
        await _edit_or_answer(
            cq,
            f"⚠️ 转发到第 {sent_ok + 1} 个文件时失败：{send_error[:100]}\n\n"
            f"已发出 <b>{sent_ok}</b> 个文件（继续处理，完成后回传）；\n"
            f"未发出的 <b>{len(unsent_files)}</b> 个文件已退回 ¥{refund:.4f}。",
            reply_markup=menu_kb(user_id),
        )
        await cleanup_state(state)
        return

    # 本地撞库的安卓先存下来：群洗结果回来以后要跟群里判定的安卓相加
    if prewash:
        ok_state = await asyncio.to_thread(
            im_prewash.save_state,
            random_code,
            user_id,
            im_prewash.file_stem(files[0].get("name") or "result"),
            prewash["android"],
            sent_ok,
        )
        stats = prewash["stats"]
        print(
            f"[im-pre] {random_code} 撞库安卓 {stats['android']:,} 个，"
            f"状态{'已登记' if ok_state else '登记失败'}"
        )

    # 文案保持原样（用户要求别在提交成功的消息里加撞库解释）
    await _edit_or_answer(
        cq,
        f"✅ <b>已提交洗数据</b>\n\n"
        f"文件数：{sent_ok} 个\n"
        f"总行数：{total_lines:,} 行\n"
        f"扣除费用：¥{cost:.4f} 元\n"
        f"剩余余额：¥{balance - cost:.4f} 元\n\n"
        "────────────\n"
        "正在处理中，完成后会自动把结果发回给你，请稍等。",
        reply_markup=menu_kb(user_id),
    )
    # 单独发一条进度消息，后面每回传一个文件就编辑它
    try:
        prog_msg = await cq.message.answer(
            f"⏳ <b>洗数据处理中</b>\n\n进度：<b>0/{sent_ok}</b> 请耐心等待。"
        )
        _progress_begin(random_code, sent_ok, prog_msg.chat.id, prog_msg.message_id)
    except Exception as e:
        print(f"[wash-progress] 发送进度消息失败: {e}")
    # 记录本批输入数据，全部回传后用来出统计报告
    # 「号码总数」按用户上传的号码算：报告里行数和号码数必须对得上，
    # 否则客户会追问「我传了 6 行怎么只有 2 个号」，等于把内部拆分暴露出去。
    _report_begin(
        random_code,
        sent_ok,
        total_lines,
        total_phones if prewash else sum(int(f.get("phones") or 0) for f in submit_files),
        cost,
        svc_label,
    )
    await cleanup_state(state)


# ---------------- 监听小号回传的所有文件，判断是洗数据还是空号检测 ----------------

@wash_reply_router.message(F.document, FromAltUserFilter(), KnownResultCodeFilter())
async def alt_user_reply(message: Message, bot: Bot):
    """监听小号发回的文件，判断是洗数据还是空号检测，分别处理。"""
    doc = message.document
    if not doc:
        return

    import re
    from database import get_config
    filename = doc.file_name or "unnamed"
    caption = message.caption or ""
    search_text = filename + " " + caption
    print(f"📥 收到小号回传文件: 文件名={filename}, caption={caption[:50]}")

    # 判断是不是空号检测的结果
    m_phone = re.search(r"([a-zA-Z0-9]{7})_号码检测", search_text)
    if m_phone:
        # 空号检测的处理
        random_code = m_phone.group(1)
        print(f"📱 空号检测结果: 随机码={random_code}")

        # 只发带分类的结果文件，不发源文件
        if not any(keyword in filename for keyword in ["活跃号", "空号", "沉默号", "风险号"]):
            print(f"⚠️ 这不是结果文件，跳过: {filename}")
            return

        # 从数据库查随机码对应的用户ID
        with SessionLocal() as session:
            user_id_str = get_config(session, f"phone_check_code:{random_code}", "")

        if not user_id_str:
            print(f"⚠️ 随机码 {random_code} 没找到对应的用户")
            await _alert_unmatched(bot, filename, random_code)
            return

        user_id = int(user_id_str)

        ok = await _send_document_retry(
            bot,
            user_id,
            doc.file_id,
            f"✅ 你的空号检测已完成！\n文件：{filename}",
        )
        if ok:
            print(f"✅ 已把空号检测结果发给用户 {user_id}")
        else:
            await _alert_undelivered(bot, filename, random_code, user_id)
        return

    # 判断是不是洗数据的结果
    m_wash = re.search(r"([a-zA-Z0-9]{7})_", search_text)
    print(f"🔍 洗数据判断: 匹配到={m_wash}, search_text={search_text[:100]}")
    if m_wash:
        # 洗数据的处理
        random_code = m_wash.group(1)
        print(f"🧺 洗数据结果: 随机码={random_code}")

        with SessionLocal() as session:
            user_id_str = get_config(session, f"wash_code:{random_code}", "")
        if not user_id_str:
            print(f"⚠️ 洗数据随机码 {random_code} 没找到对应的用户")
            await _alert_unmatched(bot, filename, random_code)
            return
        # 兼容两种格式：旧格式纯 user_id，新格式 "user_id:时间戳"
        user_id = int(user_id_str.split(":")[0])

        # 提取原文件名：只取文件名本身，不要把消息 caption（如“蓝号(IOS/imessage/FaceTime)”）拼进去
        original_name = _washed_original_name(filename)

        # 这份是「洗好的苹果」还是「群里判定不是苹果的」？
        # 群里的结果名常把两种数量都写上（原名-苹果-285-安卓-205.txt），
        # 这时光看名字分不出装的是哪一半，得先数行数，再按 285/205 对号入座。
        phones = None
        line_count = None
        if im_prewash.needs_count_probe(filename):
            phones = await _download_result_phones(bot, doc)
            line_count = len(phones) or None
        is_android = im_prewash.is_android_result(filename, caption, line_count)

        # ① 先把文件交给用户：这是用户唯一关心的结果。
        #    记账放在后面，记账只要出一点问题都不能影响交付（以前是先记账后发送，
        #    记账一抛异常就整条 update 被全局错误处理器吞掉，用户永远收不到文件）。
        dkey = _delivery_key(doc, filename)
        # 补发扫描用的是「文件名|大小」，这里必须同时记一份，否则实时发过的文件
        # 在补发扫描里查不到（老版本就是这样把同一个文件又发了一遍）。
        dkey_size = _delivery_key_size(filename, getattr(doc, "file_size", 0))
        if _already_delivered(random_code, dkey) or _already_delivered(
            random_code, dkey_size
        ):
            print(f"⚠️ 这个结果文件已经发过了，跳过重复投递: {filename}")
            return

        if is_android:
            # 安卓那份不发原文件：用户要的是「撞库安卓 + 洗完安卓」合成的一份。
            # 号码在这里记下来，等结果齐了统一发「安卓合计」。
            if phones is None:
                phones = await _download_result_phones(bot, doc)
            if phones:
                try:
                    if not im_prewash.state_exists(random_code):
                        # 没走预撞库（本地库没苹果号/开关关了）：也照样把这份发回去
                        im_prewash.ensure_state(
                            random_code,
                            user_id,
                            im_prewash.clean_stem(original_name),
                        )
                    added = im_prewash.add_washed_android(random_code, phones)
                    print(f"[im-pre] {random_code} 洗完安卓 +{added} 个（{filename}）")
                    _schedule_im_finalize(bot, random_code)
                except Exception as e:
                    print(f"[im-pre] 记录洗完安卓失败：{e}")
            else:
                # 下载失败（拿不到号码）时宁可退回老行为，也不能把用户的数据丢了
                print(f"[im-pre] 安卓结果下载失败，改为原样转发: {filename}")
                is_android = False

        if not is_android:
            ok = await _send_document_retry(
                bot,
                user_id,
                doc.file_id,
                f"✅ 你的洗数据已完成！\n原文件：{html.escape(original_name)}",
            )
            if not ok:
                # 没送到用户手上：不记账（不扣钱），通知管理员人工补发
                await _alert_undelivered(bot, filename, random_code, user_id)
                return
            print(f"✅ 已把洗好的苹果发给用户 {user_id}")
            # 洗完的苹果自动入库：下次别人送来同样的号，本地撞库直接命中
            await _remember_washed_apples(bot, doc, random_code, phones)
        else:
            print(f"🧮 安卓那份先收着，等结果齐了合成「安卓合计」再发: {filename}")

        _remember_delivery(random_code, dkey)
        _remember_delivery(random_code, dkey_size)

        _progress_mark(random_code, dkey)
        await _progress_tick(bot, random_code)

        # ② 送达后再记账（费用提交时已预扣，这里只记流水，不重复扣费）
        try:
            settle_cost, settle_lines, done = await _settle_wash_result(
                random_code, user_id, original_name
            )
            print(f"💳 结算：{settle_lines} 行 / ¥{settle_cost}，本任务{'已结清' if done else '还有未回传部分'}")
        except Exception as e:
            print(f"[wash] settle failed for {random_code}: {e}")
            admin_ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        "⚠️ <b>洗数据结算失败</b>\n\n"
                        f"用户：<code>{user_id}</code>\n"
                        f"随机码：<code>{random_code}</code>\n"
                        f"文件：<code>{html.escape(filename)}</code>\n"
                        f"错误：<code>{html.escape(str(e)[:200])}</code>\n\n"
                        "文件已发给用户，只有账单没记上，请手动核对。",
                    )
                except Exception:
                    pass

        # 洗数据完成，判定隐藏掉落（2%，不限制频率）
        try:
            from utils.drops import notify_drop_chat, roll_drop
            with SessionLocal() as session:
                drop_days = roll_drop(session, user_id, "洗数据", is_wash=True)
            if drop_days:
                await notify_drop_chat(bot, user_id, "洗数据", drop_days)
        except Exception:
            pass
        return


@router.message(Command("resend", "补发"))
async def wash_resend_cmd(message: Message, bot: Bot):
    """管理员手动补发：/resend [随机码]

    不带随机码：立刻扫一轮洗数据群，把漏掉的结果文件补发，并回报扫描情况；
    带随机码：只处理这个任务。
    任何情况下都会回复（不是管理员也会说明原因），不会静默无响应。
    """
    from utils.roles import is_admin
    from utils import wash_resend as rs

    if not is_admin(message.from_user.id):
        await message.answer(
            "⚠️ 这个命令只有管理员能用。\n"
            f"你的 Telegram ID：<code>{message.from_user.id}</code>"
        )
        return

    parts = (message.text or "").split(maxsplit=1)
    only = parts[1].strip() if len(parts) > 1 else ""
    try:
        notice = await message.answer("🔍 正在扫描洗数据群…")
    except Exception:
        notice = None
    try:
        # 不带随机码 = 只检查、不发送（避免把已经发过的历史文件再发一遍）；
        # 带随机码 = 真的补发这个任务，不要求任务记录还在
        stat = await rs._scan(
            bot,
            only_code=only or None,
            require_pending=not bool(only),
            dry_run=not bool(only),
        )
        text = rs.format_report(stat)
    except Exception as e:
        text = f"⚠️ 扫描失败：<code>{html.escape(str(e)[:300])}</code>"
    if notice is not None:
        try:
            await notice.edit_text(text)
            return
        except Exception:
            pass
    await message.answer(text)


# 也挂到最先注册的回传路由上：确保 /resend 不会被任何其它路由抢先吃掉
wash_reply_router.message.register(wash_resend_cmd, Command("resend", "补发"))


# ---------------- 兜底：文件没被任何功能接住 ----------------
# 放在 wash.router 末尾（wash 是最先注册回传、最后注册普通路由的），
# 只有在所有功能的状态处理器都没匹配时才会走到这里。
# 典型场景：点了「上传文件」后机器人重启，内存里的上传状态丢失，
# 用户再发文件就没人接——以前是完全没反应，现在给出明确提示。

@router.message(F.document)
async def document_fallback(message: Message, state: FSMContext):
    await cleanup_state(state)
    await message.answer(
        "⚠️ <b>没认出这个文件要做什么</b>\n\n"
        "可能是刚才机器人重启，上传状态丢了。\n"
        "请从主菜单重新选择功能，再发文件：\n\n"
        "· 洗数据：主菜单 → 🧼 洗数据 → 📤 上传文件洗数据\n"
        "· 其它功能：主菜单里点对应按钮，再发文件",
        reply_markup=menu_kb(message.from_user.id),
    )
