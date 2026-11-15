"""用户余额回归：返佣进余额、余额→洗数据单向、提现冻结与退回。

运行：python tests/test_cash_balance.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="cash_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import database as db  # noqa: E402


class TestCashBalance(unittest.TestCase):
    UID = 880001

    def setUp(self):
        db.init_db()
        with db.SessionLocal() as s:
            db.get_or_create_user(s, self.UID, "u", "小王")
            from sqlalchemy import delete, update

            s.execute(update(db.User).where(db.User.id == self.UID).values(
                cash_balance=0.0, wash_balance=0.0))
            s.execute(delete(db.CashTransaction).where(db.CashTransaction.user_id == self.UID))
            s.execute(delete(db.WithdrawRequest).where(db.WithdrawRequest.user_id == self.UID))
            s.commit()
            db.add_cash_balance(s, self.UID, 100.0, "referral", "测试返佣")

    def test_referral_goes_to_cash(self):
        with db.SessionLocal() as s:
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 100.0)

    def test_transfer_to_wash_one_way(self):
        with db.SessionLocal() as s:
            ok, _ = db.transfer_cash_to_wash(s, self.UID, 30.0)
            self.assertTrue(ok)
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 70.0)
            self.assertAlmostEqual(db.get_wash_balance(s, self.UID), 30.0)
        # 反向接口不存在：洗数据余额不能转回用户余额
        self.assertFalse(hasattr(db, "transfer_wash_to_cash"))

    def test_transfer_more_than_balance_rejected(self):
        with db.SessionLocal() as s:
            ok, note = db.transfer_cash_to_wash(s, self.UID, 999.0)
            self.assertFalse(ok)
            self.assertIn("不足", note)
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 100.0)

    def test_withdraw_freezes_balance(self):
        with db.SessionLocal() as s:
            ok, _, rid = db.create_withdraw_request(s, self.UID, 40.0)
            self.assertTrue(ok)
            self.assertGreater(rid, 0)
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 60.0)
            # 同一个人不能同时有两笔待处理
            ok2, note2, _ = db.create_withdraw_request(s, self.UID, 10.0)
            self.assertFalse(ok2)
            self.assertIn("处理中", note2)

    def test_withdraw_reject_refunds(self):
        with db.SessionLocal() as s:
            ok, _, rid = db.create_withdraw_request(s, self.UID, 40.0)
            self.assertTrue(ok)
        with db.SessionLocal() as s:
            ok, note, uid, amount = db.finish_withdraw(s, rid, False, 1)
            self.assertTrue(ok)
            self.assertEqual(uid, self.UID)
            self.assertAlmostEqual(amount, 40.0)
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 100.0)

    def test_withdraw_approve_keeps_deducted(self):
        with db.SessionLocal() as s:
            ok, _, rid = db.create_withdraw_request(s, self.UID, 40.0)
        with db.SessionLocal() as s:
            ok, _, _, _ = db.finish_withdraw(s, rid, True, 1)
            self.assertTrue(ok)
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 60.0)
            # 重复处理不会重复退
            ok2, note2, _, _ = db.finish_withdraw(s, rid, False, 1)
            self.assertFalse(ok2)
            self.assertAlmostEqual(db.get_cash_balance(s, self.UID), 60.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
