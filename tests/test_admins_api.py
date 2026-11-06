"""管理员功能回归：总管理员（配额/价格下限/管理员管理）与普通管理员权限。

需求：
1. 总管理员控制「累计洗数据配额」「价格下限」「管理员管理」，其它管理员都不能动；
2. 没配 SUPER_ADMIN_IDS 时不能让这些功能没人能用（退化为 ADMIN_IDS 第一个）；
3. 网页后台可以查看 / 添加 / 删除管理员。

运行：python tests/test_admins_api.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="admins_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["ADMIN_PASSWORD"] = "test123"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import utils.roles as roles  # noqa: E402
import webapp  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402


class TestRoleFallback(unittest.TestCase):
    def setUp(self):
        self._orig = roles.settings

    def tearDown(self):
        roles.settings = self._orig

    def _fake(self, supers, admins):
        roles.settings = SimpleNamespace(
            SUPER_ADMIN_IDS=set(supers), ADMIN_IDS=set(admins)
        )

    def test_super_from_env(self):
        self._fake([1], [1, 2])
        self.assertTrue(roles.is_super_admin(1))
        self.assertFalse(roles.is_super_admin(2))
        self.assertTrue(roles.is_admin(2))

    def test_fallback_when_super_not_configured(self):
        """没配 SUPER_ADMIN_IDS：ADMIN_IDS 里第一个自动成为总管理员。"""
        self._fake([], [55, 66])
        self.assertEqual(roles.super_admin_ids(), [55])
        self.assertTrue(roles.is_super_admin(55))
        self.assertFalse(roles.is_super_admin(66))
        self.assertTrue(roles.is_admin(66))

    def test_nobody_super_when_no_admins(self):
        self._fake([], [])
        self.assertEqual(roles.super_admin_ids(), [])
        self.assertFalse(roles.is_super_admin(1))


class TestAdminsApi(unittest.TestCase):
    def setUp(self):
        init_db()
        # 清掉动态管理员，保证用例独立
        with SessionLocal() as session:
            from database import set_config
            set_config(session, "admin_ids", "")

    def test_admin_management_flow(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/admins")
                self.assertEqual(resp.status, 401)  # 未登录

                await client.post(
                    "/api/login", json={"username": "admin", "password": "test123"}
                )

                d = await (await client.get("/api/admins")).json()
                self.assertIn("super_admins", d)
                self.assertIn("db_admins", d)

                # 添加管理员
                r = await client.post("/api/admins/add", json={"user_id": "1001"})
                self.assertEqual(r.status, 200, await r.text())
                self.assertTrue((await r.json())["added"])
                d = await (await client.get("/api/admins")).json()
                self.assertIn(1001, d["db_admins"])

                # 重复添加：不报错，added=False
                r = await client.post("/api/admins/add", json={"user_id": "1001"})
                self.assertFalse((await r.json())["added"])

                # 非法 ID
                r = await client.post("/api/admins/add", json={"user_id": "abc"})
                self.assertEqual(r.status, 400)
                self.assertIn("detail", await r.json())

                # 删除管理员
                r = await client.post("/api/admins/remove", json={"user_id": "1001"})
                self.assertEqual(r.status, 200, await r.text())
                d = await (await client.get("/api/admins")).json()
                self.assertNotIn(1001, d["db_admins"])

                # 审计里有记录
                aud = await (await client.get("/api/audit")).json()
                actions = {a["action"] for a in aud["items"]}
                self.assertIn("admin_add", actions)
                self.assertIn("admin_remove", actions)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
