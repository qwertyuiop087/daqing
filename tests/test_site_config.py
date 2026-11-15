"""网站设置端到端回归：保存后公开接口与官网立刻拿到新值。

运行：python tests/test_site_config.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="sitecfg_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ["ADMIN_PASSWORD"] = "test12345"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import database  # noqa: E402
import webapp  # noqa: E402

FIELDS = [
    "site_title", "brand", "hero_highlight", "hero_subtitle", "accent",
    "contact1", "contact2", "contact1_label", "contact2_label", "bot_link",
    "mock_name", "mock_status", "mock_chip1", "mock_chip2",
    "toast_prefix", "toast_footer", "footer_text", "footer_note",
    "logo_icon", "logo_suffix",
]


class TestSiteConfig(unittest.TestCase):
    def setUp(self):
        database.init_db()

    def test_save_then_public_reflects(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                await c.post("/api/login", json={"username": "admin", "password": "test12345"})
                body = {k: f"V_{k}" for k in FIELDS}
                body["accent"] = "#123456"
                body["contact1"] = "@hello"
                r = await c.post("/api/site", json=body,
                                 headers={"Sec-Fetch-Site": "same-origin"})
                status = r.status
                pub = await (await c.get("/api/public/site")).json()
                return status, pub.get("site", {})

        status, site = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertEqual(site["brand"], "V_brand")
        self.assertEqual(site["accent"], "#123456")
        self.assertEqual(site["contact1"], "@hello")

    def test_unknown_field_rejected(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                await c.post("/api/login", json={"username": "admin", "password": "test12345"})
                r = await c.post("/api/site", json={"hack": "x"},
                                 headers={"Sec-Fetch-Site": "same-origin"})
                return r.status

        self.assertEqual(asyncio.run(run()), 400)

    def test_all_frontend_fields_accepted(self):
        """前端表单里的每个字段后端都必须接受（否则整次保存会失败）。"""
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as c:
                await c.post("/api/login", json={"username": "admin", "password": "test12345"})
                body = {k: f"v_{k}" for k in FIELDS}
                r = await c.post("/api/site", json=body,
                                 headers={"Sec-Fetch-Site": "same-origin"})
                return r.status

        self.assertEqual(asyncio.run(run()), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
