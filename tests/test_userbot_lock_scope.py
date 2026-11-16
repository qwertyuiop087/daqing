"""小号锁：按 session 指纹隔离 + 心跳不互相踢。

运行：python tests/test_userbot_lock_scope.py
"""
import asyncio
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="ublock_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import database  # noqa: E402
from utils import telethon_client as tc  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TestSessionScopedLock(unittest.TestCase):
    def setUp(self):
        database.init_db()
        for key in (tc._session_key("SESSION_A"), tc._session_key("SESSION_B")):
            with database.SessionLocal() as s:
                database.set_config(s, key, "")

    def test_same_session_same_key(self):
        self.assertEqual(tc._session_key("ABC"), tc._session_key("ABC"))

    def test_different_session_different_key(self):
        self.assertNotEqual(tc._session_key("ABC"), tc._session_key("XYZ"))

    def test_two_sessions_do_not_block_each_other(self):
        """两个不同小号（两个项目）各自能拿到锁，不再互相锁死。"""
        key_a = tc._session_key("SESSION_A")
        key_b = tc._session_key("SESSION_B")
        self.assertTrue(
            asyncio.run(tc.acquire_userbot_owner(wait_seconds=0, force=True, owner_key=key_a))
        )
        self.assertTrue(
            asyncio.run(tc.acquire_userbot_owner(wait_seconds=0, force=True, owner_key=key_b))
        )
        self.assertEqual(tc._read_owner(key_a)[0], tc._INSTANCE_ID)
        self.assertEqual(tc._read_owner(key_b)[0], tc._INSTANCE_ID)

    def test_same_session_still_exclusive(self):
        key = tc._session_key("SESSION_A")
        with database.SessionLocal() as s:
            database.set_config(s, key, "otherinstance:9999999999")
        self.assertFalse(
            asyncio.run(tc.acquire_userbot_owner(wait_seconds=0, force=False, owner_key=key))
        )


class TestNoMultipleHeartbeats(unittest.TestCase):
    def test_start_cancels_old_heartbeat(self):
        with io.open(
            os.path.join(ROOT, "utils", "telethon_client.py"), encoding="utf-8"
        ) as f:
            src = f.read()
        idx = src.find("_heartbeat_task.cancel()")
        self.assertGreater(idx, 0, "启动前必须取消旧心跳")
        # 心跳里要绑定自己的 client，被替换就退出
        self.assertIn("if userbot is not my_client:", src)
        self.assertIn("await my_client.disconnect()", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
