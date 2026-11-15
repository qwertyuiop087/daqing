"""超时退款回归：同一任务只能退一次，且退款/删记录在同一事务。

运行：python tests/test_wash_refund.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="refund_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from sqlalchemy import select  # noqa: E402

from database import (  # noqa: E402
    AppConfig,
    SessionLocal,
    WashOrder,
    WASH_PENDING_TTL_SECONDS,
    get_config,
    get_or_create_user,
    get_wash_balance,
    get_washed_breakdown,
    init_db,
    set_config,
)


class TestRefundIdempotent(unittest.TestCase):
    def setUp(self):
        init_db()
        self.uid = 770001
        self.code = "Rfd0001"
        self.cost = 10.0
        # 余额先扣掉（模拟提交时预扣）
        with SessionLocal() as s:
            get_or_create_user(s, self.uid)
            old_ts = int(time.time()) - WASH_PENDING_TTL_SECONDS - 60
            set_config(
                s,
                f"wash_pending:{self.code}",
                f"{self.uid}:1000:{self.cost}:{old_ts}:"
                + json.dumps({"files": [1000], "charged": 0.0}, ensure_ascii=False),
            )

    def _refunds(self):
        with SessionLocal() as s:
            return list(
                s.scalars(
                    select(WashOrder).where(
                        WashOrder.user_id == self.uid, WashOrder.type == "refund"
                    )
                )
            )

    def test_refund_only_once(self):
        with SessionLocal() as s:
            get_washed_breakdown(s)
        first = sum(o.cost for o in self._refunds())
        self.assertAlmostEqual(-first, self.cost, places=4)

        # 再跑一次（模拟用户反复打开洗数据菜单）
        with SessionLocal() as s:
            get_washed_breakdown(s)
        second = sum(o.cost for o in self._refunds())
        self.assertAlmostEqual(-second, self.cost, places=4, msg="被重复退款了")

        with SessionLocal() as s:
            self.assertAlmostEqual(get_wash_balance(s, self.uid), self.cost, places=4)
            self.assertEqual(get_config(s, f"wash_pending:{self.code}", ""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
