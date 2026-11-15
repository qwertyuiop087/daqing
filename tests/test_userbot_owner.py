"""小号单实例锁回归：同一时刻只有一个实例能拿到占用权。

运行：python tests/test_userbot_owner.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="ubowner_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import database  # noqa: E402
from utils import telethon_client as tc  # noqa: E402


class TestUserbotOwner(unittest.TestCase):
    def setUp(self):
        database.init_db()
        with database.SessionLocal() as s:
            database.set_config(s, tc._OWNER_KEY, "")

    def test_force_takeover(self):
        self.assertTrue(asyncio.run(tc.acquire_userbot_owner(force=True)))
        iid, ts = tc._read_owner()
        self.assertEqual(iid, tc._INSTANCE_ID)
        self.assertGreater(ts, 0)

    def test_other_instance_owns(self):
        # 模拟另一个实例刚续过租
        with database.SessionLocal() as s:
            database.set_config(s, tc._OWNER_KEY, f"deadbeef9999:{__import__('time').time()}")
        got = asyncio.run(tc.acquire_userbot_owner(wait_seconds=0))
        self.assertFalse(got, "别人还在租期内时不能抢")
        iid, _ = tc._read_owner()
        self.assertEqual(iid, "deadbeef9999")

    def test_expired_lease_can_be_taken(self):
        import time

        with database.SessionLocal() as s:
            database.set_config(s, tc._OWNER_KEY, f"deadbeef9999:{time.time() - 9999}")
        got = asyncio.run(tc.acquire_userbot_owner(wait_seconds=0))
        self.assertTrue(got, "租约过期后应能接管")
        self.assertEqual(tc._read_owner()[0], tc._INSTANCE_ID)

    def test_release(self):
        asyncio.run(tc.acquire_userbot_owner(force=True))
        asyncio.run(tc.release_userbot_owner())
        self.assertEqual(tc._read_owner()[0], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
