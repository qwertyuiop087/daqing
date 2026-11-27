"""洗数据上传大小上限回归：20.0MB 的文件必须给出人能看懂的提示。

线上问题：用户把文件切成 20.0MB 一份传上来，机器人只会回一行英文
`下载失败：Telegram server says - Bad Request: file is too big`，
没人知道该怎么办。Telegram 的机器人 API 只能下载 20MB 以内，
正好 20.0MB 会被服务端直接拒，所以：
1. 上限收到 19MB（留 1MB 余量），超限在下载前就拦；
2. 服务端仍拒绝时，也给「用文件切割切小一点」这种可操作的提示。

运行：python tests/test_wash_upload_size.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washsize_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
from config import settings  # noqa: E402
from database import init_db  # noqa: E402

USER_ID = 515151
MB = 1024 * 1024


class FakeState:
    def __init__(self, data):
        self.data = dict(data)

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)


class FakeMessage:
    def __init__(self, file_name, file_size):
        self.document = SimpleNamespace(
            file_id="fid", file_name=file_name, file_size=file_size
        )
        self.from_user = SimpleNamespace(id=USER_ID)
        self.chat = SimpleNamespace(id=USER_ID)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=1)


class FakeBot:
    def __init__(self, server_size=None, download_error=None):
        self.server_size = server_size
        self.download_error = download_error
        self.downloaded = False

    async def get_file(self, file_id):
        return SimpleNamespace(
            file_path="remote", file_size=self.server_size
        )

    async def download_file(self, file_path, destination):
        self.downloaded = True
        if self.download_error:
            raise RuntimeError(self.download_error)
        with open(destination, "w", encoding="utf-8") as fh:
            fh.write("13800000001\n")


class TestWashUploadSize(unittest.TestCase):
    def setUp(self):
        init_db()
        self.workdir = tempfile.mkdtemp(prefix="washsize_work_")
        self.state = FakeState({"workdir": self.workdir, "files": []})

    def test_limit_is_under_telegram_cap(self):
        """上限必须低于 20MB：正好 20MB Telegram 会拒。"""
        self.assertEqual(settings.MAX_FILE_SIZE, 19 * MB)

    def test_20mb_batch_is_rejected_before_download(self):
        """20.0MB 的份：下载前就拦，提示去切小，而不是英文报错。"""
        bot = FakeBot(server_size=20 * MB)
        msg = FakeMessage("合并结果_去重_part18.txt", 20 * MB)

        asyncio.run(wash_module.wash_got_file(msg, self.state, bot))

        self.assertFalse(bot.downloaded, "超限文件不该去下载")
        shown = " ".join(msg.answers)
        self.assertIn("19MB", shown)
        self.assertIn("文件切割", shown)
        self.assertNotIn("Bad Request", shown)

    def test_server_refusal_on_18mb_still_explains(self):
        """18MB 但仍被服务端拒绝（刚好卡线/异常）：也要给可操作提示。"""
        bot = FakeBot(
            server_size=18 * MB,
            download_error="Telegram server says - Bad Request: file is too big",
        )
        msg = FakeMessage("名单.txt", 18 * MB)

        asyncio.run(wash_module.wash_got_file(msg, self.state, bot))

        self.assertTrue(bot.downloaded)
        shown = " ".join(msg.answers)
        self.assertIn("19MB", shown)
        self.assertIn("文件切割", shown)
        self.assertIn("18.0MB", shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
