"""管理端数据库函数测试：加/扣天数、删除卡密、按用户名查找。

运行：python tests/test_membership_db.py
"""
import itertools
import os
import sys
import tempfile
import unittest

tmpdir = tempfile.mkdtemp(prefix="mdb_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmpdir, 'm.db')}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_uid_counter = itertools.count(1)

from database import (  # noqa: E402
    SessionLocal,
    add_user_days,
    create_cards,
    deduct_user_days,
    delete_card,
    find_user,
    get_or_create_user,
    init_db,
    is_member,
    redeem_card,
)


class TestAdminDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = SessionLocal()
        self.uid = 123456789 + next(_uid_counter)  # 每个测试用独立用户，避免状态串扰
        self.uname = f"user{self.uid}"
        self.user = get_or_create_user(self.session, self.uid, self.uname, "张三")

    def tearDown(self):
        self.session.close()

    def test_add_days_activates_membership(self):
        text = add_user_days(self.session, self.user, 30)
        self.assertIn("30 天", text)
        self.session.refresh(self.user)
        self.assertTrue(is_member(self.user))

    def test_add_days_extends_from_current_expiry(self):
        add_user_days(self.session, self.user, 10)
        self.session.refresh(self.user)
        first_exp = self.user.membership_expires_at
        add_user_days(self.session, self.user, 10)
        self.session.refresh(self.user)
        self.assertGreater(self.user.membership_expires_at, first_exp)

    def test_deduct_days(self):
        add_user_days(self.session, self.user, 30)
        self.session.refresh(self.user)
        text = deduct_user_days(self.session, self.user, 10)
        self.assertIn("扣除 10 天", text)
        self.session.refresh(self.user)
        self.assertTrue(is_member(self.user))

    def test_deduct_beyond_remaining_expires(self):
        add_user_days(self.session, self.user, 3)
        text = deduct_user_days(self.session, self.user, 10)
        self.assertIn("已到期失效", text)
        self.session.refresh(self.user)
        self.assertIsNone(self.user.membership_expires_at)
        self.assertFalse(is_member(self.user))

    def test_deduct_without_membership(self):
        text = deduct_user_days(self.session, self.user, 5)
        self.assertIn("没有会员时长", text)

    def test_find_user_by_id_and_username(self):
        self.assertIs(find_user(self.session, str(self.uid)), self.user)
        self.assertIs(find_user(self.session, f"@{self.uname}"), self.user)
        self.assertIs(find_user(self.session, self.uname.upper()), self.user)
        self.assertIsNone(find_user(self.session, "nobody"))

    def test_delete_card(self):
        codes = create_cards(self.session, 1, 30, 1)
        ok, text = delete_card(self.session, codes[0].lower())
        self.assertTrue(ok)
        self.assertIn("未使用卡密", text)
        ok2, _ = delete_card(self.session, codes[0])
        self.assertFalse(ok2)

    def test_delete_used_card(self):
        codes = create_cards(self.session, 1, 30, 1)
        redeem_card(self.session, self.uid, codes[0])
        ok, text = delete_card(self.session, codes[0])
        self.assertTrue(ok)
        self.assertIn("已被使用", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
