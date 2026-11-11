"""加区号回归：多文件批量收集 + 逐个加 +86。

运行：python tests/test_add_area_code.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="addcode_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from handlers.add_area_code import _out_name, add_plus86  # noqa: E402


class TestAddPlus86(unittest.TestCase):
    def test_simple_phone(self):
        text, changed = add_plus86("13800138000\n13900139000")
        self.assertEqual(text, "+8613800138000\n+8613900139000")
        self.assertEqual(changed, 2)

    def test_already_prefixed_not_duplicated(self):
        for raw in ("+8613800138000", "8613800138000", "86 13800138000", "+86 13800138000"):
            text, changed = add_plus86(raw)
            self.assertEqual(changed, 0, raw)
            self.assertNotIn("+86+86", text, raw)

    def test_multiple_phones_in_one_line(self):
        text, changed = add_plus86("张三 13800138000 13900139000")
        self.assertEqual(changed, 1)
        self.assertIn("+8613800138000", text)
        self.assertIn("+8613900139000", text)

    def test_blank_lines_dropped(self):
        text, changed = add_plus86("13800138000\n\n\n13900139000\n")
        self.assertEqual(len(text.splitlines()), 2)
        self.assertEqual(changed, 2)

    def test_other_text_kept(self):
        text, changed = add_plus86("备注：测试 13800138000")
        self.assertIn("备注：测试", text)
        self.assertEqual(changed, 1)

    def test_id_card_not_touched(self):
        """18 位身份证里的数字不应被当成手机号加区号。"""
        text, changed = add_plus86("110101199003071234")
        self.assertEqual(changed, 0)
        self.assertEqual(text, "110101199003071234")


class TestOutName(unittest.TestCase):
    def test_basic(self):
        used = set()
        self.assertEqual(_out_name("名单.txt", used), "名单_加区号.txt")
        self.assertEqual(_out_name("名单.csv", used), "名单_加区号_2.txt")
        self.assertEqual(_out_name("名单.txt", used), "名单_加区号_3.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
