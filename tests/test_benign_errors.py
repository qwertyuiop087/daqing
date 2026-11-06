"""用户操作类异常不该当故障告警：连点按钮 / 点开很旧的按钮。

线上现象：管理员私聊收到「⚠️ 机器人出现异常
类型：TelegramBadRequest
详情：Bad Request: message is not modified ...」

原因：用户连点同一个按钮，机器人把消息改成和当前一模一样的内容，
Telegram 返回 400 —— 业务上没有任何问题，不该刷管理员。

运行：python tests/test_benign_errors.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from utils.helpers import is_benign_update_error  # noqa: E402


class TestBenignErrors(unittest.TestCase):
    def test_not_modified_is_benign(self):
        exc = Exception(
            "Telegram server says - Bad Request: message is not modified: specified new "
            "message content and reply markup are exactly the same as a current content "
            "and reply markup of the message"
        )
        self.assertTrue(is_benign_update_error(exc))

    def test_old_message_cannot_be_edited(self):
        self.assertTrue(
            is_benign_update_error(Exception("Bad Request: message can't be edited"))
        )

    def test_deleted_message(self):
        self.assertTrue(
            is_benign_update_error(Exception("Bad Request: message to edit not found"))
        )

    def test_expired_callback(self):
        self.assertTrue(
            is_benign_update_error(Exception("Bad Request: query is too old and response timeout expired"))
        )

    def test_real_errors_are_not_benign(self):
        """真正的故障不能被吞掉，否则管理员永远收不到告警。"""
        for msg in (
            "Telegram server says - Bad Request: chat not found",
            "Telegram server says - Forbidden: bot was blocked by the user",
            "Connection reset by peer",
            "sqlalchemy.exc.OperationalError: no such table: users",
        ):
            self.assertFalse(is_benign_update_error(Exception(msg)), msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
