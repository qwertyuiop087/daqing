"""洗数据回调注册回归：装饰器被误删会让按钮"点了没反应"。

线上事故：改 wash_recharge_kb 时按行替换，误删了
`@router.callback_query(F.data == "menu:wash")`，于是菜单里的
「🧼 洗数据」没有任何 handler 接手，点上去毫无反应。

运行：python tests/test_wash_routing.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washroute_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.okaypay as okaypay  # noqa: E402
import handlers.wash as wash  # noqa: E402

# 这些按钮必须都有 handler，否则用户点上去毫无反应
WASH_CALLBACKS = {
    "menu_wash",            # 🧼 洗数据（主菜单）
    "wash_recharge",        # 💳 充值洗数据
    "wash_recharge_custom",  # ✏️ 自定义金额
    "wash_check_order",     # ✅ 我已支付
    "wash_upload",          # 📤 上传文件洗数据
    "wash_submit",          # 🚀 开始洗数据
}

OKPAY_CALLBACKS = {
    "okpay_topup_start",
    "okpay_plans",
    "okpay_buy",
    "okpay_check",
}


class TestWashRouting(unittest.TestCase):
    def test_wash_callbacks_registered(self):
        names = {h.callback.__name__ for h in wash.router.callback_query.handlers}
        missing = WASH_CALLBACKS - names
        self.assertFalse(missing, f"这些洗数据回调没注册（装饰器可能被删）：{missing}")

    def test_wash_message_handlers_registered(self):
        names = {h.callback.__name__ for h in wash.router.message.handlers}
        for need in ("wash_got_file", "wash_recharge_custom_amount", "document_fallback"):
            self.assertIn(need, names)

    def test_okpay_callbacks_registered(self):
        names = {h.callback.__name__ for h in okaypay.router.callback_query.handlers}
        missing = OKPAY_CALLBACKS - names
        self.assertFalse(missing, f"OKPay 回调没注册：{missing}")

    def test_okpay_message_handlers_registered(self):
        names = {h.callback.__name__ for h in okaypay.router.message.handlers}
        self.assertIn("okpay_topup_amount", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
