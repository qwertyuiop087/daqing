"""后台调整用户余额回归（单 client 内完成，避免触发登录限流）。

运行：python tests/test_admin_cash_adjust.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="adjcash_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ["ADMIN_PASSWORD"] = "test12345"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import database  # noqa: E402
import webapp  # noqa: E402

UID = 990001


async def run_session(actions):
    """登录一次，按顺序执行动作，返回每个动作的结果。"""
    app = webapp.build_app()
    results = []
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "test12345"})
        assert r.status == 200, await r.text()
        for method, url, body in actions:
            if method == "post":
                r = await c.post(url, json=body, headers={"Sec-Fetch-Site": "same-origin"})
                results.append((r.status, await r.text()))
            else:
                r = await c.get(url)
                results.append((r.status, await r.text()))
    return results


class TestAdminCashAdjust(unittest.TestCase):
    def setUp(self):
        database.init_db()
        with database.SessionLocal() as s:
            database.get_or_create_user(s, UID, "adj", "调整测试")
            from sqlalchemy import delete, update

            s.execute(
                update(database.User)
                .where(database.User.id == UID)
                .values(cash_balance=0.0, wash_balance=0.0)
            )
            s.execute(delete(database.CashTransaction).where(database.CashTransaction.user_id == UID))
            s.commit()

    def cash(self):
        with database.SessionLocal() as s:
            return database.get_cash_balance(s, UID)

    def test_add_then_deduct_then_list(self):
        results = asyncio.run(
            run_session(
                [
                    ("post", "/api/members/adjust", {"user_id": UID, "cash_delta": 25.5}),
                    ("post", "/api/members/adjust", {"user_id": UID, "cash_delta": -5}),
                    ("get", f"/api/members?q={UID}", None),
                ]
            )
        )
        self.assertEqual(results[0][0], 200, results[0][1])
        self.assertAlmostEqual(self.cash(), 20.5, places=4)
        self.assertEqual(results[2][0], 200, results[2][1])
        import json as _json
        items = _json.loads(results[2][1])["items"]
        row = [i for i in items if i["id"] == UID][0]
        self.assertAlmostEqual(row["cash_balance"], 20.5, places=4)

    def test_deduct_too_much_rejected(self):
        results = asyncio.run(
            run_session(
                [("post", "/api/members/adjust", {"user_id": UID, "cash_delta": -5})]
            )
        )
        self.assertEqual(results[0][0], 400, results[0][1])
        self.assertAlmostEqual(self.cash(), 0.0, places=4)

    def test_manual_transaction_recorded(self):
        asyncio.run(
            run_session(
                [("post", "/api/members/adjust", {"user_id": UID, "cash_delta": 8})]
            )
        )
        with database.SessionLocal() as s:
            rows = database.get_cash_transactions(s, UID)
        self.assertTrue(
            any(r.kind == "manual" and abs(r.amount - 8) < 1e-6 for r in rows),
            [(r.kind, r.amount) for r in rows],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
