"""小号自动接管与 /userbot 命令回归。

运行：python tests/test_userbot_takeover.py
"""
import asyncio
import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="ubtake_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import database  # noqa: E402
from utils import telethon_client as tc  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TestAcquireRetry(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self.key = tc._session_key("SESSION_TAKEOVER")
        with database.SessionLocal() as s:
            database.set_config(s, self.key, "")

    def test_retry_task_exists(self):
        self.assertTrue(hasattr(tc, "_start_acquire_retry"))
        self.assertTrue(hasattr(tc, "force_takeover"))

    def test_ttl_short(self):
        self.assertLessEqual(tc._OWNER_TTL, 90, "租约太长，旧实例消失后要等太久")

    def test_other_instance_then_release(self):
        """别人占着时抢不到；他释放后立刻能接管。"""
        with database.SessionLocal() as s:
            database.set_config(s, self.key, f"otherinst9999:{time.time()}")
        self.assertFalse(
            asyncio.run(tc.acquire_userbot_owner(wait_seconds=0, owner_key=self.key))
        )
        with database.SessionLocal() as s:
            database.set_config(s, self.key, "")
        self.assertTrue(
            asyncio.run(tc.acquire_userbot_owner(wait_seconds=0, owner_key=self.key))
        )

    def test_no_user_facing_alert_on_first_conflict(self):
        """抢不到锁时不该立刻弹告警（以前一启动就弹，用户以为坏了）。"""
        with io.open(os.path.join(ROOT, "utils", "telethon_client.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.find("async def acquire_userbot_owner")
        j = src.find("async def release_userbot_owner")
        body = src[i:j]
        self.assertNotIn("_notify_admins", body, "抢锁函数里不该直接发告警")


class TestUserbotCommand(unittest.TestCase):
    def test_command_registered(self):
        with io.open(os.path.join(ROOT, "handlers", "membership.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('Command("userbot"', src)
        self.assertIn("userbot:takeover", src)
        self.assertIn("force_takeover", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
