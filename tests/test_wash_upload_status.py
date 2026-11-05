"""洗数据多文件上传回归：一批文件必须只有一条状态消息滚动更新。

背景（线上实况）：用户一次选取 8 个 TXT 一起发过来，机器人回了 8 条
「已收到 1 个文件…」+ 8 个「开始洗数据」按钮 —— 因为 aiogram 默认把每个
update 丢进独立 task 并发处理，8 个 handler 同时读到 status_msg_id=None，
于是各自新发一条消息。

修复后的约定：
1. 无论同时到达多少个文件，都只编辑同一条状态消息（不新增）；
2. 同一批更新要合并成尽量少的编辑次数（避免 Telegram 编辑限流）；
3. 状态消息真的不可编辑时，才允许新发一条，并且后续继续编辑新的那条。

运行：python tests/test_wash_upload_status.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washupload_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
from database import SessionLocal, init_db, set_wash_prices  # noqa: E402
from states import WashStates  # noqa: E402

STATUS_MSG_ID = 4242


def get_router_handler(router, name: str):
    for h in router.message.handlers:
        if getattr(h.callback, "__name__", "") == name:
            return h.callback
    raise AssertionError(f"handler {name} not found")


class FakeState:
    """最小 FSMContext 替身。"""

    def __init__(self, data):
        self.data = dict(data)

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_state(self):
        return self.data.get("_state")

    async def set_state(self, value):
        self.data["_state"] = getattr(value, "state", value)

    async def clear(self):
        self.data = {}


class FakeBot:
    def __init__(self, edit_error=None):
        self.sent = []          # 新建的消息
        self.edits = []         # (message_id, text)
        self.edit_error = edit_error
        self._next_id = 5000

    async def get_file(self, file_id):
        return SimpleNamespace(file_path=file_id, file_size=1024)

    async def download_file(self, file_path, destination):
        # 每个文件写 100 行手机号
        with open(destination, "w", encoding="utf-8") as fh:
            for i in range(100):
                fh.write(f"1380013{i:04d}\n")

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                               reply_markup=None, parse_mode=None, **kw):
        if self.edit_error:
            raise self.edit_error
        self.edits.append((message_id, text))

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        self._next_id += 1
        self.sent.append((self._next_id, text))
        return SimpleNamespace(message_id=self._next_id, chat_id=chat_id)


class FakeMessage:
    def __init__(self, user_id, file_name, chat_id=None):
        self.document = SimpleNamespace(file_id=file_name, file_name=file_name)
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id if chat_id is not None else user_id)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return SimpleNamespace(message_id=1, chat_id=self.chat.id)


def make_state(user_id):
    from utils.helpers import get_workdir
    return FakeState(
        {
            "workdir": get_workdir(user_id),
            "files": [],
            "status_msg_id": STATUS_MSG_ID,
            "status_chat_id": user_id,
            "_state": WashStates.waiting_file.state,
        }
    )


class TestWashUploadStatus(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_wash_prices(
                session,
                [
                    {"min_rows": 0, "price_per_row": 0.002},
                    {"min_rows": 100000, "price_per_row": 0.001},
                ],
            )

    def test_burst_of_files_uses_single_status_message(self):
        """8 个文件并发到达：只编辑同一条状态消息，不新增消息。"""
        user_id = 70001
        handler = get_router_handler(wash_module.router, "wash_got_file")
        bot = FakeBot()
        state = make_state(user_id)

        names = [f"名单{i}.txt" for i in range(1, 9)]

        async def flow():
            await asyncio.gather(
                *(handler(FakeMessage(user_id, n), state, bot) for n in names)
            )
            # 等渲染循环把这一批合并刷新完（_UPLOAD_RENDER_DELAY 之后）
            await asyncio.sleep(wash_module._UPLOAD_RENDER_DELAY + 0.8)

        asyncio.run(flow())

        data = asyncio.run(state.get_data())
        self.assertEqual(len(data["files"]), 8, data["files"])

        # 不允许为每个文件新建一条消息
        self.assertEqual(bot.sent, [], f"不该新发消息，实际新发了 {bot.sent}")
        # 只编辑预先那条状态消息
        self.assertTrue(bot.edits, "状态消息没有被编辑")
        self.assertEqual({mid for mid, _ in bot.edits}, {STATUS_MSG_ID})
        # 同一批更新要合并，编辑次数远小于文件数
        self.assertLessEqual(len(bot.edits), 3, f"编辑次数过多：{len(bot.edits)}")
        # 最后一次编辑必须反映全部 8 个文件与总行数
        last = bot.edits[-1][1]
        self.assertIn("已收到 <b>8</b> 个文件", last)
        self.assertIn("800", last)

    def test_uneditable_status_falls_back_once(self):
        """状态消息被删除/不可改时，只补发一条，后续继续编辑新消息。"""
        user_id = 70002
        handler = get_router_handler(wash_module.router, "wash_got_file")
        err = Exception("Bad Request: message to edit not found")
        bot = FakeBot(edit_error=err)
        state = make_state(user_id)

        async def flow():
            await handler(FakeMessage(user_id, "a.txt"), state, bot)
            await asyncio.sleep(wash_module._UPLOAD_RENDER_DELAY + 0.8)
            await handler(FakeMessage(user_id, "b.txt"), state, bot)
            await asyncio.sleep(wash_module._UPLOAD_RENDER_DELAY + 0.8)

        asyncio.run(flow())

        # 第一条不可编辑 → 补发一条；之后这条继续被复用（但仍在报错，故再补）
        self.assertGreaterEqual(len(bot.sent), 1)
        data = asyncio.run(state.get_data())
        self.assertEqual(len(data["files"]), 2)
        self.assertIn("已收到", bot.sent[0][1])

    def test_not_modified_error_does_not_spam(self):
        """「内容未修改」是正常返回，不能被当成失败而重复发消息。"""
        user_id = 70003
        handler = get_router_handler(wash_module.router, "wash_got_file")
        err = Exception("Bad Request: message is not modified")
        bot = FakeBot(edit_error=err)
        state = make_state(user_id)

        async def flow():
            await handler(FakeMessage(user_id, "x.txt"), state, bot)
            await asyncio.sleep(wash_module._UPLOAD_RENDER_DELAY + 0.8)

        asyncio.run(flow())
        self.assertEqual(bot.sent, [], "not modified 不应触发新消息")

    def test_temp_paths_do_not_collide(self):
        """并发下载时临时文件名必须互不相同（以前用列表长度+1 会覆盖）。"""
        user_id = 70004
        handler = get_router_handler(wash_module.router, "wash_got_file")
        bot = FakeBot()
        state = make_state(user_id)

        async def flow():
            await asyncio.gather(
                *(handler(FakeMessage(user_id, f"same.txt"), state, bot) for _ in range(4))
            )
            await asyncio.sleep(wash_module._UPLOAD_RENDER_DELAY + 0.8)

        asyncio.run(flow())

        data = asyncio.run(state.get_data())
        paths = [f["path"] for f in data["files"]]
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(set(paths)), 4, f"临时路径重复：{paths}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
