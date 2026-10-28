"""services.file_ops 单元测试（仅标准库，可直接运行：python tests/test_file_ops.py）。"""
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.file_ops import (  # noqa: E402
    decode_bytes,
    dedup_numbers,
    extract_txt_from_zip,
    insert_phones_per_file,
    insert_phones_randomly,
    merge_texts,
    numbers_to_vcf,
    parse_numbers,
    sanitize_filename,
    split_lines,
)


class TestSplitLines(unittest.TestCase):
    def test_split_even(self):
        content = "\n".join(f"line{i}" for i in range(100))
        parts = split_lines(content, 30)
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0].splitlines()[0], "line0")
        self.assertEqual(len(parts[3].splitlines()), 10)

    def test_split_invalid(self):
        with self.assertRaises(ValueError):
            split_lines("abc", 0)

    def test_split_empty(self):
        self.assertEqual(split_lines("", 10), [])


class TestInsertPhones(unittest.TestCase):
    def test_insert_count_and_report(self):
        files = ["a\nb\nc", "d\ne\nf"]
        phones = ["13800000001", "13800000002", "13800000003"]
        new_files, report = insert_phones_randomly(files, phones)
        self.assertEqual(len(report), 3)
        self.assertEqual(len(new_files), 2)
        # 每个号码都应出现在某个文件中
        all_text = "\n".join(new_files)
        for p in phones:
            self.assertIn(p, all_text)
        # 报告行格式：文件N 第X行 插入: 号码
        for line in report:
            self.assertRegex(line, r"^文件\d+ 第\d+行 插入: \d+$")
            _, _, phone = line.rpartition("插入: ")
            self.assertIn(phone, all_text)

    def test_per_file_insert(self):
        """每份文件插入指定数量手机号：随机抽号码，不是随机选文件。"""
        files = ["a\nb\nc", "d\ne\nf", "g\nh\ni"]
        phones = [f"13{i:09d}" for i in range(1, 10)]  # 9 个号码
        new_files, report = insert_phones_per_file(files, phones, 3)
        self.assertEqual(len(new_files), 3)
        self.assertEqual(len(report), 9)
        # 每份文件都多 3 行（每份固定 3 个号码）
        for f in new_files:
            self.assertEqual(len(f.splitlines()), 6)
        # 9 个号码全部用上、无重复
        all_text = "\n".join(new_files)
        for p in phones:
            self.assertIn(p, all_text)
        # 报告每行格式正确
        for line in report:
            self.assertRegex(line, r"^文件\d+ 第\d+行 插入: \d+$")

    def test_per_file_insufficient_reuse(self):
        """号码数量不足时自动循环复用号码池，每份文件仍然固定插入指定数量。"""
        files = ["a\nb", "c\nd"]
        phones = ["13800000001", "13800000002"]  # 2 个号码，但需要 2 份 × 3 = 6 个
        new_files, report = insert_phones_per_file(files, phones, 3)
        self.assertIsNotNone(new_files)
        self.assertEqual(len(report), 6)
        # 每份文件固定插入 3 个号码
        self.assertEqual(len(new_files[0].splitlines()), 5)
        self.assertEqual(len(new_files[1].splitlines()), 5)
        # 插入的号码全部来自原号码池（循环复用）
        for line in report:
            _, _, phone = line.rpartition("插入: ")
            self.assertIn(phone, phones)

    def test_per_file_empty_phones(self):
        """号码池为空时返回 None。"""
        result = insert_phones_per_file(["a\nb"], [], 3)
        self.assertIsNone(result)


class TestExtractZip(unittest.TestCase):
    def _make_zip(self, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return buf.getvalue()

    def test_extract_txt_only(self):
        raw = self._make_zip([
            ("a.txt", "hello"),
            ("sub/b.txt", "world"),
            ("readme.md", "ignore"),
            ("c.TXT", "upper"),
        ])
        out = extract_txt_from_zip(raw)
        names = [n for n, _ in out]
        self.assertEqual(sorted(names), ["a.txt", "b.txt", "c.TXT"])
        texts = {n: t for n, t in out}
        self.assertEqual(texts["a.txt"], "hello")
        self.assertEqual(texts["b.txt"], "world")

    def test_zip_slip_protected(self):
        raw = self._make_zip([
            ("../evil.txt", "bad"),
            ("good.txt", "ok"),
        ])
        out = extract_txt_from_zip(raw)
        names = [n for n, _ in out]
        self.assertNotIn("evil.txt", names)
        self.assertIn("good.txt", names)

    def test_duplicate_names(self):
        raw = self._make_zip([("x.txt", "1"), ("x.txt", "2")])
        out = extract_txt_from_zip(raw)
        names = [n for n, _ in out]
        self.assertIn("x.txt", names)
        self.assertIn("x(1).txt", names)

    def test_bad_zip(self):
        with self.assertRaises(zipfile.BadZipFile):
            extract_txt_from_zip(b"not a zip")

    def test_single_file_size_cap(self):
        raw = self._make_zip([("big.txt", "X" * 100), ("small.txt", "ok")])
        out = extract_txt_from_zip(raw, max_size=50)
        names = [n for n, _ in out]
        self.assertNotIn("big.txt", names)
        self.assertIn("small.txt", names)

    def test_zip_total_size_cap(self):
        # 累计解压字节超过 max_total_size 时提前停止
        raw = self._make_zip([
            ("a.txt", "A" * 100),
            ("b.txt", "B" * 100),
            ("c.txt", "C" * 100),
        ])
        out = extract_txt_from_zip(raw, max_total_size=250)
        self.assertEqual(len(out), 2)


class TestDedup(unittest.TestCase):
    def test_dedup_by_digits(self):
        lines = [
            "13800138000",
            "138-0013-8000",
            "+86 138 0013 8000",
            "13900139000",
            "",
            "hello world",
        ]
        unique, removed = dedup_numbers(lines)
        self.assertEqual(removed, 2)
        self.assertEqual(len(unique), 2)
        self.assertEqual(unique[0], "13800138000")
        self.assertEqual(unique[1], "13900139000")


class TestMerge(unittest.TestCase):
    def test_merge_with_headers(self):
        merged = merge_texts([("a.txt", "1\n2"), ("b.txt", "3")], with_headers=True)
        self.assertIn("===== a.txt =====", merged)
        self.assertIn("===== b.txt =====", merged)
        self.assertIn("1\n2", merged)

    def test_merge_plain(self):
        merged = merge_texts([("a.txt", "1"), ("b.txt", "2")], with_headers=False)
        self.assertNotIn("=====", merged)
        self.assertIn("1", merged)
        self.assertIn("2", merged)


class TestVcf(unittest.TestCase):
    def test_numbers_to_vcf(self):
        vcf = numbers_to_vcf(["13800138000", "13900139000"])
        self.assertEqual(vcf.count("BEGIN:VCARD"), 2)
        self.assertEqual(vcf.count("END:VCARD"), 2)
        self.assertIn("TEL;TYPE=CELL:13800138000", vcf)
        self.assertIn("FN:联系人1", vcf)


class TestMisc(unittest.TestCase):
    def test_decode_gbk(self):
        data = "中文内容".encode("gbk")
        self.assertEqual(decode_bytes(data), "中文内容")

    def test_parse_numbers(self):
        self.assertEqual(parse_numbers("138\n\n139\n"), ["138", "139"])

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("a/b/c.txt"), "c.txt")
        self.assertEqual(sanitize_filename("a\\b\\c.txt"), "c.txt")
        self.assertEqual(sanitize_filename(""), "file.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
