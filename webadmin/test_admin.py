"""webadmin 冒烟测试：登录 → 读配置 → 改抽奖奖品/通知模板 → 概览 → 页面。"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="webadmin_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["ADMIN_PASSWORD"] = "test123"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def test_flow():
    client = TestClient(main.app)
    # 未登录访问配置 → 401
    assert client.get("/api/config").status_code == 401
    # 错误密码 → 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    # 正确登录
    r = client.post("/api/login", json={"password": "test123"})
    assert r.status_code == 200, r.text
    # 读配置
    cfg = client.get("/api/config").json()
    assert "lottery_prizes" in cfg and "plans" in cfg and "welcome_text" in cfg
    # 改抽奖奖品（合法）
    r = client.post("/api/config/lottery_prizes", json={"value": "天:1:50,高级券:1:10"})
    assert r.status_code == 200, r.text
    cfg2 = client.get("/api/config").json()
    assert "ticket" in cfg2["lottery_prizes"]
    # 非法输入（概率超 100）→ 400
    assert client.post("/api/config/lottery_prizes", json={"value": "天:1:60,天:2:50"}).status_code == 400
    # 通知模板（含用户名占位符）
    r = client.post("/api/config/lottery_notify_template", json={"value": "用户 {username} 中奖 {prize}"})
    assert r.status_code == 200, r.text
    assert "{username}" in client.get("/api/config").json()["lottery_notify_template"]
    # 套餐
    r = client.post("/api/config/plans", json={"value": "7:1,30:3,365:25"})
    assert r.status_code == 200, r.text
    # 欢迎语（含昵称占位符）
    r = client.post("/api/config/welcome_text", json={"value": "你好，{昵称}！欢迎使用<b>大晴分包机器人</b>"})
    assert r.status_code == 200, r.text
    # 洗数据定价 / 配额
    assert client.post("/api/config/wash_prices", json={"value": "0:0.0013,100000:0.0010"}).status_code == 200
    assert client.post("/api/config/wash_quota", json={"value": "1000000"}).status_code == 200
    # 重置
    assert client.post("/api/reset/lottery_prizes").status_code == 200
    # 概览
    st = client.get("/api/stats").json()
    assert "total_users" in st and "records" in st
    # 页面
    assert client.get("/").status_code == 200        # 落地页
    assert client.get("/admin").status_code == 200   # 管理页
    print("WEBADMIN_OK")


if __name__ == "__main__":
    test_flow()
