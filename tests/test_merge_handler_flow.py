"""合并文本 handler 流程回归测试：上传完成 → 自定义导出文件名 → 输出。

覆盖「导出文件名可自定义 / 跳过用默认名 / 文件名清洗自动补 .txt」。
运行：python tests/test_merge_handler_flow.py
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
_TMP_DB_DIR = tempfile.mkdtemp(prefix="mergeflow_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DB_DIR, 'test.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.merge as merge_module  # noqa: E402
from database import init_db  # noqa: E402
from states import MergeStates  # noqa: E402


def get_handler(attr: str, name: str):
    """从 router 的 observer 里取出指定 handler 的原始回调。"""
    router = getattr(merge_module.router, attr)
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


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.from_user = SimpleNamespace(id=123, username="u", first_name="U")
        self.sent = []

    async def answer(self, text="", **kw):
        self.sent.append(("text", text, kw))

    async def answer_document(self, doc, **kw):
        self.sent.append(("doc", doc, kw))


class FakeCq:
    def __init__(self):
        self.answered = False
        self.edited = []
        self.sent = []
        self.from_user = SimpleNamespace(id=123, username="u", first_name="U")

    async def answer(self, text="", **kw):
        self.answered = True
        if text:
            self.sent.append(("text", text, kw))

    async def answer_document(self, doc, **kw):
        self.sent.append(("doc", doc, kw))

    async def edit_text(self, text, **kw):
        self.edited.append((text, kw))

    @property
    def message(self):
        return self


class TestMergeHandlerFlow(unittest.TestCase):
    def setUp(self):
        init_db()
        # 流程结束后保留 workdir，便于断言输出文件内容
        async def noop_cleanup(state):
            pass

        self._orig_cleanup = merge_module.cleanup_state
        merge_module.cleanup_state = noop_cleanup

        self.tmp = tempfile.mkdtemp(prefix="mergeflow_work_")
        self.files = [
            ("a.txt", os.path.join(self.tmp, "a.txt")),
            ("b.txt", os.path.join(self.tmp, "b.txt")),
        ]
        with open(self.files[0][1], "wb") as f:
            f.write(b"111\n222\n")
        with open(self.files[1][1], "wb") as f:
            f.write(b"333\n444\n")

    def tearDown(self):
        merge_module.cleanup_state = self._orig_cleanup
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _docs(self, obj):
        return [d for k, d, _ in obj.sent if k == "doc"]

    def _texts(self, obj):
        return [t for k, t, _ in obj.sent if k == "text"]

    def test_custom_filename(self):
        """完成上传 → 询问文件名 → 输入「名单」→ 输出 名单.txt，内容为直接拼接。"""
        async def flow():
            cq = FakeCq()
            state = FakeState({"workdir": self.tmp, "files": self.files})
            merge_done = get_handler("callback_query", "merge_done")
            await merge_done(cq, state)
            self.assertEqual(state.state, MergeStates.waiting_filename)
            self.assertTrue(any("导出文件名" in t for t in self._texts(cq)))

            msg = FakeMessage("名单")
            merge_filename_input = get_handler("message", "merge_filename_input")
            await merge_filename_input(msg, state)

            out = os.path.join(self.tmp, "out", "名单.txt")
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), "111\n222\n333\n444")
            docs = self._docs(msg)
            self.assertTrue(docs)
            self.assertEqual(docs[0].filename, "名单.txt")

        asyncio.run(flow())

    def test_skip_uses_default_name(self):
        """跳过 → 默认导出 合并结果.txt。"""
        async def flow():
            cq = FakeCq()
            state = FakeState({"workdir": self.tmp, "files": self.files})
            merge_done = get_handler("callback_query", "merge_done")
            await merge_done(cq, state)

            merge_filename_skip = get_handler("callback_query", "merge_filename_skip")
            await merge_filename_skip(cq, state)

            out = os.path.join(self.tmp, "out", "合并结果.txt")
            self.assertTrue(os.path.exists(out))
            docs = self._docs(cq)
            self.assertTrue(docs)
            self.assertEqual(docs[0].filename, "合并结果.txt")

        asyncio.run(flow())

    def test_filename_sanitized_and_txt_appended(self):
        """非法字符被清洗，缺后缀自动补 .txt。"""
        async def flow():
            msg = FakeMessage("a/b:c*d?.txt")
            state = FakeState({"workdir": self.tmp, "files": self.files})
            merge_filename_input = get_handler("message", "merge_filename_input")
            await merge_filename_input(msg, state)
            docs = self._docs(msg)
            self.assertTrue(docs)
            name = docs[0].filename
            self.assertTrue(name.endswith(".txt"))
            self.assertNotIn("/", name)
            self.assertNotIn(":", name)
            # 无后缀输入自动补 .txt
            msg2 = FakeMessage("导出名单")
            state2 = FakeState({"workdir": self.tmp, "files": self.files})
            await merge_filename_input(msg2, state2)
            docs2 = self._docs(msg2)
            self.assertEqual(docs2[0].filename, "导出名单.txt")

        asyncio.run(flow())


if __name__ == "__main__":
    unittest.main(verbosity=2)
