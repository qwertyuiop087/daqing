"""介绍页静态资源回归：favicon.svg / og.jpg 必须能访问到。

背景：加了 og:image 和 favicon 之后，如果 webapp 不提供这两个文件，
链接预览和浏览器标签就会 404——等于白加 Meta。

运行：python tests/test_site_assets.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="siteasset_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import webapp  # noqa: E402
from database import init_db  # noqa: E402


class TestSiteAssets(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_favicon_and_og_image_are_served(self):
        app = webapp.build_app()

        async def flow():
            from aiohttp.test_utils import TestClient, TestServer

            async with TestClient(TestServer(app)) as client:
                r1 = await client.get("/favicon.svg")
                body1 = await r1.read()
                r2 = await client.get("/og.jpg")
                body2 = await r2.read()
                return r1, body1, r2, body2

        r1, body1, r2, body2 = asyncio.run(flow())
        self.assertEqual(r1.status, 200)
        self.assertIn("image/svg", r1.headers["Content-Type"])
        self.assertIn(b"<svg", body1[:200])
        self.assertEqual(r2.status, 200)
        self.assertIn("image/jpeg", r2.headers["Content-Type"])
        self.assertGreater(len(body2), 10_000)

    def test_unknown_assets_are_404(self):
        app = webapp.build_app()

        async def flow():
            from aiohttp.test_utils import TestClient, TestServer

            async with TestClient(TestServer(app)) as client:
                return [
                    (await client.get(p)).status
                    for p in ("/robots.txt", "/website/index.html", "/favicon.svg.bak")
                ]

        self.assertEqual(asyncio.run(flow()), [404, 404, 404])

    def test_index_still_served(self):
        app = webapp.build_app()

        async def flow():
            from aiohttp.test_utils import TestClient, TestServer

            async with TestClient(TestServer(app)) as client:
                r = await client.get("/")
                return r.status, await r.text()

        status, html = asyncio.run(flow())
        self.assertEqual(status, 200)
        self.assertIn("错落雨", html)
        self.assertIn('property="og:image"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
