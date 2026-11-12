"""解压 zip 收文件回归：真实调用 handler，防止 UnboundLocalError 之类回归。

线上报错：cannot access local variable 'zips' where it is not associated
with a value —— out_dir 里误用了锁内才定义的 zips。

运行：python tests/test_zip_handler_flow.py
"""
import asyncio
import io as _io
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="zipflow_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.zip_extract as zx  # noqa: E402

WORKDIR = tempfile.mkdtemp(prefix="zipwork_")


class FakeState:
    def __init__(self):
        self.data = {"workdir": WORKDIR, "zips": [], "total_txt": 0}

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)


class FakeBot:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="p", file_size=len(self.payload))

    async def download_file(self, file_path, destination):
        with open(destination, "wb") as f:
            f.write(self.payload)


class FakeMessage:
    def __init__(self, file_name, payload):
        self.document = SimpleNamespace(
            file_id="f1", file_name=file_name, file_size=len(payload)
        )
        self.from_user = SimpleNamespace(id=999001)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=1)


def make_zip(inner_name="名单.txt", content="13800138000\n") -> bytes:
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(inner_name, content)
    return buf.getvalue()


class TestZipHandlerFlow(unittest.TestCase):
    def test_two_zips_accumulate(self):
        """连发两个 zip，累计应该是 2 个包 / 2 个 TXT。"""
        payload = make_zip()
        state = FakeState()
        bot = FakeBot(payload)

        async def run():
            m1 = FakeMessage("a.zip", payload)
            await zx.zip_got_file(m1, state, bot)
            m2 = FakeMessage("b.zip", payload)
            await zx.zip_got_file(m2, state, bot)
            return m1, m2

        m1, m2 = asyncio.run(run())
        self.assertEqual(len(state.data["zips"]), 2, state.data)
        self.assertEqual(state.data["total_txt"], 2)
        # 第二次回复必须是「第 2 个压缩包」且累计 2
        self.assertIn("第 <b>2</b> 个压缩包", m2.answers[-1])
        self.assertIn("累计：2 个 ZIP", m2.answers[-1])

    def test_same_inner_name_not_overwritten(self):
        """两个包内都有 名单.txt，输出文件不能互相覆盖。"""
        payload = make_zip("名单.txt")
        state = FakeState()
        bot = FakeBot(payload)

        async def run():
            await zx.zip_got_file(FakeMessage("a.zip", payload), state, bot)
            await zx.zip_got_file(FakeMessage("b.zip", payload), state, bot)

        asyncio.run(run())
        paths = [f["path"] for z in state.data["zips"] for f in z["files"]]
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(set(paths)), 2, f"文件被覆盖：{paths}")

    def test_non_zip_ignored(self):
        payload = b"not a zip"
        state = FakeState()
        bot = FakeBot(payload)
        m = FakeMessage("a.txt", payload)

        asyncio.run(zx.zip_got_file(m, state, bot))
        self.assertEqual(state.data["zips"], [])
        self.assertTrue(any(".zip" in t for t in m.answers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
