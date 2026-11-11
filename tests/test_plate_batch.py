"""车牌分包回归：字段只保留 名字/车牌/手机号，支持多文件。

运行：python tests/test_plate_batch.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="platebatch_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from handlers.plate import (  # noqa: E402
    _FIELD_KEYS,
    _ORDER_PRESETS,
    _parse_order,
    generate_outputs,
    parse_records,
)


class TestPlateFields(unittest.TestCase):
    def test_only_three_fields(self):
        """只保留 名字 / 车牌 / 手机号 三种字段。"""
        self.assertEqual(set(_FIELD_KEYS), {"名字", "车牌", "手机号"})
        for preset in _ORDER_PRESETS.values():
            self.assertEqual(set(preset), {"name", "plate", "phone"})

    def test_parse_order(self):
        keys, bad = _parse_order("名字 车牌 手机号")
        self.assertEqual(keys, ["name", "plate", "phone"])
        self.assertIsNone(bad)

    def test_parse_order_rejects_unknown(self):
        keys, bad = _parse_order("名字 地址 手机号")
        self.assertIsNone(keys)
        self.assertEqual(bad, "地址")


class TestParseRecords(unittest.TestCase):
    def test_plate_phone_name(self):
        lines = ["张三 京A12345 13800138000", "李四 沪B88888 13900139000"]
        records = parse_records(lines)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["phone"], "13800138000")
        self.assertEqual(records[0]["plate"], "京A12345")
        self.assertTrue(records[0]["name"])

    def test_skip_blank(self):
        self.assertEqual(parse_records(["", "   ", "\n"]), [])

    def test_name_only_line_kept(self):
        """只有名字的行也会保留（不丢数据）。"""
        records = parse_records(["王五"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["plate"], "")


class TestGenerateOutputs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="plateout_")

    def test_single_file_named_after_source(self):
        records = [{"name": "张三", "plate": "京A12345", "phone": "13800138000"}]
        outs = generate_outputs(records, ["name", "plate", "phone"], 0, "名单", self.dir)
        self.assertEqual(len(outs), 1)
        self.assertTrue(outs[0][1].startswith("名单_名字_车牌_手机号"))
        with open(outs[0][0], encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "张三 京A12345 13800138000")

    def test_split_by_lines(self):
        records = [{"name": f"人{i}", "plate": "", "phone": ""} for i in range(1, 6)]
        outs = generate_outputs(records, ["name"], 2, "车牌分包", self.dir)
        self.assertEqual(len(outs), 3)  # 2 + 2 + 1
        sizes = []
        for path, _name in outs:
            with open(path, encoding="utf-8") as f:
                sizes.append(len(f.read().splitlines()))
        self.assertEqual(sizes, [2, 2, 1])

    def test_field_order_respected(self):
        records = [{"name": "张三", "plate": "京A12345", "phone": "13800138000"}]
        outs = generate_outputs(records, ["phone", "plate", "name"], 0, "x", self.dir)
        with open(outs[0][0], encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "13800138000 京A12345 张三")


if __name__ == "__main__":
    unittest.main(verbosity=2)
