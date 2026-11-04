"""洗数据批量上传定价回归：多文件合并后按总行数计费，必须比分开计费便宜。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washbatch_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from database import (  # noqa: E402
    SessionLocal,
    calc_wash_cost,
    init_db,
    set_config,
    set_wash_prices,
)


class TestWashBatchPricing(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            # 阶梯价：10 万行以内 0.002/行，10 万行以上 0.001/行
            set_wash_prices(
                session,
                [
                    {"min_rows": 0, "price_per_row": 0.002},
                    {"min_rows": 100000, "price_per_row": 0.001},
                ],
            )
            set_config(session, "wash_price_floor", "0")

    def test_batch_total_cheaper_than_separate(self):
        """两个 6 万行文件：分开洗 240 元，合并 12 万行只花 120 元。"""
        with SessionLocal() as session:
            separate = calc_wash_cost(session, 60000) + calc_wash_cost(session, 60000)
            batch = calc_wash_cost(session, 120000)
            self.assertAlmostEqual(separate, 240.0)
            self.assertAlmostEqual(batch, 120.0)
            self.assertLess(batch, separate)

    def test_tier_boundary(self):
        """档位边界：99,999 行仍按第一档，100,000 行进入第二档。"""
        with SessionLocal() as session:
            self.assertAlmostEqual(calc_wash_cost(session, 99999), 199.998)
            self.assertAlmostEqual(calc_wash_cost(session, 100000), 100.0)

    def test_price_floor_applies_to_batch(self):
        """价格下限对合并后的总行数同样生效。"""
        with SessionLocal() as session:
            set_config(session, "wash_price_floor", "0.0015")
            self.assertAlmostEqual(calc_wash_cost(session, 120000), 180.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
