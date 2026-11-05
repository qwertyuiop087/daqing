"""洗数据回传顺序回归：必须「先交给用户，再记账」。

线上事故：结果文件已经进了洗数据群（群里能看到成品和统计），用户却一个文件
都没收到。原因是旧代码先记账后发送，记账里任何一处抛异常都会被全局错误处理器
吞掉，发送那一步根本走不到；而发送失败也只是 print，没有任何提示。

现在的约定：
1. 先投递文件，投递成功才记账（没送到就不扣钱）；
2. 记账抛异常不影响已经发出的文件，并私聊管理员；
3. 同一个结果文件重复到达只投递一次（不会重复扣费、不会重复发文件）。

运行：python tests/test_wash_delivery_order.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washorder_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
from database import SessionLocal, get_config, get_wash_balance, init_db, set_config  # noqa: E402

ALT_ID = 135791
# 每个用例用自己的随机码与用户，互不串数据
CODE = "Dl7vQ2x"
USER_ID = 246810


def get_handler(attr: str, name: str):
    router = getattr(wash_module.wash_reply_router, attr)
    for h in router.handlers:
        if getattr(h.callback, "__name__", "") == name:
            return h.callback
    raise AssertionError(f"handler {name} not found")


class FakeMessage:
    def __init__(self, file_name, caption="", unique="u1"):
        self.document = SimpleNamespace(
            file_id=f"fid_{file_name}", file_name=file_name, file_unique_id=unique
        )
        self.caption = caption
        self.from_user = SimpleNamespace(id=ALT_ID)


class FakeBot:
    """记录发送；可配置发送直接失败。"""

    def __init__(self, send_error=None):
        self.sent = []
        self.messages = []
        self.send_error = send_error

    async def send_document(self, chat_id, document, caption=None, **kw):
        if self.send_error:
            raise self.send_error
        self.sent.append((chat_id, document, caption))

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


class TestWashDeliveryOrder(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_config(session, "alt_user_id", str(ALT_ID))

    def _register(self, code, user_id):
        with SessionLocal() as session:
            set_config(session, f"wash_code:{code}", f"{user_id}:1698000000")
            set_config(session, f"wash_pending:{code}", f"{user_id}:5000:6.5:1698000000")
            set_config(session, f"wash_sent:{code}", "[]")

    def _pending(self, code=CODE):
        with SessionLocal() as session:
            return get_config(session, f"wash_pending:{code}", "")

    def test_settle_error_still_delivers(self):
        """记账抛异常时，文件必须已经发出去，账号不崩。"""
        self._register("Set1Err", 246801)
        original = wash_module._settle_wash_result

        def boom(*a, **kw):
            raise RuntimeError("db went away")

        async def flow():
            handler = get_handler("message", "alt_user_reply")
            bot = FakeBot()
            wash_module._settle_wash_result = boom
            try:
                await handler(FakeMessage("Set1Err_结果.txt"), bot)
            finally:
                wash_module._settle_wash_result = original
            return bot

        bot = asyncio.run(flow())
        self.assertEqual(len(bot.sent), 1, "文件必须先发给用户")
        self.assertIn("洗数据已完成", bot.sent[0][2])
        # 记账失败：pending 仍在（不会误判为已结清）
        self.assertNotEqual(self._pending("Set1Err"), "")

    def test_send_failure_does_not_charge(self):
        """文件发不出去时不能记账，pending 保留，等超时按比例退款。"""
        self._register("FailNCh", 246802)

        async def flow():
            handler = get_handler("message", "alt_user_reply")
            bot = FakeBot(send_error=Exception("chat not found"))
            await handler(FakeMessage("FailNCh_结果.txt"), bot)
            return bot

        bot = asyncio.run(flow())
        self.assertEqual(bot.sent, [])
        self.assertNotEqual(self._pending("FailNCh"), "", "没送到就不该结算")
        with SessionLocal() as session:
            from sqlalchemy import select

            from database import WashOrder

            rows = list(session.scalars(
                select(WashOrder).where(WashOrder.user_id == 246802)
            ))
            self.assertEqual(rows, [], "没送达不能产生扣费流水")

    def test_duplicate_result_delivered_once(self):
        """同一个结果文件重复到达：只发一次，也只结算一次。"""
        self._register("DupOnce", 246803)

        async def flow():
            handler = get_handler("message", "alt_user_reply")
            bot = FakeBot()
            msg = FakeMessage("DupOnce_结果.txt", unique="same-file")
            await handler(msg, bot)
            await handler(msg, bot)  # Telegram 重投 / 群消息重复
            return bot

        bot = asyncio.run(flow())
        self.assertEqual(len(bot.sent), 1, f"重复投递了：{bot.sent}")

    def test_delivery_marked_after_success(self):
        """投递成功后写入去重标记。"""
        self._register("Mark1Ok", 246804)

        async def flow():
            handler = get_handler("message", "alt_user_reply")
            bot = FakeBot()
            await handler(FakeMessage("Mark1Ok_结果.txt", unique="mark-me"), bot)

        asyncio.run(flow())
        keys = wash_module._delivery_keys("Mark1Ok")
        self.assertTrue(any("mark-me" in k for k in keys), keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
