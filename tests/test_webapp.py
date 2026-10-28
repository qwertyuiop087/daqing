"""内置网站/后台（webapp）冒烟测试：登录 → 配置读写 → 概览 → 页面。"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="webapp_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["ADMIN_PASSWORD"] = "test123"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import webapp  # noqa: E402
from database import SessionLocal, User, init_db  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


class TestWebapp(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_full_flow(self):
        async def run():
            app = webapp.build_app()
            async with TestClient(TestServer(app)) as client:
                # 未登录 → 401
                resp = await client.get("/api/config")
                self.assertEqual(resp.status, 401)
                # 错误密码 → 401
                resp = await client.post("/api/login", json={"password": "wrong"})
                self.assertEqual(resp.status, 401)
                # 正确登录
                resp = await client.post("/api/login", json={"password": "test123"})
                self.assertEqual(resp.status, 200)

                # 读配置
                cfg = await (await client.get("/api/config")).json()
                self.assertIn("lottery_prizes", cfg)
                self.assertIn("plans", cfg)
                self.assertIn("welcome_text", cfg)

                # 改抽奖奖品（含高级券）
                resp = await client.post("/api/config/lottery_prizes", json={"value": "天:1:50,高级券:1:10"})
                self.assertEqual(resp.status, 200, await resp.text())
                cfg2 = await (await client.get("/api/config")).json()
                self.assertIn("ticket", cfg2["lottery_prizes"])

                # 非法输入 → 400
                resp = await client.post("/api/config/lottery_prizes", json={"value": "天:1:60,天:2:50"})
                self.assertEqual(resp.status, 400)

                # 通知模板（用户名占位符）
                resp = await client.post("/api/config/lottery_notify_template", json={"value": "用户 {username} 中奖 {prize}"})
                self.assertEqual(resp.status, 200, await resp.text())

                # 套餐 / 欢迎语 / 洗数据
                self.assertEqual((await client.post("/api/config/plans", json={"value": "7:1,30:3,365:25"})).status, 200)
                self.assertEqual((await client.post("/api/config/welcome_text", json={"value": "你好，{昵称}！"})).status, 200)
                self.assertEqual((await client.post("/api/config/wash_prices", json={"value": "0:0.0013,100000:0.0010"})).status, 200)
                self.assertEqual((await client.post("/api/config/wash_quota", json={"value": "1000000"})).status, 200)

                # 重置
                self.assertEqual((await client.post("/api/reset/lottery_prizes")).status, 200)

                # 公开套餐列表（无需登录，与机器人配置同步）
                plans_resp = await (await client.get("/api/public/plans")).json()
                self.assertIn("items", plans_resp)
                self.assertTrue(len(plans_resp["items"]) >= 1)

                # 公开中奖播报（无需登录），用户名打码
                feed = await (await client.get("/api/public/feed")).json()
                self.assertIn("items", feed)
                if feed["items"]:
                    first = feed["items"][0]
                    self.assertIn("user", first)
                    self.assertTrue(first["user"].endswith("**") or first["user"] == "匿名用户")

                # 概览
                st = await (await client.get("/api/stats")).json()
                self.assertIn("total_users", st)

                # 成员管理：搜索 + 调整天数/余额
                with SessionLocal() as s:
                    if s.get(User, 555) is None:
                        s.add(User(id=555, username="u555", first_name="王五"))
                        s.commit()
                resp = await client.get("/api/members?q=555")
                self.assertEqual(resp.status, 200)
                items = (await resp.json())["items"]
                self.assertGreaterEqual(len(items), 1)
                # 加 7 天会员 + 5 元余额
                resp = await client.post(
                    "/api/members/adjust",
                    json={"user_id": 555, "days_delta": 7, "balance_delta": 5},
                )
                self.assertEqual(resp.status, 200, await resp.text())
                u = (await (await client.get("/api/members?q=555")).json())["items"][0]
                self.assertIn("天", u["member_text"])
                self.assertAlmostEqual(u["wash_balance"], 5.0, places=4)
                # 扣减超过余额 → 400
                resp = await client.post(
                    "/api/members/adjust",
                    json={"user_id": 555, "days_delta": 0, "balance_delta": -10},
                )
                self.assertEqual(resp.status, 400)

                # 网站设置：读取 + 保存 + 公开接口
                site_resp = await (await client.get("/api/site")).json()
                self.assertIn("site", site_resp)
                self.assertIn("brand", site_resp["site"])
                resp = await client.post("/api/site", json={"brand": "测试品牌", "accent": "#123456"})
                self.assertEqual(resp.status, 200, await resp.text())
                pub = await (await client.get("/api/public/site")).json()
                self.assertEqual(pub["site"]["brand"], "测试品牌")

                # 页面
                self.assertEqual((await client.get("/")).status, 200)
                self.assertEqual((await client.get("/admin")).status, 200)
                self.assertEqual((await client.get("/health")).status, 200)

                # 退出后 → 401
                await client.post("/api/logout")
                self.assertEqual((await client.get("/api/config")).status, 401)
            print("WEBAPP_OK")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
