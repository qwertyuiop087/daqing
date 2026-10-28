"""支付自动到账回归测试：买会员/洗数据充值自动入账、通知、幂等。"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="autocredit_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from sqlalchemy import select  # noqa: E402

from database import (  # noqa: E402
    PaymentOrder,
    SessionLocal,
    User,
    WashOrder,
    get_or_create_user,
    init_db,
    utcnow,
)
import main as payments  # noqa: E402  （自动到账逻辑内联在 main.py）


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, path, payload):
        self.calls.append((path, payload))
        return {"ok": True, "data": {"statusText": "paid", "txid": "tx1", "amount": "10"}}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def fake_rate(session):
    return 7.0


class TestAutoCredit(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_buy_and_wash_autocredit(self):
        async def run():
            with SessionLocal() as s:
                get_or_create_user(s, 111, "u1", "U")
                s.add(PaymentOrder(order_id="buy_1", user_id=111, days=30, amount="3", status="pending"))
                s.add(PaymentOrder(order_id="wash_1", user_id=111, days=0, amount="10", status="pending"))
                s.commit()

            bot = FakeBot()
            fake = FakeClient()
            with mock.patch.object(payments, "get_client", lambda: fake), \
                 mock.patch.object(payments, "get_usdt_rate_async", fake_rate):
                await payments._sweep_payments(bot)

            with SessionLocal() as s:
                buy = s.scalar(select(PaymentOrder).where(PaymentOrder.order_id == "buy_1"))
                wash = s.scalar(select(PaymentOrder).where(PaymentOrder.order_id == "wash_1"))
                self.assertEqual(buy.status, "paid")
                self.assertEqual(buy.txid, "tx1")
                self.assertEqual(wash.status, "paid")
                u = s.get(User, 111)
                self.assertIsNotNone(u.membership_expires_at)          # 加了 30 天
                self.assertAlmostEqual(u.wash_balance, 70.0, places=4)  # 10 USDT × 7
                rec = s.scalar(select(WashOrder).where(WashOrder.type == "recharge"))
                self.assertIsNotNone(rec)

            # 两个到账通知都发了
            self.assertEqual(len(bot.sent), 2)
            self.assertTrue(any("会员已自动到账" in t for _, t in bot.sent))
            self.assertTrue(any("洗数据充值已自动到账" in t for _, t in bot.sent))

            # 幂等：再扫一轮不应重复入账、不再通知
            await payments._sweep_payments(bot)
            with SessionLocal() as s:
                u = s.get(User, 111)
                self.assertAlmostEqual(u.wash_balance, 70.0, places=4)
            self.assertEqual(len(bot.sent), 2)

        asyncio.run(run())


    def test_order_timeout(self):
        """创建超过 5 分钟的未支付订单 → 标记 timeout 并通知用户。"""
        async def run():
            from datetime import timedelta
            with SessionLocal() as s:
                get_or_create_user(s, 222, "u2", "U")
                o = PaymentOrder(order_id="old_1", user_id=222, days=30, amount="3", status="pending")
                o.created_at = utcnow() - timedelta(minutes=6)
                s.add(o)
                s.commit()

            bot = FakeBot()
            with mock.patch.object(payments, "get_client", lambda: FakeClient()):
                await payments._sweep_payments(bot)

            with SessionLocal() as s:
                o2 = s.scalar(select(PaymentOrder).where(PaymentOrder.order_id == "old_1"))
                self.assertEqual(o2.status, "timeout")
            self.assertTrue(any("订单已超时" in t for _, t in bot.sent))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
