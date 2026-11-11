"""解压提取 TXT / 提取手机号：多文件回归。

运行：python tests/test_multi_upload.py
"""
import io as _io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="multiup_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from handlers.extract import extract_phone_numbers, read_text_by_suffix  # noqa: E402
from handlers.zip_extract import _unique_name  # noqa: E402
from services.file_ops import extract_txt_from_zip  # noqa: E402


class TestUniqueName(unittest.TestCase):
    def test_duplicate_renamed(self):
        used = set()
        self.assertEqual(_unique_name("名单.txt", used), "名单.txt")
        self.assertEqual(_unique_name("名单.txt", used), "名单_2.txt")
        self.assertEqual(_unique_name("名单.txt", used), "名单_3.txt")

    def test_different_names_kept(self):
        used = set()
        self.assertEqual(_unique_name("a.txt", used), "a.txt")
        self.assertEqual(_unique_name("b.txt", used), "b.txt")


class TestZipMulti(unittest.TestCase):
    def test_two_zips_same_inner_name_do_not_collide(self):
        """两个压缩包里都有 名单.txt，提取后要用不同的输出名。"""
        def make_zip():
            buf = _io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("名单.txt", "13800138000\n")
            return buf.getvalue()

        used = set()
        for _ in range(2):
            items = extract_txt_from_zip(make_zip(), max_txt=10, max_size=1024 * 1024, max_total_size=4 * 1024 * 1024)
            self.assertEqual(items[0][0], "名单.txt")
            name = _unique_name(items[0][0], used)
            self.assertNotIn(name, {"名单.txt"}) if name != "名单.txt" else None
        self.assertEqual(len(used), 2)


class TestExtractPhones(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(extract_phone_numbers("13800138000"), ["13800138000"])

    def test_with_prefix_and_dashes(self):
        self.assertEqual(
            extract_phone_numbers("+86 138-0013-8000"), ["13800138000"]
        )

    def test_multiple_in_text(self):
        got = extract_phone_numbers("张三 13800138000 李四 13900139000")
        self.assertEqual(got, ["13800138000", "13900139000"])

    def test_ignores_short_and_id(self):
        self.assertEqual(extract_phone_numbers("1234567890 110101199003071234"), [])

    def test_read_text_by_suffix_txt(self):
        path = os.path.join(_TMP, "a.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("13800138000\n")
        self.assertIn("13800138000", read_text_by_suffix(path, "a.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
