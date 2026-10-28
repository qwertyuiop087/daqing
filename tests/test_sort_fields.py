"""定制分包（行内多字段模型）单元测试：分隔符检测 / 行内字段识别 / 调序。

运行：python tests/test_sort_fields.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.file_ops import (  # noqa: E402
    analyze_record_order,
    decode_bytes,
    detect_delimiter,
    detect_field_type,
    parse_desired_order,
    reorder_file_lines,
    reorder_record_fields,
    split_record,
)


class TestDetectFieldType(unittest.TestCase):
    def test_phone(self):
        for s in ("13800138000", "+86 138 0013 8000", "138-0013-8000", "13912345678"):
            self.assertEqual(detect_field_type(s), "手机号", s)

    def test_bank_card(self):
        for s in ("6222021000123456", "622202100012345678", "6222 0210 1234 5678 901"):
            self.assertEqual(detect_field_type(s), "银行卡", s)

    def test_id_card(self):
        for s in ("11010519491231002X", "110105194912310021", "110105491231002"):
            self.assertEqual(detect_field_type(s), "身份证", s)

    def test_time(self):
        for s in ("2024-01-01", "2024/1/5 12:30:00", "2024.01.01", "2024年1月1日", "20240101", "12:30:45"):
            self.assertEqual(detect_field_type(s), "时间", s)

    def test_amount(self):
        for s in ("100", "100.50", "¥100", "$ 1,000.00", "100元", "99.9"):
            self.assertEqual(detect_field_type(s), "金额", s)

    def test_amount_4_5_digits(self):
        # 用户场景：金额 4~5 位，比手机号/银行卡/身份证都短
        for s in ("1000", "1234", "10000", "12345", "5000.00", "1.5万", "20万"):
            self.assertEqual(detect_field_type(s), "金额", s)

    def test_name(self):
        for s in ("张三", "王", "欧阳娜娜", "张三丰", "古丽·阿依"):
            self.assertEqual(detect_field_type(s), "名字", s)

    def test_other(self):
        for s in ("hello world", "abc123", "广东省湛江市"):
            self.assertEqual(detect_field_type(s), "其他", s)


class TestDetectDelimiter(unittest.TestCase):
    def test_space(self):
        lines = [
            "13800138000 6222021000123456 100.50",
            "13900139000 6222021000123457 99元",
        ]
        self.assertEqual(detect_delimiter(lines), " ")

    def test_tab(self):
        lines = [
            "13800138000\t6222021000123456\t100.50",
            "13900139000\t6222021000123457\t99元",
        ]
        self.assertEqual(detect_delimiter(lines), "\t")

    def test_pipe(self):
        lines = [
            "13800138000|6222021000123456|100.50",
            "13900139000|6222021000123457|99元",
        ]
        self.assertEqual(detect_delimiter(lines), "|")


class TestSplitRecord(unittest.TestCase):
    def test_space(self):
        self.assertEqual(split_record("138 6222 100", " "), ["138", "6222", "100"])

    def test_tab(self):
        self.assertEqual(split_record("138\t6222\t100", "\t"), ["138", "6222", "100"])

    def test_pipe(self):
        self.assertEqual(split_record("138|6222|100", "|"), ["138", "6222", "100"])

    def test_split_name_id_glued(self):
        # 名字和身份证粘在一起：自动拆成 名字 + 身份证
        line = "刘玲500240198612311624 45000 6222803767231028850 18688537750"
        self.assertEqual(
            split_record(line, " "),
            ["刘玲", "500240198612311624", "45000", "6222803767231028850", "18688537750"],
        )

    def test_amount_unit_not_split(self):
        # “100元”“10000元”这类金额+单位不能拆
        self.assertEqual(split_record("100元 5", " "), ["100元", "5"])
        self.assertEqual(split_record("10000元", " "), ["10000元"])

    def test_pure_name_not_split(self):
        self.assertEqual(split_record("张三", " "), ["张三"])


class TestGluedNameId(unittest.TestCase):
    """用户上报场景：一行里 名字+身份证 粘连"""

    LINE = "刘玲500240198612311624 45000 6222803767231028850 18688537750"

    def test_analyze_order(self):
        order, counts, variants = analyze_record_order([self.LINE], " ")
        self.assertEqual(order, ["名字", "身份证", "金额", "银行卡", "手机号"])
        self.assertEqual(counts["名字"], 1)
        self.assertEqual(counts["身份证"], 1)
        self.assertEqual(counts["金额"], 1)
        self.assertEqual(counts["银行卡"], 1)
        self.assertEqual(counts["手机号"], 1)

    def test_reorder(self):
        out = reorder_file_lines([self.LINE], " ", ["手机号", "银行卡", "身份证", "金额", "名字"])
        self.assertEqual(
            out[0],
            "18688537750 6222803767231028850 500240198612311624 45000 刘玲",
        )

    def test_id_detected_after_split(self):
        self.assertEqual(detect_field_type("500240198612311624"), "身份证")


class TestAnalyzeOrder(unittest.TestCase):
    def test_position_majority(self):
        lines = [
            "13800138000 6222021000123456 100.50 110105194912310021 2024-01-01",
            "13900139000 6222021000123457 99元 110105194912310022 2024-02-02",
        ]
        order, counts, variants = analyze_record_order(lines, " ")
        self.assertEqual(order, ["手机号", "银行卡", "金额", "身份证", "时间"])
        self.assertEqual(counts["手机号"], 2)
        self.assertEqual(counts["金额"], 2)
        self.assertEqual(variants, [["手机号", "银行卡", "金额", "身份证", "时间"]])

    def test_pipe_lines(self):
        lines = [
            "13800138000|6222021000123456|100.50",
            "13900139000|6222021000123457|99元",
        ]
        order, counts, variants = analyze_record_order(lines, "|")
        self.assertEqual(order, ["手机号", "银行卡", "金额"])
        self.assertEqual(variants, [["手机号", "银行卡", "金额"]])


class TestMixedLineOrders(unittest.TestCase):
    """用户场景：同一批文件里不同行字段顺序不一致，也要逐行识别并调序"""

    def test_reorder_mixed_orders(self):
        lines = [
            "13800138000 张三 6222021000123456 500240198612311624 100.50",
            "李四 13900139000 6222021000123457 500240198612311625 200",
        ]
        out = reorder_file_lines(lines, " ", ["手机号", "名字", "银行卡", "身份证", "金额"])
        self.assertEqual(out[0], "13800138000 张三 6222021000123456 500240198612311624 100.50")
        self.assertEqual(out[1], "13900139000 李四 6222021000123457 500240198612311625 200")

    def test_variants_detected(self):
        lines = [
            "13800138000 张三 6222021000123456",
            "李四 13900139000 6222021000123457",
        ]
        order, counts, variants = analyze_record_order(lines, " ")
        self.assertEqual(
            variants,
            [["手机号", "名字", "银行卡"], ["名字", "手机号", "银行卡"]],
        )
        self.assertEqual(counts["手机号"], 2)
        self.assertEqual(counts["名字"], 2)

    def test_reorder_desired_subset(self):
        # 用户只要求部分字段顺序：其余已知字段按原顺序跟在后面
        lines = [
            "13800138000 张三 6222021000123456 100.50",
            "李四 13900139000 6222021000123457 200",
        ]
        out = reorder_file_lines(lines, " ", ["金额", "手机号"])
        self.assertEqual(out[0], "100.50 13800138000 张三 6222021000123456")
        self.assertEqual(out[1], "200 13900139000 李四 6222021000123457")


class TestReorderFields(unittest.TestCase):
    def test_reorder_in_line(self):
        tokens = ["13800138000", "6222021000123456", "100.50", "110105194912310021", "2024-01-01"]
        out = reorder_record_fields(tokens, ["银行卡", "手机号", "身份证", "金额", "时间"])
        self.assertEqual(
            out,
            ["6222021000123456", "13800138000", "110105194912310021", "100.50", "2024-01-01"],
        )

    def test_unlisted_known_appended(self):
        # 用户只指定 银行卡：其余已知字段按原顺序跟在后面
        tokens = ["13800138000", "6222021000123456", "100.50"]
        out = reorder_record_fields(tokens, ["银行卡"])
        self.assertEqual(out, ["6222021000123456", "13800138000", "100.50"])

    def test_unknown_at_end(self):
        tokens = ["13800138000", "note", "100.50", "这是一段长备注"]
        out = reorder_record_fields(tokens, ["金额", "手机号"])
        self.assertEqual(out, ["100.50", "13800138000", "note", "这是一段长备注"])

    def test_name_field_reorder(self):
        tokens = ["张三", "13800138000", "100.50"]
        out = reorder_record_fields(tokens, ["手机号", "金额", "名字"])
        self.assertEqual(out, ["13800138000", "100.50", "张三"])

    def test_same_type_multiple_keeps_order(self):
        tokens = ["13800138000", "13900139000", "100.50"]
        out = reorder_record_fields(tokens, ["金额", "手机号"])
        self.assertEqual(out, ["100.50", "13800138000", "13900139000"])


class TestReorderFile(unittest.TestCase):
    def test_file_reorder(self):
        lines = [
            "13800138000 6222021000123456 100.50 110105194912310021 2024-01-01",
            "13900139000 6222021000123457 99元 110105194912310022 2024-02-02",
        ]
        out = reorder_file_lines(lines, " ", ["银行卡", "手机号", "身份证", "金额", "时间"])
        self.assertEqual(
            out[0],
            "6222021000123456 13800138000 110105194912310021 100.50 2024-01-01",
        )
        self.assertEqual(
            out[1],
            "6222021000123457 13900139000 110105194912310022 99元 2024-02-02",
        )

    def test_pipe_file_reorder(self):
        lines = ["13800138000|100.50|6222021000123456", "13900139000|99元|6222021000123457"]
        out = reorder_file_lines(lines, "|", ["银行卡", "手机号", "金额"])
        self.assertEqual(out[0], "6222021000123456|13800138000|100.50")
        self.assertEqual(out[1], "6222021000123457|13900139000|99元")


class TestSixDigitAfterId(unittest.TestCase):
    """“身份证后6位”特性已删除：紧跟身份证的 6 位数字一律按普通字段（金额）处理"""

    LINE = "周以库 310108197909130810 357726 12100"

    def test_analyze(self):
        order, counts, variants = analyze_record_order([self.LINE], " ")
        self.assertEqual(order, ["名字", "身份证", "金额"])
        self.assertEqual(counts["金额"], 2)

    def test_reorder(self):
        out = reorder_file_lines([self.LINE], " ", ["手机号", "银行卡", "身份证", "金额", "名字"])
        self.assertEqual(out[0], "310108197909130810 357726 12100 周以库")

    def test_amount_first(self):
        out = reorder_file_lines([self.LINE], " ", ["手机号", "金额", "身份证", "名字"])
        self.assertEqual(out[0], "357726 12100 310108197909130810 周以库")

    def test_id_tail_alias_rejected(self):
        # 字段已删除：用户再输入 后6位/尾号 应提示无法识别
        result, bad = parse_desired_order("名字 后6位 身份证")
        self.assertIsNone(result)
        self.assertEqual(bad, "后6位")

    def test_6digit_without_id_stays_amount(self):
        # 行内没有身份证时，6 位纯数字仍是金额
        order, counts, variants = analyze_record_order(["500000 12100"], " ")
        self.assertEqual(counts["金额"], 2)

    def test_two_line_record_variants(self):
        # 名字+身份证+6位码+金额 一行，手机号单独一行
        lines = [self.LINE, "17702103548"]
        order, counts, variants = analyze_record_order(lines, " ")
        self.assertEqual(len(variants), 2)
        self.assertEqual(counts["手机号"], 1)


class TestSixDigitAmountAfterId(unittest.TestCase):
    """用户上报场景：6 位数字紧跟在身份证后面，但它是金额（如 150000）"""

    LINE = "李高攀 412326198906050018 150000 13333837978 6222031702011714167"

    def test_amount_not_glued(self):
        # 紧跟身份证的 6 位数字就是金额，不会粘到身份证上
        order, counts, variants = analyze_record_order([self.LINE], " ")
        self.assertIn("金额", order)
        self.assertEqual(counts["金额"], 1)

    def test_reorder_moves_amount(self):
        out = reorder_file_lines(
            [self.LINE], " ", ["名字", "身份证", "手机号", "银行卡", "金额"]
        )
        self.assertEqual(out[0], "李高攀 412326198906050018 13333837978 6222031702011714167 150000")

    def test_6digit_after_id_is_amount(self):
        # 34.txt 结构（名字 身份证 6位码 金额）：6位码现在按金额处理
        line = "周以库 310108197909130810 357726 12100"
        out = reorder_file_lines(line.split("\n"), " ", ["手机号", "银行卡", "身份证", "金额", "名字"])
        self.assertEqual(out[0], "310108197909130810 357726 12100 周以库")


class TestBankCardRange(unittest.TestCase):
    """银行卡位数范围 16~21 位"""

    def test_20_digit_bank(self):
        self.assertEqual(detect_field_type("62175875000006519023"), "银行卡")

    def test_21_digit_bank(self):
        self.assertEqual(detect_field_type("621758750000065190231"), "银行卡")

    def test_19_digit_id_like_is_bank(self):
        # 19 位号码（非合法身份证）→ 银行卡
        self.assertEqual(detect_field_type("4301181981002241466"), "银行卡")


class TestBom(unittest.TestCase):
    """文件开头带 UTF-8 BOM 时，首行第一个字段不能被污染"""

    def test_decode_strips_bom(self):
        # UTF-8 BOM（\xef\xbb\xbf）+ “廖林培 450421199010193538 1400”
        raw = b"\xef\xbb\xbf\xe5\xbb\x96\xe6\x9e\x97\xe5\x9f\xb9 450421199010193538 1400"
        self.assertEqual(decode_bytes(raw), "廖林培 450421199010193538 1400")

    def test_first_line_bom_name_not_moved(self):
        # 用户场景：带 BOM 的首行，名字必须识别为“名字”，不能跑到行尾
        line = "\ufeff廖林培 450421199010193538 1400 6217003400002221946 18978486083"
        out = reorder_file_lines(line.split("\n"), " ", ["名字", "身份证", "金额", "银行卡", "手机号"])
        self.assertEqual(
            out[0],
            "廖林培 450421199010193538 1400 6217003400002221946 18978486083",
        )

    def test_zero_width_char_in_token_cleaned(self):
        # 零宽字符出现在字段中间也要清理
        self.assertEqual(split_record("廖\u200b林培 123", " "), ["廖林培", "123"])


class TestParseOrder(unittest.TestCase):
    def test_parse_ok(self):
        result, bad = parse_desired_order("手机号 银行卡 身份证 金额 时间")
        self.assertIsNone(bad)
        self.assertEqual(result, ["手机号", "银行卡", "身份证", "金额", "时间"])

    def test_parse_aliases(self):
        result, _ = parse_desired_order("手机号、卡号/证件，日期")
        self.assertEqual(result, ["手机号", "银行卡", "身份证", "时间"])

    def test_parse_bad(self):
        result, bad = parse_desired_order("手机号 火星")
        self.assertIsNone(result)
        self.assertEqual(bad, "火星")


class TestRealDataRegression(unittest.TestCase):
    """用户真实文件（小鸡_08 / 小鸡9_01）上报的错行回归。"""

    def test_name_with_road_building_chars(self):
        # 人名里含 路/楼/栋 等字（也是地址用字），短中文串必须识别为名字，不能被地址关键词误伤
        for name in ("路炜", "韩国栋", "王华楼", "焦远栋", "李青路", "吕路婷", "李栋"):
            self.assertEqual(detect_field_type(name), "名字", name)

    def test_ethnic_name_with_dot(self):
        # 含·的少数民族名，即使含"路"字也必须是名字
        for name in ("尼路帕·热合木", "赛乃木·艾尔西丁", "古丽·阿依"):
            self.assertEqual(detect_field_type(name), "名字", name)

    def test_six_digit_amount_with_real_bank(self):
        # 行内已有 16 位完整银行卡时，6 位金额（199500 等）不得升级成银行卡
        line = "杨明冉 230231199706033322 6214680094091103 199500 13911309295"
        out = reorder_file_lines(
            line.split("\n"), " ",
            ["名字", "身份证", "金额", "银行卡", "手机号"],
            treat_6digit_as_bank=True,
        )
        self.assertEqual(out[0], "杨明冉 230231199706033322 199500 6214680094091103 13911309295")

    def test_future_birthday_18digits_is_bank(self):
        # 18 位银联卡 622136203010137980 的日期段是 2030-10-13（未来），不能判为身份证
        self.assertEqual(detect_field_type("622136203010137980"), "银行卡")
        # 同一张卡与真实身份证同行时，身份证仍应识别
        line = "李进 610113198711051638 622136203010137980 50000 15809291623"
        out = reorder_file_lines(
            line.split("\n"), " ",
            ["名字", "身份证", "金额", "银行卡", "手机号"],
            treat_6digit_as_bank=True,
        )
        self.assertEqual(out[0], "李进 610113198711051638 50000 622136203010137980 15809291623")

    def test_reported_rows_end_to_end(self):
        # 用户点名的 8 行，重排后名字必须回到第一列
        rows = [
            "152601198108180112\t11500\t6214850114024265\t13401118667\t路炜",
            "610113199210021611\t10000\t6214830299210277\t17391748955\t韩国栋",
            "362331199906223610\t28000\t6214855924687581\t17605052819\t王华楼",
            "652927199702070293\t23000\t6217921972977063\t17629108197\t焦远栋",
            "371522199004072118\t10000\t6215211507951920\t13671218896\t李青路",
            "654101199310281168\t705000\t6217661199900378674\t18109993431\t尼路帕·热合木",
            "140522199404080041\t25000\t6214833563245938\t18834356566\t吕路婷",
            "610526199505017912\t10500\t6217004220033094453\t18395418827\t李栋",
        ]
        out = reorder_file_lines(
            rows, "\t",
            ["名字", "身份证", "金额", "银行卡", "手机号"],
            treat_6digit_as_bank=True,
        )
        for src, res in zip(rows, out):
            name = src.split("\t")[-1]
            self.assertTrue(res.startswith(name + "\t"), f"{name} 未排到第一列: {res}")
            self.assertEqual(len(res.split("\t")), 5, res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
