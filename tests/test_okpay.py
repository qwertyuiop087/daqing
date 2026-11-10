"""OKPay 收款回归：签名向量、回调入账、查单入账、会员与洗数据两种权益。

文档测试向量见 https://docs.okaypay.me/doc.html#vectors

运行：python tests/test_okpay.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="okpay_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["ADMIN_PASSWORD"] = "test123"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import webapp  # noqa: E402
from database import (  # noqa: E402
    PaymentOrder,
    SessionLocal,
    WashOrder,
    get_or_create_user,
    get_wash_balance,
    init_db,
    set_config,
)
from utils import okaypay_client as okpay  # noqa: E402

TEST_TOKEN = "TESTtoken123456789abcdefghijABCD"
SHOP_ID = "10001"


class TestOkpaySign(unittest.TestCase):
    def test_vector_a_request(self):
        params = dict(
            id=10001, amount="100.5", coin="USDT", unique_id="ORDER-20260628-001",
            timestamp=1782680000, nonce="a1b2c3d4e5",
        )
        self.assertEqual(
            okpay.build_base(params),
            "amount=100.5&coin=USDT&id=10001&nonce=a1b2c3d4e5"
            "&timestamp=1782680000&unique_id=ORDER-20260628-001",
        )
        self.assertEqual(
            okpay.sign(params, TEST_TOKEN),
            "7444ADFD8E4F4DA09D752DDF9345E0EE56DC25090FCFAF675DD042830E5E3F79",
        )

    def test_vector_b_callback(self):
        body = {
            "status": "success", "code": 200,
            "data": {
                "order_id": "abc123def456", "unique_id": "ORDER-20260628-001",
                "pay_user_id": 123456789, "amount": "100.5", "coin": "USDT",
                "status": 1, "type": "deposit",
            },
            "id": 10001,
        }
        self.assertEqual(
            okpay.sign(body, TEST_TOKEN),
            "64B09C8847849FA6921D8FFBDF8E406D4A8EA623E53970712350F61783403F7D",
        )

    def test_vector_c_edges(self):
        params = {
            "id": 7, "a": "0", "b": 0, "c": "", "d": None, "e": False,
            "f": "hello", "nest": {"x": "1", "y": "2"},
        }
        self.assertEqual(okpay.build_base(params), "a=0&b=0&e=false&f=hello&id=7&nest.x=1&nest.y=2")
        self.assertEqual(
            okpay.sign(params, TEST_TOKEN),
            "8BC0AF979075038025DDD51B6F4A2E6CF3FF9B5B5371EB2268D303F89883E92A",
        )

    def test_verify_rejects_tampering(self):
        body = {"status": "success", "code": 200, "data": {"unique_id": "X", "amount": "10"}, "id": 10001}
        body["sign"] = okpay.sign(body, TEST_TOKEN)
        self.assertTrue(okpay.verify(body, TEST_TOKEN))
        body["data"]["amount"] = "999"
        self.assertFalse(okpay.verify(body, TEST_TOKEN))


class TestOkpayCredit(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_config(session, "okpay_shop_id", SHOP_ID)
            set_config(session, "okpay_token", TEST_TOKEN)
            set_config(session, "usdt_rate", "7")

    def _make_order(self, order_id, user_id, days, amount):
        with SessionLocal() as session:
            get_or_create_user(session, user_id)
            session.add(
                PaymentOrder(
                    order_id=order_id, user_id=user_id, days=days,
                    amount=str(amount), coin="okpay", status="pending", pay_url="https://t.me/okpay?x=1",
                )
            )
            session.commit()

    def test_callback_credits_wash_balance(self):
        uid = 910001
        self._make_order("okpay_cb1", uid, 0, "100")

        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as client:
                body = {
                    "status": "success", "code": 200,
                    "data": {
                        "order_id": "abc123", "unique_id": "okpay_cb1",
                        "pay_user_id": 1, "amount": "100", "coin": "USDT",
                        "status": 1, "type": "deposit",
                    },
                    "id": 10001,
                }
                body["sign"] = okpay.sign(body, TEST_TOKEN)
                resp = await client.post("/okpay/callback", json=body)
                self.assertEqual(resp.status, 200, await resp.text())
                # 重复回调：幂等，不再加余额
                resp2 = await client.post("/okpay/callback", json=body)
                self.assertEqual(resp2.status, 200)

        asyncio.run(run())

        from sqlalchemy import select

        from database import get_usdt_rate

        with SessionLocal() as session:
            rows = list(session.scalars(
                select(WashOrder).where(WashOrder.user_id == uid)
            ))
            self.assertTrue(rows, "应写一条洗数据充值流水")
            rate = get_usdt_rate(session)
            self.assertAlmostEqual(get_wash_balance(session, uid), 100.0 * rate, places=2)

    def test_callback_bad_sign_is_rejected(self):
        self._make_order("okpay_cb2", 910002, 0, "10")

        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as client:
                body = {
                    "status": "success", "code": 200,
                    "data": {"order_id": "x", "unique_id": "okpay_cb2", "amount": "10",
                             "coin": "USDT", "status": 1, "type": "deposit"},
                    "id": 10001, "sign": "WRONG",
                }
                resp = await client.post("/okpay/callback", json=body)
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 400)

    def test_member_order_adds_days(self):
        uid = 910003
        self._make_order("okpay_mem", uid, 30, "3")

        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as client:
                body = {
                    "status": "success", "code": 200,
                    "data": {"order_id": "y", "unique_id": "okpay_mem", "amount": "3",
                             "coin": "USDT", "status": 1, "type": "deposit"},
                    "id": 10001,
                }
                body["sign"] = okpay.sign(body, TEST_TOKEN)
                return (await client.post("/okpay/callback", json=body)).status

        self.assertEqual(asyncio.run(run()), 200)
        from sqlalchemy import select

        from database import User, is_member

        with SessionLocal() as session:
            user = session.scalar(select(User).where(User.id == uid))
            self.assertTrue(is_member(user), "会员天数应已生效")
            self.assertIsNotNone(user.membership_expires_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
