"""保存返佣必须真的写进去（端到端），以及 CSRF 不再误拦。

运行：python tests/test_config_save_flow.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="cfgsave_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ["ADMIN_PASSWORD"] = "test12345"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import database  # noqa: E402
import webapp  # noqa: E402


async def with_login(actions):
    app = webapp.build_app()
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "test12345"})
        assert r.status == 200, await r.text()
        out = []
        for method, url, body, headers in actions:
            if method == "post":
                r = await c.post(url, json=body, headers=headers or {})
                out.append((r.status, await r.text()))
            else:
                r = await c.get(url)
                out.append((r.status, await r.text()))
        return out


class TestConfigSave(unittest.TestCase):
    def setUp(self):
        database.init_db()
        with database.SessionLocal() as s:
            database.set_config(s, "referral_wash_rate", "5")
            database.set_config(s, "referral_member_rate", "10")

    def test_referral_rate_saved_and_effective(self):
        """保存返佣比例 → DB 里是新值 → 业务侧读到新值。"""
        asyncio.run(
            with_login(
                [
                    ("post", "/api/config/referral_wash_rate", {"value": "12"},
                     {"Sec-Fetch-Site": "same-origin"}),
                    ("post", "/api/config/referral_member_rate", {"value": "20"},
                     {"Sec-Fetch-Site": "same-origin"}),
                ]
            )
        )
        with database.SessionLocal() as s:
            self.assertEqual(database.get_config(s, "referral_wash_rate", ""), "12")
            self.assertEqual(database.get_config(s, "referral_member_rate", ""), "20")
            wash, member = database.get_referral_rates(s)
            self.assertAlmostEqual(wash, 12.0)
            self.assertAlmostEqual(member, 20.0)

    def test_save_without_sec_fetch_header_works(self):
        """老浏览器/反代没有 Sec-Fetch-Site 时不能被误拦。"""
        results = asyncio.run(
            with_login(
                [("post", "/api/config/referral_wash_rate", {"value": "7"}, None)]
            )
        )
        self.assertEqual(results[0][0], 200, results[0][1])
        with database.SessionLocal() as s:
            self.assertEqual(database.get_config(s, "referral_wash_rate", ""), "7")

    def test_cross_site_still_blocked(self):
        results = asyncio.run(
            with_login(
                [("post", "/api/config/referral_wash_rate", {"value": "9"},
                  {"Sec-Fetch-Site": "cross-site"})]
            )
        )
        self.assertEqual(results[0][0], 403, results[0][1])

    def test_version_public(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                r = await c.get("/api/version")
                return r.status, await r.json()

        status, data = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertTrue(data.get("version"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
