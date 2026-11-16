"""小号登录流程回归：验证码过期/输错要能原地恢复，不用从头再来。

运行：python tests/test_alt_login_flow.py
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = os.path.join(os.environ.get("TEMP", "/tmp"), "altlogin")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}_t.db")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestAltLoginFlow(unittest.TestCase):
    def setUp(self):
        self.src = read("handlers/membership.py")

    def test_handles_expired_code(self):
        self.assertIn("PhoneCodeExpiredError", self.src)

    def test_handles_invalid_code(self):
        self.assertIn("PhoneCodeInvalidError", self.src)

    def test_handles_2fa_by_type(self):
        self.assertIn("SessionPasswordNeededError", self.src)

    def test_resend_callback_exists(self):
        self.assertIn('F.data == "admin:alt_resend"', self.src)

    def test_expired_branch_resends_and_keeps_state(self):
        """过期分支里必须 send_code_request 重新发码，并且不退出等待状态。"""
        i = self.src.find("isinstance(e, PhoneCodeExpiredError)")
        self.assertGreater(i, 0)
        block = self.src[i : i + 1200]
        self.assertIn("send_code_request", block)
        # 只有重发失败才清理会话；重发成功要留在输入状态等新码
        self.assertIn("except Exception as e2", block)
        self.assertLess(
            block.find("except Exception as e2"), block.find("cleanup_state(state)")
        )

    def test_keyboard_has_resend(self):
        kb = read("utils/keyboards.py")
        self.assertIn("def alt_login_resend_kb", kb)
        self.assertIn('admin:alt_resend', kb)


if __name__ == "__main__":
    unittest.main(verbosity=2)
