"""按钮图标设置回归：新加的按钮必须能在「🎨 按钮图标设置」里选到。

线上反馈：加了「号码拆包」「预撞库开关」「清空撞库库」之后，
管理面板 → 按钮图标设置 里看不到它们，没法配图标。

原因：图标设置用的是 `handlers/membership.BTN_ICONS_LIST`（决定列表里有哪些按钮），
启动加载图标用的是 `utils/keyboards.load_btn_icons()` 里的 key 清单（决定配置读不读得到），
两处都要登记，漏一处就会出现「列表里没有」或「设了也不显示」。

运行：python tests/test_btn_icons.py
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="btnicon_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.membership as membership  # noqa: E402
import utils.keyboards as kb  # noqa: E402
from database import SessionLocal, init_db, set_config  # noqa: E402


class TestButtonIcons(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_new_buttons_are_listed_in_icon_settings(self):
        """图标设置列表里必须有新按钮，否则用户根本选不到。"""
        keys = {key for _text, key in membership.BTN_ICONS_LIST}
        for need in ("menu:cut", "admin:im_prewash", "admin:imdb_clear"):
            self.assertIn(need, keys, f"按钮图标设置里缺少 {need}")

    def test_list_has_no_duplicate_keys(self):
        keys = [key for _text, key in membership.BTN_ICONS_LIST]
        self.assertEqual(len(keys), len(set(keys)), "BTN_ICONS_LIST 有重复项")

    def test_saved_icon_is_loaded_for_new_button(self):
        """给「号码拆包」设了图标，重启后 load_btn_icons 要读得出来。"""
        with SessionLocal() as session:
            set_config(session, "btn_icon:menu:cut", "5368324170671202286")
        kb.load_btn_icons()
        self.assertEqual(
            kb._btn_icons_cache.get("menu:cut"), "5368324170671202286"
        )

    def test_menu_renders_new_button(self):
        kb.load_btn_icons()
        markup = kb.main_menu_keyboard()
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("menu:cut", datas)

    def test_clear_icon_falls_back_to_plain_button(self):
        """图标被清掉后不能再挂旧图标。"""
        with SessionLocal() as session:
            set_config(session, "btn_icon:menu:cut", "")
        kb.load_btn_icons()
        self.assertNotIn("menu:cut", kb._btn_icons_cache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
