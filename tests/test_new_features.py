# -*- coding: utf-8 -*-
"""新增功能（总管理员/管理员角色、洗数据价格下限、累计洗数据配额）的回归测试。

使用独立临时 SQLite，不污染正式库。
"""
import os
import sys
import tempfile

import pytest

tmpdir = tempfile.mkdtemp(prefix="feature_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmpdir, 'feature.db')}"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_IDS"] = "111,222"
os.environ["SUPER_ADMIN_IDS"] = "999"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import settings  # noqa: E402
from database import (  # noqa: E402
    SessionLocal,
    WashOrder,
    add_admin_id,
    add_wash_balance,
    calc_wash_cost,
    get_admin_ids,
    get_washed_total,
    get_wash_price_floor,
    get_wash_quota,
    init_db,
    remove_admin_id,
    set_config,
    set_wash_price_floor,
    set_wash_prices,
    set_wash_quota,
)
from utils.roles import is_admin, is_super_admin  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


def test_role_checks(monkeypatch):
    # 不依赖环境变量加载顺序，直接改写 settings 属性（monkeypatch 自动还原）
    monkeypatch.setattr(settings, "ADMIN_IDS", [111, 222])
    monkeypatch.setattr(settings, "SUPER_ADMIN_IDS", [999])
    assert is_super_admin(999) is True
    assert is_super_admin(111) is False
    assert is_admin(111) is True      # 环境变量管理员
    assert is_admin(999) is True      # 总管理员也是管理员
    assert is_admin(12345) is False   # 普通用户


def test_dynamic_admin_add_remove():
    with SessionLocal() as s:
        assert add_admin_id(s, 555) is True
        assert add_admin_id(s, 555) is False          # 重复添加
        assert 555 in get_admin_ids(s)
        assert is_admin(555) is True                  # 数据库管理员生效
        assert remove_admin_id(s, 555) is True
        assert 555 not in get_admin_ids(s)
        assert is_admin(555) is False


def test_wash_price_floor_default_and_set():
    with SessionLocal() as s:
        assert get_wash_price_floor(s) == 0.0010      # 默认值
        set_wash_price_floor(s, 0.0020)
        assert get_wash_price_floor(s) == 0.0020


def test_calc_wash_cost_clamps_to_floor():
    with SessionLocal() as s:
        set_wash_price_floor(s, 0.0020)
        # 档位单价低于下限，应按下限计费
        set_wash_prices(s, [{"min_rows": 0, "price_per_row": 0.0005}])
        cost = calc_wash_cost(s, 10000)
        assert cost == round(10000 * 0.0020, 4), cost


def test_wash_quota_and_washed_total():
    with SessionLocal() as s:
        assert get_wash_quota(s) == 0                 # 默认不限制
        set_wash_quota(s, 1000000)
        assert get_wash_quota(s) == 1000000
        assert get_washed_total(s) == 0

        # 已洗总量 = 已扣费订单 + 待扣费任务
        add_wash_balance(s, 888, 100)
        s.add(WashOrder(user_id=888, filename="a.txt", row_count=120000, cost=1.2, type="deduct"))
        s.commit()
        assert get_washed_total(s) == 120000
        set_config(s, "wash_pending:AbC1234", "888:30000:0.3")
        assert get_washed_total(s) == 150000


def test_admin_keyboards():
    # 注意：路由挂载由 smoke_test.py 验证（router 为模块级单例，只能挂载一次）
    from utils.keyboards import admin_kb

    base_data = [b.callback_data for r in admin_kb().inline_keyboard for b in r]
    super_data = [b.callback_data for r in admin_kb(is_super=True).inline_keyboard for b in r]
    assert "admin:set_price_floor" not in base_data     # 价格下限仅总管理员
    assert "admin:set_wash_quota" not in base_data      # 累计配额仅总管理员
    assert "admin:wash_usage" in base_data             # 用量统计（查看累计已洗）
    assert "admin:manage_admins" not in base_data       # 普通管理员键盘无管理员管理
    assert "admin:set_price_floor" in super_data
    assert "admin:set_wash_quota" in super_data
    assert "admin:manage_admins" in super_data          # 总管理员键盘有
