"""后台保存回归：同源写请求能通过、跨站被拒、页面不缓存。

线上现象：后台改了配置点保存没反应。原因是 CSRF 同源校验过严
（Host 带端口 / Origin 不带端口就判成跨站），加上 HTML 被浏览器缓存。

运行：python tests/test_web_csrf.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="csrf_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ["ADMIN_PASSWORD"] = "test12345"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import database  # noqa: E402
import webapp  # noqa: E402


class TestAdminWrite(unittest.TestCase):
    def setUp(self):
        database.init_db()

    def test_same_origin_save_ok(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                await c.post("/api/login", json={"username": "admin", "password": "test12345"})
                r = await c.post(
                    "/api/config/referral_wash_rate",
                    json={"value": "6"},
                    headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"},
                )
                return r.status

        self.assertEqual(asyncio.run(run()), 200)

    def test_cross_site_blocked(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                await c.post("/api/login", json={"username": "admin", "password": "test12345"})
                r = await c.post(
                    "/api/config/referral_wash_rate",
                    json={"value": "7"},
                    headers={"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site"},
                )
                return r.status

        self.assertEqual(asyncio.run(run()), 403)

    def test_non_json_rejected(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                await c.post("/api/login", json={"username": "admin", "password": "test12345"})
                r = await c.post(
                    "/api/config/referral_wash_rate",
                    data="value=8",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                return r.status

        self.assertEqual(asyncio.run(run()), 415)

    def test_pages_not_cached(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                r1 = await c.get("/admin")
                r2 = await c.get("/")
                return r1.headers.get("Cache-Control", ""), r2.headers.get("Cache-Control", "")

        admin_cc, index_cc = asyncio.run(run())
        self.assertIn("no-store", admin_cc)
        self.assertIn("no-store", index_cc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
