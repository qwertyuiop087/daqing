"""提醒与日报回归：到期提醒只发对应档位、每天只发一次、日报能生成。

运行：python tests/test_reminders.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="remind_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from database import (  # noqa: E402
    SessionLocal,
    User,
    get_config,
    get_or_create_user,
    init_db,
    utcnow,
)
from utils import reminders  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class TestExpiryReminder(unittest.TestCase):
    def setUp(self):
        init_db()
        # 清掉今天已经发过的标记
        today = reminders._today_bj()
        with SessionLocal() as s:
            from database import set_config

            set_config(s, f"expiry_remind_date:{today}", "")
            from sqlalchemy import delete

            from database import AppConfig

            s.execute(delete(AppConfig).where(AppConfig.key.like("expiry_remind_date:%")))
            s.commit()

    def _set_member(self, uid, days):
        # remaining_days 会把不足一天的部分向上取整，所以用 days-1 天零 1 小时
        with SessionLocal() as s:
            u = get_or_create_user(s, uid)
            u.membership_expires_at = utcnow() + timedelta(days=max(0, days - 1), hours=1)
            s.commit()

    def test_only_reminds_7_3_1(self):
        self._set_member(500001, 3)
        self._set_member(500002, 5)  # 不该提醒
        bot = FakeBot()
        sent = asyncio.run(reminders.send_expiry_reminders(bot))
        targets = {cid for cid, _ in bot.sent}
        self.assertIn(500001, targets)
        self.assertNotIn(500002, targets)
        self.assertGreaterEqual(sent, 1)

    def test_only_once_per_day(self):
        self._set_member(500003, 1)
        bot = FakeBot()
        asyncio.run(reminders.send_expiry_reminders(bot))
        first = len(bot.sent)
        asyncio.run(reminders.send_expiry_reminders(bot))
        self.assertEqual(len(bot.sent), first, "同一天重复发了提醒")


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_report_contains_sections(self):
        text = reminders.collect_daily_report()
        for kw in ("对账日报", "收入", "消耗", "对账", "待处理"):
            self.assertIn(kw, text)

    def test_report_once_per_day(self):
        bot = FakeBot()
        # 清标记
        today = reminders._today_bj()
        from sqlalchemy import delete

        from database import AppConfig

        with SessionLocal() as s:
            s.execute(delete(AppConfig).where(AppConfig.key.like("daily_report_date:%")))
            s.commit()
        r1 = asyncio.run(reminders.send_daily_report(bot))
        r2 = asyncio.run(reminders.send_daily_report(bot))
        self.assertTrue(r1)
        self.assertFalse(r2, "同一天重复发了日报")


if __name__ == "__main__":
    unittest.main(verbosity=2)
