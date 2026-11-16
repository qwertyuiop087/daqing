"""返佣默认值回归：新默认 3% / 5%，旧默认自动迁移，自定义值不动。

运行：python tests/test_referral_defaults.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="refdef_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import database as db  # noqa: E402


class TestReferralDefaults(unittest.TestCase):
    def setUp(self):
        db.init_db()
        with db.SessionLocal() as s:
            for k in ("referral_wash_rate", "referral_member_rate"):
                db.set_config(s, k, "")

    def test_defaults_are_four_and_ten(self):
        self.assertEqual(db.DEFAULT_REFERRAL_WASH_RATE, 4.0)
        self.assertEqual(db.DEFAULT_REFERRAL_MEMBER_RATE, 10.0)
        with db.SessionLocal() as s:
            wash, member = db.get_referral_rates(s)
        self.assertAlmostEqual(wash, 4.0)
        self.assertAlmostEqual(member, 10.0)

    def test_old_defaults_migrated(self):
        with db.SessionLocal() as s:
            db.set_config(s, "referral_wash_rate", "3")
            db.set_config(s, "referral_member_rate", "5")
            db.set_config(s, "referral_defaults_rev", "")
            changed = db.migrate_referral_defaults(s)
            self.assertTrue(changed)
            wash, member = db.get_referral_rates(s)
        self.assertAlmostEqual(wash, 4.0)
        self.assertAlmostEqual(member, 10.0)

    def test_custom_values_untouched_after_migration(self):
        """迁移跑过一次以后，后台手改的比例不会再被重启覆盖。"""
        with db.SessionLocal() as s:
            db.migrate_referral_defaults(s)  # 先完成本次迁移
            db.set_config(s, "referral_wash_rate", "12")
            db.set_config(s, "referral_member_rate", "20")
            self.assertEqual(db.migrate_referral_defaults(s), [])
            wash, member = db.get_referral_rates(s)
        self.assertAlmostEqual(wash, 12.0)
        self.assertAlmostEqual(member, 20.0)

    def test_migration_idempotent(self):
        with db.SessionLocal() as s:
            db.migrate_referral_defaults(s)
            self.assertEqual(db.migrate_referral_defaults(s), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
