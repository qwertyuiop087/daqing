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
                resp = await client.post("/api/login", json={"username": "admin", "password": "wrong"})
                self.assertEqual(resp.status, 401)
                # 错误用户名 → 401
                resp = await client.post("/api/login", json={"username": "nobody", "password": "test123"})
                self.assertEqual(resp.status, 401)
                # 正确登录
                resp = await client.post("/api/login", json={"username": "admin", "password": "test123"})
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
                self.assertIn("高级券", cfg2["lottery_prizes"])

                # 操作审计：配置保存应记录一条 config_save
                aud = await (await client.get("/api/audit")).json()
                self.assertTrue(any(
                    a.get("action") == "config_save" and a.get("admin") == "admin"
                    for a in aud["items"]
                ))

                # 洗数据账单：符号统一（扣费显示为负）、汇总按绝对值统计、含余额
                from database import SessionLocal, WashOrder, get_or_create_user, add_wash_balance
                with SessionLocal() as s:
                    get_or_create_user(s, 123)
                    add_wash_balance(s, 123, 57.0)  # 模拟充值后余额 70 - 13 = 57
                    s.add(WashOrder(user_id=123, filename="充值 $10 USDT", row_count=0, cost=70.0, type="recharge"))
                    s.add(WashOrder(user_id=123, filename="名单.txt", row_count=10000, cost=13.0, type="deduct"))
                    s.add(WashOrder(user_id=123, filename="网页后台手动扣费", row_count=0, cost=-5.0, type="deduct"))
                    s.add(WashOrder(user_id=123, filename="超时退款", row_count=0, cost=-2.5, type="refund"))
                    s.commit()
                wo = await (await client.get("/api/wash/orders")).json()
                self.assertEqual(wo["total_recharge"], 70.0)
                # 扣费按绝对值汇总（自动 +13 与手动 -5 都算扣了 18）
                self.assertEqual(wo["total_deduct"], 18.0)
                self.assertEqual(wo["total_refund"], 2.5)
                self.assertAlmostEqual(wo["total_balance"], 57.0)
                deduct_row = next(i for i in wo["items"] if i.get("filename") == "名单.txt")
                self.assertLess(deduct_row["cost"], 0)  # 扣费展示为负数
                manual_row = next(i for i in wo["items"] if i.get("filename") == "网页后台手动扣费")
                self.assertLess(manual_row["cost"], 0)
                # 分类筛选：只看退款
                wo_refund = await (await client.get("/api/wash/orders?type=refund")).json()
                self.assertEqual(wo_refund["count"], 1)
                self.assertEqual(wo_refund["items"][0]["type"], "refund")
                # 汇总仍是全局口径，不受筛选影响
                self.assertEqual(wo_refund["total_recharge"], 70.0)

                # 按用户搜索：ID 命中
                wo_uid = await (await client.get("/api/wash/orders?q=123")).json()
                self.assertGreaterEqual(wo_uid["count"], 1)
                self.assertTrue(all(i["user_id"] == 123 for i in wo_uid["items"]))

                # 按用户名搜索 + 类型组合：没有匹配项时返回空列表而不是报错
                wo_none = await (await client.get("/api/wash/orders?q=nobody&type=lottery")).json()
                self.assertEqual(wo_none["count"], 0)
                self.assertEqual(wo_none["items"], [])

                # 账目校验：充值 + 抽奖 + 退款 − 扣费
                self.assertAlmostEqual(wo["balance_check"], 70.0 + 0 - 18.0 - 2.5)

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
