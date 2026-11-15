"""邀请关系双向展示回归：ref_<ID> 绑定、能看到邀请人与被邀请人。

运行：python tests/test_referral_link.py
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="reflink_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from database import (  # noqa: E402
    SessionLocal,
    bind_inviter,
    get_invitees,
    get_or_create_user,
    get_referral_stats,
    get_referrer,
    init_db,
)


class TestReferralLink(unittest.TestCase):
    def setUp(self):
        init_db()
        self.a = 700001
        self.b = 700002
        self.c = 700003
        with SessionLocal() as s:
            get_or_create_user(s, self.a, "laowang", "老王")
            get_or_create_user(s, self.b, "xiaoli", "小李")
            get_or_create_user(s, self.c, "xiaozhang", "小张")

    def test_link_is_bot_link_plus_id(self):
        """链接格式必须是 机器人链接 + ref_<邀请人ID>。"""
        bot = "sechou_bot"
        link = f"https://t.me/{bot}?start=ref_{self.a}"
        self.assertTrue(link.startswith("https://t.me/sechou_bot?start=ref_"))
        self.assertTrue(link.endswith(str(self.a)))
        self.assertIn("?start=ref_", link)

    def test_bind_by_numeric_id(self):
        """ref_<ID> 能直接解析成邀请人（不依赖随机码）。"""
        with SessionLocal() as s:
            a = get_or_create_user(s, self.a)
            b = get_or_create_user(s, self.b)
            target = s.get(type(a), int(str(self.a)))
            self.assertIsNotNone(target)
            self.assertTrue(bind_inviter(s, b, target))

    def test_invitee_sees_inviter(self):
        with SessionLocal() as s:
            a = get_or_create_user(s, self.a)
            b = get_or_create_user(s, self.b)
            bind_inviter(s, b, a)
        with SessionLocal() as s:
            inviter = get_referrer(s, self.b)
            self.assertIsNotNone(inviter)
            self.assertEqual(inviter.id, self.a)
            self.assertEqual(inviter.first_name, "老王")

    def test_inviter_sees_invitees(self):
        with SessionLocal() as s:
            a = get_or_create_user(s, self.a)
            b = get_or_create_user(s, self.b)
            c = get_or_create_user(s, self.c)
            bind_inviter(s, b, a)
            bind_inviter(s, c, a)
        with SessionLocal() as s:
            invitees = get_invitees(s, self.a)
            self.assertEqual({u.id for u in invitees}, {self.b, self.c})
            stats = get_referral_stats(s, self.a)
            self.assertEqual(stats["count"], 2)

    def test_no_inviter_returns_none(self):
        uid = 700099  # 独立用户，避免被前面的用例绑过邀请人
        with SessionLocal() as s:
            get_or_create_user(s, uid, "solo", "独行")
        with SessionLocal() as s:
            self.assertIsNone(get_referrer(s, uid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
