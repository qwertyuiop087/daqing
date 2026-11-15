"""洗数据结算回归：并发回传不能重复结算、同名同大小文件不能被吞。

运行：python tests/test_wash_settle.py
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="settle_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash  # noqa: E402
from database import SessionLocal, WashOrder, init_db, set_config  # noqa: E402


class TestDeliveryKey(unittest.TestCase):
    def test_same_name_diff_unique_id_kept(self):
        """同批两个同名同大小文件，file_unique_id 不同 → 键必须不同。"""
        d1 = SimpleNamespace(file_unique_id="AAA", file_size=4096)
        d2 = SimpleNamespace(file_unique_id="BBB", file_size=4096)
        k1 = wash._delivery_key(d1, "名单.txt")
        k2 = wash._delivery_key(d2, "名单.txt")
        self.assertNotEqual(k1, k2)

    def test_fallback_without_unique_id(self):
        d = SimpleNamespace(file_size=100)
        self.assertEqual(wash._delivery_key(d, "a.txt"), "a.txt|100")


class TestSettleConcurrent(unittest.TestCase):
    """批量两份结果同时到达：合计结算必须等于总价，且各结算一份。"""

    def setUp(self):
        init_db()
        self.code = "Settle01"
        self.uid = 880001
        self.cost = 8.0
        with SessionLocal() as s:
            set_config(s, f"wash_pending:{self.code}",
                       f"{self.uid}:8000:{self.cost}:g:-")
            set_config(s, f"wash_pending:{self.code}",
                       f"{self.uid}:8000:{self.cost}:g:"
                       + json.dumps({"files": [5000, 3000], "charged": 0.0},
                                    ensure_ascii=False))

    def _orders(self):
        from sqlalchemy import select

        with SessionLocal() as s:
            return list(s.scalars(
                select(WashOrder).where(WashOrder.user_id == self.uid)
            ))

    def test_concurrent_settle_not_duplicated(self):
        async def run():
            return await asyncio.gather(
                wash._settle_wash_result(self.code, self.uid, "a.txt"),
                wash._settle_wash_result(self.code, self.uid, "b.txt"),
            )

        results = asyncio.run(run())
        orders = self._orders()
        total_cost = round(sum(o.cost for o in orders), 4)
        total_lines = sum(o.row_count for o in orders)
        self.assertAlmostEqual(total_cost, self.cost, places=4,
                               msg=f"结算金额不对：{[ (o.row_count, o.cost) for o in orders ]}")
        self.assertEqual(total_lines, 8000, f"结算行数不对：{total_lines}")
        # 两份各结算一次，不能出现同一份 5000 行被结算两次
        self.assertEqual(sorted(o.row_count for o in orders), [3000, 5000])


if __name__ == "__main__":
    unittest.main(verbosity=2)
