"""/version 更新说明回归测试：保证「版本号」与「更新列表」不会再次脱节。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

from config import CHANGELOG, settings  # noqa: E402


class VersionNotesTest(unittest.TestCase):
    def test_current_version_has_notes(self):
        """当前 VERSION 必须在 CHANGELOG 里有对应条目。"""
        versions = [v for v, _ in CHANGELOG]
        self.assertIn(
            settings.VERSION,
            versions,
            f"CHANGELOG 缺少当前版本 {settings.VERSION} 的说明，请到 config.py 补上",
        )

    def test_notes_not_empty(self):
        """每条更新说明都不能为空，也不能有空白项。"""
        notes = dict(CHANGELOG)[settings.VERSION]
        self.assertTrue(notes, "当前版本的更新说明为空")
        for item in notes:
            self.assertTrue(str(item).strip(), "存在空的更新说明条目")

    def test_versions_unique(self):
        """版本号不能重复，否则取的顺序不确定。"""
        versions = [v for v, _ in CHANGELOG]
        self.assertEqual(len(versions), len(set(versions)), "CHANGELOG 中存在重复版本号")


if __name__ == "__main__":
    unittest.main(verbosity=2)
