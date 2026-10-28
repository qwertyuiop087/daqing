"""抽奖系统回归测试：奖品解析、普通抽奖、高级抽奖、兑换、每日限制。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="lottery_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from database import (  # noqa: E402
    SessionLocal,
    get_or_create_user,
    get_today_lottery_record,
    init_db,
    set_lottery_premium_prizes,
    set_lottery_prizes,
    set_lottery_redeem,
)
from utils.lottery import (  # noqa: E402
    draw_ordinary,
    draw_premium,
    mask_name,
    parse_prizes_input,
    parse_redeem_input,
    redeem_prize,
)


class TestMaskName(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask_name("张三"), "张*")
        self.assertEqual(mask_name("王小二"), "王*二")
        self.assertEqual(mask_name("买买提明"), "买**明")
        self.assertEqual(mask_name("李"), "李")
        self.assertEqual(mask_name(""), "")
        self.assertEqual(mask_name("  "), "")


class TestParse(unittest.TestCase):
    def test_prizes_valid(self):
        prizes, err = parse_prizes_input("天:1:40,余额:5:5,高级券:1:3")
        self.assertIsNone(err)
        self.assertEqual(len(prizes), 3)
        self.assertEqual(prizes[0], {"type": "days", "value": 1, "prob": 40.0})
        self.assertEqual(prizes[2], {"type": "ticket", "value": 1, "prob": 3.0})

    def test_prizes_over_100(self):
        prizes, err = parse_prizes_input("天:1:60,天:2:50")
        self.assertIsNone(prizes)
        self.assertIn("100", err)

    def test_prizes_bad_type(self):
        _, err = parse_prizes_input("金币:1:10")
        self.assertIsNotNone(err)

    def test_redeem_valid(self):
        items, err = parse_redeem_input("1:天:10,2:余额:20")
        self.assertIsNone(err)
        self.assertEqual(items[0], {"cost": 1, "type": "days", "value": 10})

    def test_redeem_bad_cost(self):
        _, err = parse_redeem_input("0:天:10")
        self.assertIsNotNone(err)


class TestDraw(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_draw_ordinary_days_and_daily_limit(self):
        with SessionLocal() as s:
            user = get_or_create_user(s, 111, "u1", "U")
            set_lottery_prizes(s, [{"type": "days", "value": 3, "prob": 100}])
            res = draw_ordinary(s, user)
            self.assertEqual(res["label"], "3 天会员")
            s.refresh(user)
            self.assertIsNotNone(get_today_lottery_record(s, user.id))

    def test_draw_ordinary_ticket(self):
        with SessionLocal() as s:
            user = get_or_create_user(s, 222, "u2", "U")
            set_lottery_prizes(s, [{"type": "ticket", "value": 2, "prob": 100}])
            res = draw_ordinary(s, user)
            self.assertEqual(res["label"], "高级抽奖 2 次")
            s.refresh(user)
            self.assertEqual(user.lottery_tickets, 2)

    def test_draw_premium_consumes_ticket(self):
        with SessionLocal() as s:
            user = get_or_create_user(s, 333, "u3", "U")
            user.lottery_tickets = 1
            s.commit()
            set_lottery_premium_prizes(s, [{"type": "days", "value": 30, "prob": 100}])
            res = draw_premium(s, user)
            self.assertEqual(res["label"], "30 天会员")
            s.refresh(user)
            self.assertEqual(user.lottery_tickets, 0)

    def test_redeem_prize(self):
        with SessionLocal() as s:
            user = get_or_create_user(s, 444, "u4", "U")
            user.lottery_tickets = 2
            s.commit()
            set_lottery_redeem(s, [{"cost": 1, "type": "wash_balance", "value": 10}])
            label = redeem_prize(s, user, {"cost": 1, "type": "wash_balance", "value": 10})
            self.assertEqual(label, "¥10.0000 洗数据余额")
            s.refresh(user)
            self.assertEqual(user.lottery_tickets, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
