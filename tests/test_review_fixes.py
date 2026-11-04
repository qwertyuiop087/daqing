"""本轮全面审查修复的回归测试（车牌、Excel 精度、套餐解析、编码回退）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.file_ops import decode_bytes, extract_plates, xlsx_to_lines  # noqa: E402
from utils.config_api import parse_plans_input, parse_wash_prices_input  # noqa: E402


class TestReviewFixes(unittest.TestCase):
    def test_plate_hk_macau_and_glued_name(self):
        self.assertEqual(extract_plates("粤Z1234港"), ["粤Z1234港"])
        self.assertEqual(extract_plates("粤ZD1234港"), ["粤ZD1234港"])
        self.assertEqual(extract_plates("京A12345张三"), ["京A12345"])
        self.assertEqual(extract_plates("张三13800138000京A12345"), ["京A12345"])
        self.assertEqual(extract_plates("京A1234警"), ["京A1234警"])
        self.assertEqual(extract_plates("京A1234567"), [])

    def test_xlsx_long_digits_raise(self):
        """16 位以上数字单元格（银行卡/身份证）宁可报错也不给错数据。"""
        import openpyxl
        from io import BytesIO
        wb = openpyxl.Workbook()
        ws = wb.active
        # 模拟 Excel 已按浮点存储的 16 位卡号（精度已丢失）
        ws.append([6.22202100012346e17])
        ws.append(["13800138000"])
        buf = BytesIO()
        wb.save(buf)
        with self.assertRaises(ValueError):
            xlsx_to_lines(buf.getvalue())

    def test_plans_parse_json_fallback(self):
        plans = parse_plans_input('[{"days": 7, "price": 1}, {"days": 30, "price": 3}]')
        self.assertEqual(plans, [{"days": 7, "price": 1.0}, {"days": 30, "price": 3.0}])
        plans2 = parse_plans_input("7:1,30:3")
        self.assertEqual(plans2, [{"days": 7, "price": 1.0}, {"days": 30, "price": 3.0}])
        prices = parse_wash_prices_input('[{"min_rows": 0, "price_per_row": 0.002}]')
        self.assertEqual(prices, [{"min_rows": 0, "price_per_row": 0.002}])

    def test_utf16_only_on_bom(self):
        """无 BOM 的 UTF-16 字节不再被当成 UTF-16 解码（避免与 Big5 等互相误判）。"""
        raw = "45".encode("utf-16-le")  # 无 BOM
        text = decode_bytes(raw)
        self.assertNotEqual(text, "45")
        # 带 BOM 的 UTF-16 仍可正常解码
        bom = b"\xff\xfe" + "45".encode("utf-16-le")
        self.assertEqual(decode_bytes(bom).lstrip("\ufeff"), "45")


if __name__ == "__main__":
    unittest.main(verbosity=2)
