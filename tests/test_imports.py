"""导入完整性回归：某个模块用了标准库却没 import，线上才会 NameError。

历史事故：
- 洗数据提交：name 'secrets' is not defined
- 邀请返佣页面：name 'html' is not defined

用 AST 扫全项目：只要某个文件里出现了「标准库模块名.属性」的用法，
该文件就必须 import 过这个模块（顶层或函数内都算）。
"""
import ast
import io
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

# 只要出现 X.Y 就必须 import X 的标准库/常用模块
MODULE_NAMES = {
    "html", "json", "os", "re", "time", "math", "uuid", "secrets", "string",
    "asyncio", "zipfile", "shutil", "tempfile", "hashlib", "hmac", "base64",
    "random", "itertools", "collections", "decimal", "datetime", "logging",
    "traceback", "csv", "io", "urllib", "typing", "dataclasses", "functools",
    "pathlib", "subprocess", "socket", "ssl", "textwrap", "unicodedata",
    "calendar", "copy", "glob", "sqlite3", "weakref", "contextlib",
}


def iter_py_files():
    for dirpath, dirnames, files in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def missing_imports(path: str) -> list[tuple[str, int]]:
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported.add(a.asname or a.name)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in MODULE_NAMES and node.id not in imported:
                problems.append((node.id, node.lineno))
    return problems


class TestImports(unittest.TestCase):
    def test_all_stdlib_usages_imported(self):
        bad = []
        for path in iter_py_files():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep):
                continue
            for name, line in missing_imports(path):
                bad.append(f"{rel}:{line} 使用了 {name} 但没有 import")
        self.assertFalse(bad, "缺 import：\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
