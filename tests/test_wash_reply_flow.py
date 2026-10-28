"""洗数据回传回归测试：同一任务多个结果文件必须全部转发给用户。

背景：曾因"收到第一个结果文件就删除 wash_code 映射"导致同一任务
后面的结果文件找不到用户被丢弃（真实场景：苹果/安卓 两个结果文件只发了苹果）。
另外验证提示语里的"原文件"不会被消息 caption 污染。
运行：python tests/test_wash_reply_flow.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 独立临时库，保证测试自包含
_TMP_DB_DIR = tempfile.mkdtemp(prefix="washreply_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DB_DIR, 'test.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
from database import SessionLocal, get_config, init_db, set_config  # noqa: E402

USER_ID = 123456789
ALT_ID = 987654321
CODE = "FBhFN4E"  # 7 位随机码（与线上实际格式一致）


def get_handler(attr: str, name: str):
    router = getattr(wash_module.wash_reply_router, attr)
    for h in router.handlers:
        if getattr(h.callback, "__name__", "") == name:
            return h.callback
    raise AssertionError(f"handler {name} not found")


class FakeMessage:
    def __init__(self, file_name, caption=""):
        self.document = SimpleNamespace(file_id=f"fid_{file_name}", file_name=file_name)
        self.caption = caption
        self.from_user = SimpleNamespace(id=ALT_ID)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.sent.append((chat_id, document, caption))


class TestWashReplyFlow(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_config(session, "alt_user_id", str(ALT_ID))
            set_config(session, f"wash_code:{CODE}", f"{USER_ID}:1698000000")
            set_config(session, f"wash_pending:{CODE}", f"{USER_ID}:5000:6.5:1698000000")

    def test_multiple_result_files_all_forwarded(self):
        """苹果/安卓 两个结果文件都要转发；原文件名不带群里的 caption。

        文件名与截图一致：FBhFN4E_r6EYSW0_123-苹果-927-苹果-898.txt（旧码前缀保留在文件名里）。
        """
        async def flow():
            handler = get_handler("message", "alt_user_reply")
            bot = FakeBot()

            await handler(
                FakeMessage(
                    f"{CODE}_r6EYSW0_123-苹果-927-苹果-898.txt",
                    caption="蓝号(IOS/imessage/FaceTime)",
                ),
                bot,
            )
            await handler(
                FakeMessage(
                    f"{CODE}_r6EYSW0_123-苹果-927-安卓-29.txt",
                    caption="检测完成统计",
                ),
                bot,
            )

            # 两个文件都应发给用户
            self.assertEqual(len(bot.sent), 2, bot.sent)
            for chat_id, _, caption in bot.sent:
                self.assertEqual(chat_id, USER_ID)
                self.assertIn("洗数据已完成", caption)

            # 原文件名只保留文件名本身，不混入消息 caption
            self.assertIn("原文件：r6EYSW0_123-苹果-927-苹果-898.txt", bot.sent[0][2])
            self.assertNotIn("蓝号", bot.sent[0][2])
            self.assertIn("原文件：r6EYSW0_123-苹果-927-安卓-29.txt", bot.sent[1][2])
            self.assertNotIn("检测完成统计", bot.sent[1][2])

            # 扣费只发生一次：pending 已删，wash_code 保留（供后续文件继续匹配）
            with SessionLocal() as session:
                self.assertEqual(get_config(session, f"wash_pending:{CODE}", ""), "")
                self.assertNotEqual(get_config(session, f"wash_code:{CODE}", ""), "")

        asyncio.run(flow())

    def test_legacy_code_format_still_works(self):
        """旧格式（纯 user_id，无时间戳）的映射仍能正常转发。"""
        async def flow():
            handler = get_handler("message", "alt_user_reply")
            bot = FakeBot()
            with SessionLocal() as session:
                set_config(session, "wash_code:Legacy1", str(USER_ID))

            await handler(FakeMessage("Legacy1_名单.txt"), bot)

            self.assertEqual(len(bot.sent), 1)
            self.assertEqual(bot.sent[0][0], USER_ID)

        asyncio.run(flow())


if __name__ == "__main__":
    unittest.main(verbosity=2)
