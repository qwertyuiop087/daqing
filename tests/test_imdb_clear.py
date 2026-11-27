"""清空撞库号码库回归测试。

需求：管理员要能把本地号码库（苹果 iMessage 撞库用的那张表）一键清空。
约定：
1. 只有管理员能清，普通用户点了没反应也不能删数据；
2. 命令只显示「确认页」，真正删除必须再点一次确认按钮；
3. 清空后预撞库要自动跳过（不然整批都会被判成安卓免费放走）。

运行：python tests/test_imdb_clear.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="imdbclear_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
import services.im_prewash as impre  # noqa: E402
from database import (  # noqa: E402
    SessionLocal,
    add_admin_id,
    bulk_add_phone_profiles,
    count_phone_profiles,
    init_db,
)

ADMIN_ID = 424242
NORMAL_ID = 434343


class FakeMessage:
    def __init__(self, user_id, text="/清空库"):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, username="u", first_name="U")
        self.answers = []
        self.edits = []
        self.markups = []
        self.chat = SimpleNamespace(id=user_id)

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=1)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append(text)
        self.markups.append(reply_markup)


def _buttons(markup) -> list:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


class FakeState:
    async def get_state(self):
        return None

    async def clear(self):
        return None


class FakeCq:
    def __init__(self, user_id, data="wash:imdb_clear:yes"):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.message = FakeMessage(user_id)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append(text)


def _seed():
    with SessionLocal() as session:
        bulk_add_phone_profiles(
            session,
            [("13800000001", "苹果"), ("13800000002", "苹果"), ("13900000003", "安卓")],
        )


def _total() -> int:
    with SessionLocal() as session:
        return count_phone_profiles(session)["total"]


class TestImdbClear(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            add_admin_id(session, ADMIN_ID)
        _seed()

    def test_counts_split_apple_and_android(self):
        with SessionLocal() as session:
            stat = count_phone_profiles(session)
        self.assertEqual(stat, {"total": 3, "apple": 2, "android": 1})

    def test_command_shows_confirm_and_does_not_delete(self):
        """命令只出确认页，数据必须还在。"""
        msg = FakeMessage(ADMIN_ID)
        asyncio.run(wash_module.imdb_clear_cmd(msg, FakeState()))
        self.assertEqual(_total(), 3, "没点确认就删了数据")
        shown = " ".join(msg.answers)
        self.assertIn("当前库内", shown)
        self.assertIn("删除不可恢复", shown)
        self.assertTrue(
            any("确认清空 3 条" in t for t in _buttons(msg.markups[-1])),
            _buttons(msg.markups[-1]),
        )

    def test_confirm_deletes_everything(self):
        cq = FakeCq(ADMIN_ID)
        asyncio.run(wash_module.imdb_clear_confirm(cq))
        self.assertEqual(_total(), 0)
        self.assertIn("已清空 3 条", " ".join(cq.answers))
        self.assertIn("已清空", " ".join(cq.message.edits))

    def test_normal_user_cannot_clear(self):
        cq = FakeCq(NORMAL_ID)
        asyncio.run(wash_module.imdb_clear_confirm(cq))
        self.assertEqual(_total(), 3, "普通用户居然清掉了库")

        msg = FakeMessage(NORMAL_ID)
        asyncio.run(wash_module.imdb_clear_cmd(msg, FakeState()))
        self.assertEqual(_total(), 3)
        self.assertIn("只有管理员", " ".join(msg.answers))

    def test_clear_twice_is_harmless(self):
        asyncio.run(wash_module.imdb_clear_confirm(FakeCq(ADMIN_ID)))
        asyncio.run(wash_module.imdb_clear_confirm(FakeCq(ADMIN_ID)))
        self.assertEqual(_total(), 0)

    def test_panel_button_shows_same_page(self):
        cq = FakeCq(ADMIN_ID, data="admin:imdb_clear")
        asyncio.run(wash_module.imdb_clear_from_panel(cq))
        self.assertEqual(_total(), 3)
        shown = " ".join(cq.message.edits + cq.message.answers)
        self.assertIn("当前库内", shown)
        self.assertTrue(
            any("确认清空 3 条" in t for t in _buttons(cq.message.markups[-1])),
            _buttons(cq.message.markups[-1]),
        )

    def test_empty_library_says_nothing_to_clear(self):
        """库已经空了：提示一句，不要把面板消息改成提示、也不报错。"""
        asyncio.run(wash_module.imdb_clear_confirm(FakeCq(ADMIN_ID)))
        cq = FakeCq(ADMIN_ID, data="admin:imdb_clear")
        asyncio.run(wash_module.imdb_clear_from_panel(cq))
        self.assertEqual(cq.message.edits, [])
        self.assertIn("空的", " ".join(cq.answers))

        msg = FakeMessage(ADMIN_ID)
        asyncio.run(wash_module.imdb_clear_cmd(msg, FakeState()))
        self.assertIn("空的", " ".join(msg.answers))

    def test_after_clear_prewash_has_no_apple_library(self):
        """清空后预撞库必须自动跳过（否则整批会被判成安卓免费放走）。"""
        asyncio.run(wash_module.imdb_clear_confirm(FakeCq(ADMIN_ID)))
        with SessionLocal() as session:
            self.assertEqual(impre.library_size(session), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
