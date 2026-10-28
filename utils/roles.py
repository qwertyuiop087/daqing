"""管理员角色：总管理员（Super Admin）与管理员（Admin）。

- 总管理员：由环境变量 SUPER_ADMIN_IDS 指定，拥有最高权限（可管理管理员、调整洗数据价格下限与累计配额）。
- 管理员：环境变量 ADMIN_IDS 或数据库中动态添加（总管理员通过「管理面板 → 管理员管理」增删）。
"""
from config import settings


def is_super_admin(tg_id: int) -> bool:
    """是否为总管理员。"""
    return tg_id in settings.SUPER_ADMIN_IDS


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
