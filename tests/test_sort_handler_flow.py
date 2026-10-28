"""定制分包 handler 流程回归测试：模拟「完成上传 → 识别 → 输入顺序 → 输出」。

覆盖“识别消息正常显示，但状态写入失败导致后续消息无响应”这类运行时错误
（曾因 sort_done 引用未定义变量 order 导致用户输入顺序后机器人无反应）。
运行：python tests/test_sort_handler_flow.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 独立临时库：不依赖 CWD 下的 bot_data.db，保证测试自包含可重复运行
_TMP_DB_DIR = tempfile.mkdtemp(prefix="sortflow_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DB_DIR, 'test.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.sort as sort_module  # noqa: E402
from states import SortStates  # noqa: E402

LINE1 = "13800138000 张三 6222021000123456 500240198612311624 100.50"
LINE2 = "李四 13900139000 6222021000123457 500240198612311625 200"


def get_handler(attr: str, name: str):
    """从 router 的 observer 里取出指定 handler 的原始回调。"""
    router = getattr(sort_module.router, attr)
    for h in router.handlers:
        if getattr(h.callback, "__name__", "") == name:
            return h.callback
    raise AssertionError(f"handler {name} not found in router.{attr}")


class FakeState:
    def __init__(self, data):
        self._d = dict(data)
        self.state = None

    async def get_data(self):
        return self._d

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self._d = {}
        self.state = None

    async def close(self):
        pass


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.from_user = SimpleNamespace(id=123, username="u", first_name="U")
        self.sent = []

    async def answer(self, text="", **kw):
        self.sent.append(("text", text, kw))

    async def answer_document(self, doc, **kw):
        self.sent.append(("doc", doc, kw))

    async def answer_media_group(self, group, **kw):
        self.sent.append(("group", group, kw))


class FakeCq:
    def __init__(self):
        self.answered = False
        self.edited = []

    async def answer(self):
        self.answered = True

    async def edit_text(self, text, **kw):
        self.edited.append((text, kw))

    @property
    def message(self):
        return self


class TestSortHandlerFlow(unittest.TestCase):
    def setUp(self):
        from database import init_db
        init_db()
        self.tmp = tempfile.mkdtemp(prefix="sortflow_")
        self.files = [
            ("a.txt", os.path.join(self.tmp, "a.txt")),
            ("b.txt", os.path.join(self.tmp, "b.txt")),
        ]
        with open(self.files[0][1], "w", encoding="utf-8") as f:
            f.write(LINE1)
        with open(self.files[1][1], "w", encoding="utf-8") as f:
            f.write(LINE2)
        # 让 cleanup 不删目录，便于断言输出文件内容
        async def noop_cleanup(state):
            pass

        self._orig_cleanup = sort_module.cleanup_state
        sort_module.cleanup_state = noop_cleanup

    def tearDown(self):
        sort_module.cleanup_state = self._orig_cleanup
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _flow(self):
        cq = FakeCq()
        state = FakeState({"workdir": self.tmp, "files": self.files})

        # 1) 点「完成上传」→ 识别消息展示 + 状态进入 waiting_order
        sort_done = get_handler("callback_query", "sort_done")
        await sort_done(cq, state)
        self.assertTrue(cq.edited)
        head = cq.edited[0][0]
        self.assertIn("检测到 2 种", head)
        self.assertIn("名字", head)
        self.assertEqual(state.state, SortStates.waiting_order)
        self.assertEqual(state._d["delim"], " ")
        self.assertEqual(len(state._d["variants"]), 2)

        # 2) 用户输入期望顺序 → 正常重排并发送文件
        msg = FakeMessage("手机号 银行卡 身份证 金额 名字")
        sort_got_order = get_handler("message", "sort_got_order")
        await sort_got_order(msg, state)

        texts = [t for k, t, _ in msg.sent if k == "text"]
        self.assertTrue(any("批量调序完成" in t for t in texts), texts)
        # 2 个文件作为一组媒体组发送，文件名与原文件一致
        groups = [g for k, g, _ in msg.sent if k == "group"]
        self.assertEqual(len(groups), 1, msg.sent)
        self.assertEqual(len(groups[0]), 2)
        names = [im.media.filename for im in groups[0]]
        self.assertEqual(names, ["a.txt", "b.txt"])

        # 3) 输出文件：保持原文件名，内容按新顺序重排
        out_a = os.path.join(self.tmp, "out", "a.txt")
        out_b = os.path.join(self.tmp, "out", "b.txt")
        self.assertTrue(os.path.exists(out_a) and os.path.exists(out_b))
        with open(out_a, encoding="utf-8") as f:
            self.assertEqual(
                f.read().strip(),
                "13800138000 6222021000123456 500240198612311624 100.50 张三",
            )
        with open(out_b, encoding="utf-8") as f:
            self.assertEqual(
                f.read().strip(),
                "13900139000 6222021000123457 500240198612311625 200 李四",
            )

    def test_full_flow(self):
        asyncio.run(self._flow())


if __name__ == "__main__":
    unittest.main(verbosity=2)
