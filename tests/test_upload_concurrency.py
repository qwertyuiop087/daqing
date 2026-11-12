"""并发上传回归：多选/媒体组同时到达的文件必须累加，不能互相覆盖。

线上现象：一次发 5 个 zip，每条回复都写「第 1 个压缩包，累计 1 个 ZIP」，
最后只剩最后一个文件——因为 aiogram 对每个 update 单独起 task，
几个 handler 同时读到空列表再各自覆盖。

运行：python tests/test_upload_concurrency.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="upconc_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from utils.helpers import user_lock  # noqa: E402


class FakeState:
    """模拟 FSMContext：get_data 返回副本，update_data 覆盖。"""

    def __init__(self):
        self.data = {}

    async def get_data(self):
        await asyncio.sleep(0)  # 让出控制权，放大竞态
        return dict(self.data)

    async def update_data(self, **kw):
        await asyncio.sleep(0)
        self.data.update(kw)


async def append_without_lock(state, item):
    data = await state.get_data()
    items = list(data.get("items") or [])
    await asyncio.sleep(0.01)  # 模拟下载/解析耗时
    items.append(item)
    await state.update_data(items=items)
    return len(items)


async def append_with_lock(state, uid, item):
    async with user_lock(uid):
        data = await state.get_data()
        items = list(data.get("items") or [])
        await asyncio.sleep(0.01)
        items.append(item)
        await state.update_data(items=items)
        return len(items)


class TestUploadConcurrency(unittest.TestCase):
    def test_without_lock_loses_files(self):
        """不加锁时确实是坏的（用来证明这个 bug 真的存在）。"""
        state = FakeState()

        async def run():
            return await asyncio.gather(*[append_without_lock(state, i) for i in range(5)])

        asyncio.run(run())
        self.assertLess(len(asyncio.run(self._final(state))), 5)

    async def _final(self, state):
        return (await state.get_data()).get("items") or []

    def test_with_lock_keeps_all(self):
        state = FakeState()
        uid = 777

        async def run():
            return await asyncio.gather(*[append_with_lock(state, uid, i) for i in range(5)])

        counts = asyncio.run(run())
        final = asyncio.run(self._final(state))
        self.assertEqual(len(final), 5, f"丢文件了：{final}")
        self.assertEqual(sorted(counts), [1, 2, 3, 4, 5], f"累计编号不连续：{counts}")

    def test_lock_is_per_user(self):
        self.assertIs(user_lock(1), user_lock(1))
        self.assertIsNot(user_lock(1), user_lock(2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
