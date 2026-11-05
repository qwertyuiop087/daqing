"""洗数据结果补发回归：每分钟扫群，把漏掉的结果文件补发给用户。

线上场景：结果文件已经发到洗数据群，但机器人那一刻掉线/异常，用户没收到。
补发任务必须在下面这些边界上正确：
1. 群里我们自己发给小号的源文件（wash_src）绝不能当结果再发一遍；
2. 太新的消息（< 60 秒）跳过，留给实时处理器；
3. 已经投递过的文件跳过，不会重复发、重复扣；
4. 补发成功后照常结算一份费用。

运行：python tests/test_wash_resend.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washresend_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import utils.wash_resend as resend  # noqa: E402
from database import SessionLocal, get_config, init_db, set_config  # noqa: E402

USER_ID = 314159
CHAT_ID = -1001234567890
CODE = "Rs1Scan"
PENDING_FILES = [500, 500]


def make_msg(name, age_seconds=300, caption="", size=100, msg_id=1):
    async def download_media(file=None):
        with open(file, "wb") as fh:
            fh.write(b"data")
        return file

    return SimpleNamespace(
        id=msg_id,
        date=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        document=SimpleNamespace(size=size),
        file=SimpleNamespace(name=name),
        message=caption,
        download_media=download_media,
    )


class FakeUserbot:
    def __init__(self, messages):
        self._messages = messages

    def is_connected(self):
        return True

    async def iter_messages(self, chat_id, limit=None):
        for m in self._messages:
            yield m


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.sent.append((chat_id, getattr(document, "filename", ""), caption))


class TestWashResend(unittest.TestCase):
    def setUp(self):
        init_db()
        ts = int(time.time())
        fm = json.dumps({"files": list(PENDING_FILES), "charged": 0.0}, ensure_ascii=False)
        with SessionLocal() as session:
            set_config(session, "wash_chat_id", str(CHAT_ID))
            set_config(session, "wash_code:Rs1Scan", f"{USER_ID}:{ts}")
            set_config(session, "wash_pending:Rs1Scan", f"{USER_ID}:1000:2.0:{ts}:{fm}")
            set_config(session, "wash_src:Rs1Scan", json.dumps(["Rs1Scan_源文件.txt"]))
            set_config(session, "wash_sent:Rs1Scan", "[]")

    def _run(self, messages):
        import utils.telethon_client as tc

        original = tc.userbot
        tc.userbot = FakeUserbot(messages)
        bot = FakeBot()
        try:
            n = asyncio.run(resend._scan_once(bot))
        finally:
            tc.userbot = original
        return n, bot

    def test_pending_codes(self):
        codes = resend._pending_codes()
        self.assertEqual(codes.get("Rs1Scan"), 2)

    def test_source_file_is_skipped(self):
        """我们自己发给小号的源文件不能被当成结果补发。"""
        n, bot = self._run([make_msg("Rs1Scan_源文件.txt")])
        self.assertEqual(n, 0)
        self.assertEqual(bot.sent, [])

    def test_too_new_is_skipped(self):
        """60 秒内的消息留给实时处理器，补发不抢。"""
        n, bot = self._run([make_msg("Rs1Scan_结果.txt", age_seconds=10)])
        self.assertEqual(n, 0)
        self.assertEqual(bot.sent, [])

    def test_result_is_resent_and_settled(self):
        """正常补发：发给对应用户，并结算一份。"""
        n, bot = self._run([make_msg("Rs1Scan_结果.txt", age_seconds=300, size=1000)])
        self.assertEqual(n, 1, bot.sent)
        self.assertEqual(bot.sent[0][0], USER_ID)
        self.assertIn("洗数据已完成", bot.sent[0][2])
        with SessionLocal() as session:
            pending = get_config(session, "wash_pending:Rs1Scan", "")
        # 结算一份：剩余 files 只有一条
        fm = json.loads(pending.split(":", 4)[4])
        self.assertEqual(len(fm["files"]), 1)
        self.assertGreater(fm["charged"], 0)

    def test_already_delivered_is_skipped(self):
        """同一个文件补发过一次后，再次扫描不再发送。"""
        n1, bot1 = self._run([make_msg("Rs1Scan_结果.txt", age_seconds=300, size=777)])
        self.assertEqual(n1, 1)
        n2, bot2 = self._run([make_msg("Rs1Scan_结果.txt", age_seconds=300, size=777)])
        self.assertEqual(n2, 0, f"重复补发：{bot2.sent}")

    def test_unknown_code_is_skipped(self):
        """随机码没有未回传任务（例如已结清）时不补发，避免白送。"""
        n, bot = self._run([make_msg("Zz9None_结果.txt", age_seconds=300)])
        self.assertEqual(n, 0)
        self.assertEqual(bot.sent, [])

    def test_old_message_out_of_window_is_skipped(self):
        """超过 30 分钟窗口的消息不处理。"""
        n, bot = self._run([make_msg("Rs1Scan_结果.txt", age_seconds=3600)])
        self.assertEqual(n, 0)
        self.assertEqual(bot.sent, [])

    def test_manual_scan_works_without_pending(self):
        """手动补发放宽：任务记录已结清删除，只要随机码能查到用户也能捞回来。"""
        from database import delete_config

        with SessionLocal() as session:
            delete_config(session, "wash_pending:Rs1Scan")

        import utils.telethon_client as tc

        original = tc.userbot
        tc.userbot = FakeUserbot([make_msg("Rs1Scan_结果.txt", age_seconds=300, size=4242)])
        bot = FakeBot()
        try:
            # 自动循环（要求 pending）→ 不处理
            self.assertEqual(asyncio.run(resend._scan_once(bot)), 0)
            # 手动模式 → 补发
            stat = asyncio.run(resend._scan(bot, require_pending=False))
        finally:
            tc.userbot = original
        self.assertEqual(stat["sent"], 1, stat)
        self.assertEqual(len(bot.sent), 1)

    def test_settled_task_keeps_record_and_never_recharges(self):
        """结清后记录保留（带 done 标记），再次结算不会重复扣钱。"""
        from handlers import wash as wash_handlers

        with SessionLocal() as session:
            set_config(session, "wash_sent:Settle1", "[]")

        ts = int(time.time())
        fm = json.dumps({"files": [1000], "charged": 0.0}, ensure_ascii=False)
        with SessionLocal() as session:
            set_config(session, "wash_code:Settle1", f"{USER_ID}:{ts}")
            set_config(session, "wash_pending:Settle1", f"{USER_ID}:1000:2.0:{ts}:{fm}")

        first = wash_handlers._settle_wash_result("Settle1", USER_ID, "结果.txt")
        self.assertGreater(first[0], 0)
        self.assertTrue(first[2])

        with SessionLocal() as session:
            raw = get_config(session, "wash_pending:Settle1", "")
        self.assertNotEqual(raw, "", "结清后要保留记录，补发才能认出随机码")
        self.assertIn('"done": 1', raw)

        # 再结算一次：不能重复扣费
        second = wash_handlers._settle_wash_result("Settle1", USER_ID, "结果.txt")
        self.assertEqual(second[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
