"""洗数据提交回归：点「开始洗数据」绝不能静默无响应。

线上反馈：点了开始洗数据，机器人一点反应都没有。
原因是提交逻辑里任何一步抛异常，都会落到全局错误处理器（30 分钟节流、
只私聊管理员），用户端是完全静默的。

约定：
1. 点按钮立刻回一个可见提示；
2. 上传状态丢了（机器人刚重启）要说清楚「请重新发文件」；
3. 提交过程抛异常必须把失败原因发回给用户。

运行：python tests/test_wash_submit_guard.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washsubmit_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
from database import SessionLocal, init_db, set_wash_prices  # noqa: E402
from states import WashStates  # noqa: E402

USER_ID = 112233


class FakeState:
    def __init__(self, data):
        self.data = dict(data)

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_state(self):
        return self.data.get("_state")

    async def set_state(self, value):
        self.data["_state"] = getattr(value, "state", value)

    async def clear(self):
        self.data = {}


class FakeMessage:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=1)


class FakeCq:
    def __init__(self):
        self.answers = []
        self.message = FakeMessage()
        self.from_user = SimpleNamespace(id=USER_ID, username="tester", first_name="T")

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeBot:
    async def send_document(self, *a, **kw):
        return None


class TestWashSubmitGuard(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_wash_prices(session, [{"min_rows": 0, "price_per_row": 0.002}])

    def test_empty_state_tells_user_to_reupload(self):
        """没有文件（机器人重启丢了状态）时要有明确提示。"""
        cq = FakeCq()
        state = FakeState({"_state": WashStates.waiting_file.state})

        asyncio.run(wash_module.wash_submit(cq, state, FakeBot()))

        alerts = [t for t, show in cq.answers if show and t and "重新发" in t]
        self.assertTrue(alerts, f"没有提示用户重新上传：{cq.answers}")

    def test_always_answers_immediately(self):
        """点按钮后必须先回一个可见提示。"""
        cq = FakeCq()
        state = FakeState({"_state": WashStates.waiting_file.state})

        asyncio.run(wash_module.wash_submit(cq, state, FakeBot()))

        self.assertTrue(cq.answers, "没有任何 cq.answer 回应")
        self.assertEqual(cq.answers[0][0], "正在提交，请稍等…")

    def test_exception_is_reported_to_user(self):
        """提交过程抛异常时，用户必须看到失败原因。"""
        original = wash_module._wash_submit_impl

        async def boom(cq, state, bot):
            raise RuntimeError("db went away")

        cq = FakeCq()
        state = FakeState(
            {
                "files": [{"name": "a.txt", "path": "/tmp/a.txt", "lines": 10, "phones": 10}],
                "_state": WashStates.waiting_file.state,
            }
        )
        wash_module._wash_submit_impl = boom
        try:
            asyncio.run(wash_module.wash_submit(cq, state, FakeBot()))
        finally:
            wash_module._wash_submit_impl = original

        shown = " ".join(cq.message.edits + cq.message.answers)
        self.assertIn("提交失败", shown, shown)
        self.assertIn("db went away", shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
