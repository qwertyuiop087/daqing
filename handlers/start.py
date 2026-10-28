"""入口：/start、主菜单、取消操作。"""
import json
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageEntity

from config import settings
from database import DEFAULT_WELCOME_TEXT, SessionLocal, get_config, get_or_create_user
from utils.helpers import cleanup_state
from utils.keyboards import main_menu_keyboard
from utils.roles import is_admin

router = Router(name="start")


@router.message(Command("version"))
async def cmd_version(message: Message):
    """显示当前部署版本，用来确认 Railway 上跑的是不是最新包。"""
    await message.answer(
        "🤖 <b>版本信息</b>\n\n"
        f"当前版本：<code>{settings.VERSION}</code>\n\n"
        "本版本修复：\n"
        "• 洗数据多结果文件（苹果/安卓）全部转发\n"
        "• 结果提示语不再混入群消息 caption\n"
        "• 合并文本支持自定义导出文件名",
    )


def utf16_len(s: str) -> int:
    """计算字符串在 UTF-16 下的长度（Telegram entities 偏移用的就是这个）"""
    return len(s.encode('utf-16-le')) // 2


def get_home_text(user_name: str = "") -> tuple[str, list | None]:
    """从数据库读欢迎语，没配置就用默认值。自动替换 {昵称} 占位符，同步调整 entities 偏移量。"""
    with SessionLocal() as session:
        text = get_config(session, "welcome_text", DEFAULT_WELCOME_TEXT)
        entities_raw = get_config(session, "welcome_entities", "")

    if not user_name:
        return text, get_home_entities()

    # 替换占位符
    placeholder = "{昵称}"
    if placeholder not in text:
        placeholder = "{name}"
        if placeholder not in text:
            return text, get_home_entities()

    # 计算替换后文本和 entities 的偏移量（用 UTF-16 长度，Telegram 标准）
    new_text = text.replace(placeholder, user_name)
    old_len = utf16_len(placeholder)
    new_len = utf16_len(user_name)
    offset_diff = new_len - old_len
    placeholder_pos = utf16_len(text[:text.find(placeholder)])

    # 解析 entities 并调整偏移量
    entities = None
    if entities_raw:
        import json
        from aiogram.types import MessageEntity
        try:
            data = json.loads(entities_raw)
            entities = []
            for item in data:
                offset = item["offset"]
                length = item["length"]
                # 如果占位符在这个 entity 前面，调整 offset
                if offset > placeholder_pos:
                    offset += offset_diff
                entities.append(MessageEntity(
                    type=item["type"],
                    offset=offset,
                    length=length,
                    url=item.get("url"),
                    language=item.get("language"),
                    custom_emoji_id=item.get("custom_emoji_id"),
                ))
        except Exception:
            entities = None

    return new_text, entities


def get_home_entities() -> list[MessageEntity] | None:
    """从数据库读欢迎语的 entities（包括会员表情）。"""
    with SessionLocal() as session:
        raw = get_config(session, "welcome_entities", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        entities = []
        for item in data:
            entities.append(MessageEntity(
                type=item["type"],
                offset=item["offset"],
                length=item["length"],
                url=item.get("url"),
                language=item.get("language"),
                custom_emoji_id=item.get("custom_emoji_id"),
            ))
        return entities if entities else None
    except Exception:
        return None


@router.message(CommandStart())
async def cmd_start(message: Message):
    with SessionLocal() as session:
        get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or "",
        )
    user_is_admin = is_admin(message.from_user.id)
    user_name = message.from_user.first_name or message.from_user.username or "朋友"
    home_text, home_entities = get_home_text(user_name)
    await message.answer(
        home_text,
        reply_markup=main_menu_keyboard(user_is_admin),
        entities=home_entities,
        parse_mode=None if home_entities else "HTML",
    )


@router.callback_query(F.data == "menu:home")
async def cb_home(cq: CallbackQuery, state: FSMContext):
    await cleanup_state(state)
    await cq.answer()
    user_name = cq.from_user.first_name or cq.from_user.username or "朋友"
    home_text, home_entities = get_home_text(user_name)
    await cq.message.edit_text(
        home_text,
        reply_markup=main_menu_keyboard(is_admin(cq.from_user.id)),
        entities=home_entities,
        parse_mode=None if home_entities else "HTML",
    )


@router.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery, state: FSMContext):
    await cleanup_state(state)
    await cq.answer("已取消")
    user_name = cq.from_user.first_name or cq.from_user.username or "朋友"
    home_text, home_entities = get_home_text(user_name)
    await cq.message.edit_text(
        home_text,
        reply_markup=main_menu_keyboard(is_admin(cq.from_user.id)),
        entities=home_entities,
        parse_mode=None if home_entities else "HTML",
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await cleanup_state(state)
    await message.answer(
        "已取消当前操作。",
        reply_markup=main_menu_keyboard(is_admin(message.from_user.id)),
        parse_mode="HTML",
    )
