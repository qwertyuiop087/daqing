"""共享键盘（内联按钮）。"""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.roles import is_admin

# 按钮图标内存缓存：启动时读一次，设置时更新，不再每次查数据库
_btn_icons_cache: dict[str, str] = {}
_btn_styles_cache: dict[str, str] = {}
_icons_loaded: bool = False

# 默认按钮颜色方案
DEFAULT_BTN_STYLES = {
    "menu:split": "primary",       # 文件切割 - 蓝（主要功能）
    "menu:zip": None,               # 解压Zip - 默认
    "menu:rename": None,           # 批量重命名 - 默认
    "menu:vcf": None,              # 号码转VCF - 默认
    "menu:dedup": "primary",       # 号码去重 - 蓝
    "menu:extract": "primary",    # 提取手机号 - 蓝
    "menu:classify": "primary",     # 号码分类 - 蓝
    "menu:phonecheck": None,       # 空号检测 - 默认
    "menu:merge": None,            # 合并文本 - 默认
    "menu:sort": None,             # 定制分包 - 默认
    "menu:xlsx": None,             # xlsx转txt - 默认
    "menu:wash": "success",       # 洗数据 - 绿
    "menu:membership": "success",  # 会员中心 - 绿
    "buy:plans": "success",        # 购买会员 - 绿（突出）
    "menu:admin": "danger",        # 管理面板 - 红
}


def load_btn_icons() -> None:
    """启动时从数据库加载一次按钮图标和颜色配置到内存。"""
    global _btn_icons_cache, _btn_styles_cache, _icons_loaded
    try:
        from database import SessionLocal, get_config
        with SessionLocal() as session:
            icons = {}
            styles = {}
            for key in [
                "menu:split", "menu:zip", "menu:rename", "menu:vcf",
                "menu:dedup", "menu:extract", "menu:addcode", "menu:classify", "menu:phonecheck",
                "menu:merge", "menu:sort", "menu:xlsx", "menu:wash", "menu:lottery",
                "menu:membership", "buy:plans", "menu:admin",
                "admin:bind_drop_chat",
            ]:
                icon_val = get_config(session, f"btn_icon:{key}", "")
                if icon_val:
                    icons[key] = icon_val
                style_val = get_config(session, f"btn_style:{key}", "")
                if style_val:
                    styles[key] = style_val
                else:
                    # 用默认颜色
                    if key in DEFAULT_BTN_STYLES:
                        styles[key] = DEFAULT_BTN_STYLES[key]
            _btn_icons_cache = icons
            _btn_styles_cache = styles
            _icons_loaded = True
    except Exception:
        _icons_loaded = True


def update_btn_icon_cache(key: str, icon_id: str | None) -> None:
    """设置按钮图标后，同步更新内存缓存。"""
    if icon_id:
        _btn_icons_cache[key] = icon_id
    else:
        _btn_icons_cache.pop(key, None)


def update_btn_style_cache(key: str, style: str | None) -> None:
    """设置按钮颜色后，同步更新内存缓存。"""
    if style:
        _btn_styles_cache[key] = style
    else:
        _btn_styles_cache.pop(key, None)


def _push_btn(b: InlineKeyboardBuilder, text: str, callback_data: str) -> None:
    """按缓存给按钮加会员表情图标与颜色（无配置则普通按钮）。"""
    icon_id = _btn_icons_cache.get(callback_data)
    style = _btn_styles_cache.get(callback_data)
    try:
        b.button(text=text, callback_data=callback_data, icon_custom_emoji_id=icon_id, style=style)
    except TypeError:
        try:
            b.button(text=text, callback_data=callback_data, icon_custom_emoji_id=icon_id)
        except TypeError:
            b.button(text=text, callback_data=callback_data)


def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    buttons = [
        ("文件切割", "menu:split"),
        ("解压Zip提取TXT", "menu:zip"),
        ("批量重命名", "menu:rename"),
        ("号码转VCF", "menu:vcf"),
        ("号码去重", "menu:dedup"),
        ("提取手机号", "menu:extract"),
        ("加区号", "menu:addcode"),
        ("号码分类", "menu:classify"),
        ("空号检测", "menu:phonecheck"),
        ("合并文本", "menu:merge"),
        ("催收分包", "menu:sort"),
        ("Excel转txt", "menu:xlsx"),
        ("洗数据", "menu:wash"),
        ("抽奖中心", "menu:lottery"),
        ("会员中心", "menu:membership"),
        ("购买会员", "buy:plans"),
    ]
    if is_admin:
        buttons.append(("管理面板", "menu:admin"))

    for text, callback_data in buttons:
        _push_btn(b, text, callback_data)
    b.adjust(2)
    return b.as_markup()


def menu_kb(tg_id: int) -> InlineKeyboardMarkup:
    return main_menu_keyboard(is_admin=is_admin(tg_id))


def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ 取消", callback_data="cancel")
    return b.as_markup()


def classify_mode_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📡 按运营商导出", callback_data="classify:mode:carrier")
    b.button(text="🗺️ 按省份导出", callback_data="classify:mode:province")
    b.button(text="🏙️ 按城市导出", callback_data="classify:mode:city")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


def classify_skip_filename_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ 跳过，用默认文件名", callback_data="classify:filename:skip")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


def split_lines_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for n in (500, 1000, 2000, 5000):
        b.button(text=f"{n} 行", callback_data=f"split:lines:{n}")
    b.button(text="✍️ 自定义", callback_data="split:lines:custom")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(2)
    return b.as_markup()


def split_insert_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ 需要插入", callback_data="split:insert:yes")
    b.button(text="❌ 不需要", callback_data="split:insert:no")
    b.adjust(2)
    return b.as_markup()


def finish_upload_kb(action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ 完成上传", callback_data=f"{action}:done")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(2)
    return b.as_markup()


def merge_skip_filename_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ 跳过，用默认文件名", callback_data="merge:name:skip")
    b.button(text="❌ 取消", callback_data="cancel")
    b.adjust(1)
    return b.as_markup()


def membership_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎫 兑换卡密", callback_data="membership:redeem")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(2)
    return b.as_markup()


def buy_prompt_kb() -> InlineKeyboardMarkup:
    """非会员使用功能时的门槛提示按钮：优先引导购买会员。"""
    b = InlineKeyboardBuilder()
    b.button(text="💎 购买会员", callback_data="buy:plans")
    b.button(text="🎫 兑换卡密", callback_data="membership:redeem")
    b.button(text="🔙 返回主菜单", callback_data="menu:home")
    b.adjust(1)
    return b.as_markup()


def admin_kb(is_super: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    items = [
        ("生成卡密", "admin:gen"),
        ("删除卡密", "admin:delcard"),
        ("未使用卡密", "admin:unused"),
        ("卡密统计", "admin:stats"),
        ("成员列表", "admin:members"),
        ("添加天数", "admin:add"),
        ("扣除天数", "admin:deduct"),
        ("全站广播", "admin:broadcast"),
        ("编辑欢迎语", "admin:edit_welcome"),
        ("套餐设置", "admin:edit_plans"),
        ("按钮图标设置", "admin:btn_icons"),
        ("绑定洗数据群", "admin:bind_wash"),
        ("洗数据定价", "admin:edit_wash_prices"),
        ("洗数据用量统计", "admin:wash_usage"),
        ("洗数据账单", "admin:wash_orders"),
        ("洗数据加/扣余额", "admin:wash_balance"),
        ("小号登录", "admin:alt_login"),
        ("🎰 抽奖总览", "admin:lottery_pool"),
        ("抽奖奖品设置", "admin:lottery_config"),
        ("高级抽奖奖品", "admin:lottery_premium_config"),
        ("兑换价目表", "admin:lottery_redeem_config"),
        ("抽奖记录", "admin:lottery_records"),
        ("绑定抽奖通知群", "admin:bind_drop_chat"),
        ("抽奖通知模板", "admin:lottery_template"),
    ]
    if is_super:
        items.append(("洗数据价格下限", "admin:set_price_floor"))
        items.append(("累计洗数据配额", "admin:set_wash_quota"))
        items.append(("👥 管理员管理", "admin:manage_admins"))
    items.append(("返回主菜单", "menu:home"))
    for text, callback_data in items:
        _push_btn(b, text, callback_data)
    b.adjust(2)
    return b.as_markup()
