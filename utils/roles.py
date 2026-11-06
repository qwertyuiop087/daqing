"""管理员角色：总管理员（Super Admin）与管理员（Admin）。

- 总管理员：由环境变量 SUPER_ADMIN_IDS 指定，独占「累计洗数据配额」「洗数据价格下限」
  「管理员管理（增删管理员）」三项；其它后台功能与管理员一样都能用。
  若没有配置 SUPER_ADMIN_IDS（很常见：只填了 ADMIN_IDS），
  则退化为「ADMIN_IDS 里第一个」是总管理员，避免这些功能没人能用。
- 管理员：环境变量 ADMIN_IDS 或数据库中动态添加（总管理员在网页后台或
  「管理面板 → 管理员管理」里增删）。
"""
from config import settings


def super_admin_ids() -> list[int]:
    """实际生效的总管理员列表（含退化逻辑）。"""
    if settings.SUPER_ADMIN_IDS:
        return sorted(settings.SUPER_ADMIN_IDS)
    admins = sorted(settings.ADMIN_IDS)
    return [admins[0]] if admins else []


def is_super_admin(tg_id: int) -> bool:
    """是否为总管理员。"""
    return tg_id in super_admin_ids()


def is_admin(tg_id: int) -> bool:
    """是否为管理员（含总管理员、环境变量管理员、数据库动态管理员）。"""
    if tg_id in settings.ADMIN_IDS or tg_id in settings.SUPER_ADMIN_IDS:
        return True
    from database import SessionLocal, get_admin_ids
    with SessionLocal() as session:
        return tg_id in get_admin_ids(session)


def admin_id_list() -> list[int]:
    """返回全部管理员 ID（环境变量 + 数据库动态），用于展示。"""
    ids = set(settings.ADMIN_IDS) | set(settings.SUPER_ADMIN_IDS)
    from database import SessionLocal, get_admin_ids
    with SessionLocal() as session:
        ids |= set(get_admin_ids(session))
    return sorted(ids)
