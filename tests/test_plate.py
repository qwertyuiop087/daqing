"""车牌识别测试：车牌正则 + 车牌/手机号/名字识别。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.file_ops import extract_name, extract_plates  # noqa: E402
from utils.phone_db import extract_phones  # noqa: E402


class TestPlate(unittest.TestCase):
    def test_standard_plate(self):
        self.assertEqual(extract_plates("京A12345"), ["京A12345"])

    def test_new_energy_plate(self):
        self.assertEqual(extract_plates("京AD12345"), ["京AD12345"])

    def test_plate_after_phone(self):
        self.assertEqual(extract_plates("13800138000 京A12345"), ["京A12345"])

    def test_plate_before_phone(self):
        self.assertEqual(extract_plates("京A12345 13800138000"), ["京A12345"])

    def test_plate_in_middle(self):
        self.assertEqual(extract_plates("张三 京A12345 13800138000"), ["京A12345"])

    def test_multiple_plates(self):
        self.assertEqual(
            extract_plates("京A12345 沪B67890"), ["京A12345", "沪B67890"]
        )

    def test_no_false_positive_from_name(self):
        self.assertEqual(extract_plates("王小明 李小红"), [])

    def test_no_false_positive_from_phone(self):
        # 纯手机号不应被识别为车牌
        self.assertEqual(extract_plates("13800138000 13900139000"), [])

    def test_pairing_phone_first(self):
        line = "13800138000\t京A12345"
        self.assertEqual(extract_phones(line), ["13800138000"])
        self.assertEqual(extract_plates(line), ["京A12345"])

    def test_pairing_plate_first(self):
        line = "京AD12345 13800138000"
        self.assertEqual(extract_phones(line), ["13800138000"])
        self.assertEqual(extract_plates(line), ["京AD12345"])

    def test_extract_name_middle(self):
        self.assertEqual(extract_name("张三 13800138000 京A12345"), "张三")

    def test_extract_name_after(self):
        self.assertEqual(extract_name("13800138000 京A12345 李四"), "李四")

    def test_extract_name_glued_before_plate(self):
        self.assertEqual(extract_name("张三京A12345"), "张三")

    def test_extract_name_glued_after_plate(self):
        self.assertEqual(extract_name("京A12345张三"), "张三")

    def test_extract_name_minority(self):
        self.assertEqual(extract_name("古丽·阿依 13800138000"), "古丽·阿依")

    def test_extract_name_none(self):
        self.assertEqual(extract_name("13800138000 京A12345"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
