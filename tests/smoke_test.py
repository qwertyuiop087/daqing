"""导入冒烟 + 数据库逻辑冒烟测试（使用独立临时 sqlite，不污染正式库）。

运行：python tests/smoke_test.py
"""
import os
import sys
import tempfile

tmpdir = tempfile.mkdtemp(prefix="smoke_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmpdir, 'smoke.db')}"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_IDS"] = "111,222"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 1) 全部模块导入（含所有 handlers，验证 aiogram API 用法）
import config  # noqa: E402,F401
import database  # noqa: E402
import main  # noqa: E402,F401
import services.file_ops  # noqa: E402,F401
import states  # noqa: E402,F401
import utils.helpers  # noqa: E402,F401
import utils.keyboards  # noqa: E402,F401
import handlers  # noqa: E402,F401

from aiogram import Dispatcher  # noqa: E402

from handlers import register_all_routers  # noqa: E402

# 1.5) 路由注册冒烟：所有 handler 都应成功挂载
dp = Dispatcher()
register_all_routers(dp)
router_names = {r.name for r in dp.sub_routers}
required = {"start", "membership", "buy", "wash", "wash_reply", "phone_check", "extract"}
missing = required - router_names
assert not missing, f"缺少路由: {missing}"

from database import (  # noqa: E402
    SessionLocal,
    create_cards,
    get_or_create_user,
    init_db,
    is_member,
    redeem_card,
)

# 2) 数据库逻辑冒烟
init_db()
with SessionLocal() as session:
    user = get_or_create_user(session, 123456789, "tester", "Tester")
    codes = create_cards(session, 2, 30, 111)
    assert len(codes) == 2 and len(set(codes)) == 2, "应生成 2 个不重复卡密"
    assert codes[0].count("-") == 2, "卡密格式应为 XXXX-XXXX-XXXX"

    ok, msg = redeem_card(session, 123456789, codes[0].lower())  # 小写输入也应成功
    assert ok, f"首次兑换应成功: {msg}"
    session.refresh(user)
    assert is_member(user), "兑换后会员应生效"

    ok2, _ = redeem_card(session, 123456789, codes[0])
    assert not ok2, "重复兑换应失败"

    ok3, _ = redeem_card(session, 123456789, "NOT-EXIST")
    assert not ok3, "不存在的卡密应失败"

print("SMOKE_OK")
