"""复验 Railway 报错路径：未配置 DATABASE_URL 时 SQLite 默认分支必须可正常建表与读写。

运行：python tests/check_sqlite_default.py
"""
import os
import shutil
import sys
import tempfile

# 关键：确保与 Railway 相同的场景 —— 不设置 DATABASE_URL
os.environ.pop("DATABASE_URL", None)

workdir = tempfile.mkdtemp(prefix="sqlite_check_")
os.chdir(workdir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import (  # noqa: E402
    SessionLocal,
    create_cards,
    get_or_create_user,
    init_db,
    is_member,
    redeem_card,
)

init_db()
with SessionLocal() as session:
    user = get_or_create_user(session, 555, "check", "Check")
    codes = create_cards(session, 1, 30, 1)
    ok, msg = redeem_card(session, 555, codes[0])
    assert ok, msg
    session.refresh(user)
    assert is_member(user)

assert os.path.exists(os.path.join(workdir, "bot_data.db")), "应生成 bot_data.db"
print("SQLITE_DEFAULT_OK")
shutil.rmtree(workdir, ignore_errors=True)
