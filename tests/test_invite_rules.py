"""邀请规则回归：只有新用户能绑定，老用户不会被再次邀请、也不会重复注册。

运行：python tests/test_invite_rules.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="invrules_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from database import SessionLocal, User, get_or_create_user, init_db  # noqa: E402
from handlers.start import cmd_start  # noqa: E402


class FakeMessage:
    def __init__(self, uid, text, first_name="测试"):
        self.from_user = SimpleNamespace(
            id=uid, username=f"u{uid}", first_name=first_name
        )
        self.text = text
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class TestInviteRules(unittest.TestCase):
    def setUp(self):
        init_db()
        # 清掉可能残留
        from sqlalchemy import delete

        with SessionLocal() as s:
            s.execute(delete(User).where(User.id.in_([800001, 800002, 800003])))
            s.commit()
        with SessionLocal() as s:
            get_or_create_user(s, 800001, "inviter", "老王")

    def _count(self, uid):
        with SessionLocal() as s:
            return len(list(s.scalars(__import__("sqlalchemy").select(User).where(User.id == uid))))

    def test_new_user_can_be_invited(self):
        m = FakeMessage(800002, "/start ref_800001", "新人")
        asyncio.run(cmd_start(m))
        with SessionLocal() as s:
            u = s.get(User, 800002)
            self.assertEqual(u.inviter_id, 800001)
        self.assertTrue(any("邀请链接进入" in t for t in m.answers))

    def test_registered_user_cannot_be_invited_again(self):
        # 先注册（不带邀请）
        asyncio.run(cmd_start(FakeMessage(800003, "/start", "老用户")))
        # 再点别人的邀请链接
        m = FakeMessage(800003, "/start ref_800001", "老用户")
        asyncio.run(cmd_start(m))
        with SessionLocal() as s:
            u = s.get(User, 800003)
            self.assertIsNone(u.inviter_id, "老用户不应被绑定邀请人")
        self.assertTrue(any("已经是注册用户" in t for t in m.answers))

    def test_no_duplicate_registration(self):
        for _ in range(3):
            asyncio.run(cmd_start(FakeMessage(800003, "/start", "重复")))
        self.assertEqual(self._count(800003), 1, "重复 /start 不能创建多条用户记录")

    def test_bound_user_not_rebound(self):
        m1 = FakeMessage(800002, "/start ref_800001")
        asyncio.run(cmd_start(m1))
        # 换一个邀请人再点（800004 不存在，但也要保证不被改绑）
        m2 = FakeMessage(800002, "/start ref_800099")
        asyncio.run(cmd_start(m2))
        with SessionLocal() as s:
            self.assertEqual(s.get(User, 800002).inviter_id, 800001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
