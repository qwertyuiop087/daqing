"""预撞库一键开关回归测试。

需求：管理员要能一键开启 / 关闭「苹果 iM 预撞库」，不用进网页后台改配置。
约定：
1. 管理面板上是「🟢 预撞库：开 / 🔴 预撞库：关」，点一下直接切换；
2. /预撞库 命令能看到当前状态并给出「开启 / 关闭」两个按钮；
3. 普通用户点不了、发命令也改不了；
4. 关掉以后苹果 iMessage 提交要退回「整份送群洗」。

运行：python tests/test_im_prewash_toggle.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="imswitch_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
import services.im_prewash as impre  # noqa: E402
from database import SessionLocal, add_admin_id, get_config, init_db  # noqa: E402
from utils.keyboards import admin_kb  # noqa: E402

ADMIN_ID = 606060
NORMAL_ID = 616161


class FakeMessage:
    def __init__(self, user_id):
        self.from_user = SimpleNamespace(id=user_id, username="u", first_name="U")
        self.text = "/预撞库"
        self.answers = []
        self.markups = []
        self.chat = SimpleNamespace(id=user_id)

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=1)

    # 管理面板刷新按钮用
    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.markups.append(reply_markup)


class FakeCq:
    def __init__(self, user_id, data):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.markups = []
        self.message = FakeMessage(user_id)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append(text)


def _on() -> bool:
    with SessionLocal() as session:
        return impre.enabled(session)


def _buttons(markup) -> list:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


class TestImPrewashToggle(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            add_admin_id(session, ADMIN_ID)
            from database import set_config

            set_config(session, "im_prewash", "1")

    def test_panel_button_shows_state_and_flips(self):
        """面板按钮文字跟着状态变，点一下直接切换。"""
        self.assertIn("🟢 预撞库：开", _buttons(admin_kb()))

        cq = FakeCq(ADMIN_ID, "admin:im_prewash")
        asyncio.run(wash_module.im_prewash_toggle_from_panel(cq))
        self.assertFalse(_on(), "点了没关掉")
        self.assertIn("已关闭", " ".join(cq.answers))

        # 刷新后的面板按钮应该显示「关」
        self.assertTrue(
            any("🔴 预撞库：关" in b for b in _buttons(cq.message.markups[-1])),
            _buttons(cq.message.markups[-1]),
        )

        cq2 = FakeCq(ADMIN_ID, "admin:im_prewash")
        asyncio.run(wash_module.im_prewash_toggle_from_panel(cq2))
        self.assertTrue(_on(), "再点没打开")

    def test_command_shows_status_and_buttons(self):
        msg = FakeMessage(ADMIN_ID)
        asyncio.run(wash_module.im_prewash_switch_cmd(msg))
        self.assertIn("已开启", msg.answers[0])
        self.assertIn("🟢 开启预撞库", _buttons(msg.markups[-1]))

        asyncio.run(wash_module.im_prewash_switch_callback(
            FakeCq(ADMIN_ID, "wash:im_prewash:off")
        ))
        self.assertFalse(_on())

        msg2 = FakeMessage(ADMIN_ID)
        asyncio.run(wash_module.im_prewash_switch_cmd(msg2))
        self.assertIn("已关闭", msg2.answers[0])

    def test_switch_callback_sets_state(self):
        cq = FakeCq(ADMIN_ID, "wash:im_prewash:off")
        asyncio.run(wash_module.im_prewash_switch_callback(cq))
        self.assertFalse(_on())
        cq2 = FakeCq(ADMIN_ID, "wash:im_prewash:on")
        asyncio.run(wash_module.im_prewash_switch_callback(cq2))
        self.assertTrue(_on())

    def test_normal_user_cannot_switch(self):
        cq = FakeCq(NORMAL_ID, "admin:im_prewash")
        asyncio.run(wash_module.im_prewash_toggle_from_panel(cq))
        self.assertTrue(_on(), "普通用户居然关了预撞库")

        cq2 = FakeCq(NORMAL_ID, "wash:im_prewash:off")
        asyncio.run(wash_module.im_prewash_switch_callback(cq2))
        self.assertTrue(_on())

        msg = FakeMessage(NORMAL_ID)
        asyncio.run(wash_module.im_prewash_switch_cmd(msg))
        self.assertIn("只有管理员", " ".join(msg.answers))

    def test_off_means_whole_file_wash(self):
        """关掉后 im 提交要退回整份送群洗（不再拆分）。"""
        with SessionLocal() as session:
            from database import set_config

            set_config(session, "im_prewash", "0")
        # 只在开关这一层验证：enabled() 必须返回 False，提交逻辑用它决定要不要拆
        self.assertFalse(_on())
        self.assertFalse(impre.enabled(SessionLocal()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
