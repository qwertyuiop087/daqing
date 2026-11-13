"""洗数据提交链路回归：随机码生成不能因为缺 import 直接崩。

线上报错：提交失败：name 'secrets' is not defined
原因：随机码改成 secrets 后，模块顶部没导入，函数里只有局部 import random/string。

运行：python tests/test_wash_submit_code.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="washcode_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash  # noqa: E402


class TestWashCode(unittest.TestCase):
    def test_gen_code_works(self):
        """直接调用生成函数，缺 import 会立刻 NameError。"""
        code = wash.gen_wash_code()
        self.assertEqual(len(code), 7)
        self.assertTrue(code.isalnum(), code)

    def test_codes_are_random(self):
        codes = {wash.gen_wash_code() for _ in range(50)}
        self.assertGreater(len(codes), 45, "随机码重复太多，随机源有问题")

    def test_module_has_required_imports(self):
        """提交路径上用到的模块级名字必须都在。"""
        for name in ("secrets", "string"):
            self.assertTrue(hasattr(wash, name), f"wash.py 缺少 import {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
