"""号码拆包（按手机号切前段/后段）回归测试。

这个功能是从另一套机器人里搬过来的，规矩要锁死：
1. 找手机号所在行，前段含命中行、后段是它之后；
2. 原文件换行符（CRLF/LF）和内容原样保留；
3. md 表格转制表符；输出统一 .txt；
4. 一次最多 5 个号码，找不到的号码要提示而不是静默；
5. 文件名：只要一段 = 原名.txt，两段都要 = 原名.txt + 原名_前.txt。

运行：python tests/test_phone_split.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="cut_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.cut_by_phone as cut  # noqa: E402
from services import phone_split as ps  # noqa: E402
from states import CutByPhoneStates  # noqa: E402

USER_ID = 727272


class TestPhoneSplitCore(unittest.TestCase):
    def test_decode_gbk_and_crlf_preserved(self):
        raw = "张三,13800000001\r\n李四,13800000002\r\n".encode("gbk")
        text, enc = ps.decode_with_name(raw)
        self.assertIn(enc, ("gb18030", "gbk"))
        rows, nl = ps.rows_of(text)
        self.assertEqual(nl, "\r\n")
        self.assertEqual(rows, ["张三,13800000001", "李四,13800000002"])

    def test_render_keeps_original_newline(self):
        rows = ["a", "b"]
        self.assertEqual(ps.render(rows, "\r\n"), "a\r\nb\r\n")
        self.assertEqual(ps.render(rows, "\n"), "a\nb\n")

    def test_markdown_table_becomes_tabs(self):
        rows = ["| 姓名 | 手机 |", "| --- | --- |", "| 张三 | 13800000001 |", "备注"]
        out, table, _cleaned = ps.normalize(rows, is_md=True)
        self.assertTrue(table)
        self.assertIn("姓名\t手机", out)
        self.assertIn("张三\t13800000001", out)
        self.assertNotIn("| --- | --- |", out)

    def test_plain_text_not_touched(self):
        rows = ["张三,13800000001", "李四,13800000002"]
        out, table, _ = ps.normalize(rows, is_md=False)
        self.assertFalse(table)
        self.assertEqual(out, rows)

    def test_extract_phones_keeps_order_and_dedupes(self):
        text = "13800000002\n13800000001\n13800000002\n不是号码 1234"
        self.assertEqual(ps.extract_phones(text), ["13800000002", "13800000001"])

    def test_split_counts_include_hit_row(self):
        rows = ["a,13800000001", "b,13800000002", "c,13800000003"]
        res = ps.split_by_phone(rows, "13800000002")
        self.assertEqual(res.line_no, 2)
        self.assertEqual(res.before, ["a,13800000001", "b,13800000002"])
        self.assertEqual(res.after, ["c,13800000003"])
        self.assertEqual(res.before_count, 2)
        self.assertEqual(res.after_count, 1)

    def test_phone_not_found(self):
        rows = ["a,13800000001"]
        self.assertIsNone(ps.split_by_phone(rows, "13900000009"))

    def test_phone_on_first_and_last_line(self):
        rows = ["13800000001", "b", "c"]
        first = ps.split_by_phone(rows, "13800000001")
        self.assertEqual(first.before_count, 1)
        self.assertEqual(first.after_count, 2)

        last = ps.split_by_phone(rows, "c")
        self.assertEqual(last.after_count, 0)

    def test_build_names(self):
        self.assertEqual(ps.build_names("112名单.txt", None, "after"), {"after": "112名单.txt"})
        self.assertEqual(ps.build_names("112名单.txt", None, "before"), {"before": "112名单.txt"})
        self.assertEqual(
            ps.build_names("112名单.txt", None, "both"),
            {"after": "112名单.txt", "before": "112名单_前.txt"},
        )
        self.assertEqual(
            ps.build_names("112名单.txt", "新包", "both"),
            {"after": "新包.txt", "before": "新包_前.txt"},
        )


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.current_state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, value):
        self.current_state = getattr(value, "state", value)

    async def get_state(self):
        return self.current_state

    async def clear(self):
        self.data = {}


class FakeBot:
    def __init__(self, content: str):
        self.content = content
        self.docs = []

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="remote")

    async def download_file(self, file_path, destination):
        with open(destination, "w", encoding="utf-8", newline="") as fh:
            fh.write(self.content)

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.docs.append((chat_id, getattr(document, "filename", ""), caption))


class FakeMessage:
    def __init__(self, user_id=USER_ID, text=None, file_name=None, bot=None):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)
        self.text = text
        self.answers = []
        self.markups = []
        self.docs = []
        self.bot = bot
        self.document = (
            SimpleNamespace(file_id="fid", file_name=file_name, file_size=1024)
            if file_name
            else None
        )

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=1)

    async def answer_document(self, document, caption=None, **kw):
        self.docs.append((getattr(document, "filename", ""), caption))
        return SimpleNamespace(message_id=2)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.answers.append(text)
        self.markups.append(reply_markup)


class FakeCq:
    def __init__(self, data, message, user_id=USER_ID):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers = []
        self.bot = message.bot

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append(text)


CONTENT = "张三,13800000001\n李四,13800000002\n王五,13800000003\n"


class TestCutFlow(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="cut_work_")
        self.bot = FakeBot(CONTENT)
        self.state = FakeState({"workdir": self.workdir})

    def _upload(self):
        msg = FakeMessage(file_name="112名单.txt", bot=self.bot)
        asyncio.run(cut.cut_got_file(msg, self.state, self.bot))
        return msg

    def test_upload_reports_lines_and_waits_phone(self):
        msg = self._upload()
        self.assertIn("共 <b>3</b> 行", msg.answers[-1])
        self.assertEqual(self.state.current_state, CutByPhoneStates.waiting_phone.state)

    def test_full_flow_both_segments(self):
        self._upload()
        phone_msg = FakeMessage(text="13800000002", bot=self.bot)
        asyncio.run(cut.cut_got_phone(phone_msg, self.state))
        self.assertIn("在第 <b>2</b> 行", phone_msg.answers[-1])
        self.assertEqual(self.state.current_state, CutByPhoneStates.waiting_segment.state)

        cq = FakeCq("cut:seg:both", phone_msg)
        asyncio.run(cut.cut_pick_segment(cq, self.state))
        self.assertEqual(self.state.current_state, CutByPhoneStates.waiting_name.state)

        deliver_msg = FakeMessage(bot=self.bot)
        asyncio.run(cut.cut_use_orig_name(FakeCq("cut:name:orig", deliver_msg), self.state))
        names = [d[0] for d in deliver_msg.docs]
        self.assertEqual(names, ["112名单.txt", "112名单_前.txt"])

        # 前段两行、后段一行；文件内容不能错位
        for fname, _cap in deliver_msg.docs:
            path = os.path.join(self.workdir, fname)
            with open(path, encoding="utf-8") as fh:
                got = fh.read().splitlines()
            if fname.endswith("_前.txt"):
                self.assertEqual(got, ["张三,13800000001", "李四,13800000002"])
            else:
                self.assertEqual(got, ["王五,13800000003"])

    def test_phone_not_found_keeps_waiting(self):
        self._upload()
        msg = FakeMessage(text="13900000009", bot=self.bot)
        asyncio.run(cut.cut_got_phone(msg, self.state))
        self.assertIn("没找到", " ".join(msg.answers))
        self.assertEqual(self.state.current_state, CutByPhoneStates.waiting_phone.state)

    def test_batch_limits_to_five_and_queues(self):
        self._upload()
        phones = "\n".join(f"1380000000{i}" for i in range(1, 8))
        msg = FakeMessage(text=phones, bot=self.bot)
        asyncio.run(cut.cut_got_phone(msg, self.state))
        self.assertTrue(any("最多" in a for a in msg.answers))
        self.assertEqual(len(self.state.data["queue"]), 4)  # 5 个：1 个在办，4 个排队

    def test_custom_name(self):
        self._upload()
        asyncio.run(cut.cut_got_phone(FakeMessage(text="13800000003", bot=self.bot), self.state))
        asyncio.run(
            cut.cut_pick_segment(FakeCq("cut:seg:before", FakeMessage(bot=self.bot)), self.state)
        )
        # 走自定义：先点「自定义名字」，再发名字
        deliver_msg = FakeMessage(bot=self.bot)
        asyncio.run(cut.cut_use_custom_name(FakeCq("cut:name:custom", deliver_msg), self.state))
        name_msg = FakeMessage(text="新包", bot=self.bot)
        asyncio.run(cut.cut_custom_name_input(name_msg, self.state))
        names = [d[0] for d in name_msg.docs]
        self.assertEqual(names, ["新包.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
