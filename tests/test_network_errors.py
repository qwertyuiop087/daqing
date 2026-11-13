"""网络类异常不应被当成"机器人故障"刷屏。运行：python tests/test_network_errors.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_TMP = tempfile.mkdtemp(prefix="neterr_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from utils.helpers import is_benign_update_error, is_network_error  # noqa: E402


class FakeNetworkError(Exception):
    pass


FakeNetworkError.__name__ = "TelegramNetworkError"


class TestNetworkErrors(unittest.TestCase):
    def test_client_connector_reset(self):
        exc = FakeNetworkError(
            "HTTP Client says - ClientConnectorError: Cannot connect to host "
            "api.telegram.org:443 ssl:default [Connection reset by peer]"
        )
        self.assertTrue(is_network_error(exc))
        self.assertFalse(is_benign_update_error(exc))

    def test_timeout_and_dns(self):
        self.assertTrue(is_network_error(TimeoutError("timed out")))
        self.assertTrue(is_network_error(RuntimeError("Temporary failure in name resolution")))

    def test_business_error_is_not_network(self):
        exc = ValueError("实付金额不足")
        self.assertFalse(is_network_error(exc))

    def test_benign_still_works(self):
        exc = RuntimeError("Bad Request: message is not modified")
        self.assertTrue(is_benign_update_error(exc))
        self.assertFalse(is_network_error(exc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
