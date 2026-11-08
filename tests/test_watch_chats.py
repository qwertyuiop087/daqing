"""小号监听范围回归：只订阅「绑定的洗数据群 + 空号检测机器人」。

线上问题：小号登录后把**所有群聊和私聊**的消息都读进来（日志里刷满
"收到消息 chat_id=..."），既没必要也不安全。

运行：python tests/test_watch_chats.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="watchchats_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from database import SessionLocal, init_db, set_config  # noqa: E402
from utils.telethon_client import watch_chats  # noqa: E402

GROUP_ID = -1001234567890


class TestWatchChats(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_config(session, "wash_chat_id", "")
            set_config(session, "phone_check_bot", "")

    def test_only_bound_group_and_phone_bot(self):
        with SessionLocal() as session:
            set_config(session, "wash_chat_id", str(GROUP_ID))
            set_config(session, "phone_check_bot", "@haomamofang_bot")
        chats = watch_chats()
        self.assertEqual(chats[0], GROUP_ID)
        self.assertIn("@haomamofang_bot", chats)
        # 只有这两个，绝不包含其它会话
        self.assertEqual(len(chats), 2)

    def test_default_phone_bot_when_not_configured(self):
        with SessionLocal() as session:
            set_config(session, "wash_chat_id", str(GROUP_ID))
        chats = watch_chats()
        self.assertIn("@haomamofang_bot", chats)

    def test_no_group_means_only_phone_bot(self):
        chats = watch_chats()
        self.assertNotIn(GROUP_ID, chats)
        self.assertEqual(chats, ["@haomamofang_bot"] if chats else [])

    def test_invalid_group_id_is_ignored(self):
        with SessionLocal() as session:
            set_config(session, "wash_chat_id", "not-a-number")
        chats = watch_chats()
        self.assertFalse(any(isinstance(c, int) for c in chats))

    def test_empty_group_still_watches_phone_bot(self):
        """没绑定洗数据群时，仍然只盯空号检测机器人，不会全量监听。"""
        self.assertEqual(watch_chats(), ["@haomamofang_bot"])

    def test_phone_bot_can_be_disabled(self):
        with SessionLocal() as session:
            set_config(session, "wash_chat_id", "")
            set_config(session, "phone_check_bot", "off")
        self.assertEqual(watch_chats(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
