"""苹果 iMessage 预撞库回归测试。

需求（老板原话）：洗数据里有人在洗「苹果 iM」（不是撞库）时，
机器人先拿他的文件去撞一次库（本地号码库），
再把撞库出来的苹果拿去群里洗，洗完把苹果发回去；
另外把「撞库出来的安卓」和「洗完的安卓」相加，也发回去。

这里覆盖四件事：
1. 本地拆分：苹果进送群清单、安卓留在本地，号码原样保留（不去重）；
2. 状态合并：撞库安卓 + 洗完安卓 相加去重，落盘后能再读出来；
3. 结算口径：只按送去群洗的苹果行数预扣费、pending 行数也是苹果行数；
4. 端到端：提交 → 小号回传安卓结果 → 自动多发一份「安卓合计」。

运行：python tests/test_im_prewash.py
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="imprewash_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
import services.im_prewash as impre  # noqa: E402
from database import (  # noqa: E402
    AppConfig,
    SessionLocal,
    add_wash_balance,
    bulk_add_phone_profiles,
    get_config,
    get_or_create_user,
    init_db,
    set_config,
)
from sqlalchemy import select  # noqa: E402
from states import WashStates  # noqa: E402

USER_ID = 314159
ALT_ID = 271828
CHAT_ID = 900001

APPLE_PHONES = ["13800000001", "13800000002"]
ANDROID_PHONES = ["13900000003", "13900000004"]
WASHED_ANDROID = ["13900000005"]


def _write(path: str, lines: list) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


class FakeBot:
    """记录发送；回传的安卓结果由 download_file 写进临时文件。"""

    def __init__(self):
        self.docs = []
        self.messages = []

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.docs.append((chat_id, getattr(document, "filename", ""), caption))

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="remote")

    async def download_file(self, file_path, destination):
        _write(destination, WASHED_ANDROID)


class FakeMessage:
    def __init__(self, code="", name="名单-苹果-2-安卓-1.txt", caption="检测完成统计"):
        self.document = SimpleNamespace(
            file_id="fid",
            file_name=f"{code}_{name}" if code else name,
            file_unique_id=f"u_{name}",
            file_size=123,
        )
        self.caption = caption
        self.from_user = SimpleNamespace(id=ALT_ID)

    async def answer(self, text, **kw):
        return SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID), message_id=11)


class FakeCqMessage:
    def __init__(self):
        self.edits = []
        self.chat = SimpleNamespace(id=CHAT_ID)
        self.message_id = 10
        self._next = 20

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text, **kw):
        self._next += 1
        return SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID), message_id=self._next)


class FakeCq:
    def __init__(self):
        self.answers = []
        self.message = FakeCqMessage()
        self.from_user = SimpleNamespace(id=USER_ID, username="t", first_name="T")

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self, data):
        self.data = dict(data)

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_state(self):
        return self.data.get("_state")

    async def set_state(self, value):
        self.data["_state"] = getattr(value, "state", value)

    async def clear(self):
        self.data = {}


def _latest_code() -> str:
    """取最近一次提交生成的随机码（wash_code 的值是 user:时间戳）。"""
    best_ts, best_code = -1, ""
    with SessionLocal() as session:
        for row in session.scalars(
            select(AppConfig).where(AppConfig.key.like("wash_code:%"))
        ):
            parts = (row.value or "").split(":")
            try:
                ts = int(parts[1])
            except (IndexError, ValueError):
                ts = 0
            if ts >= best_ts:
                best_ts, best_code = ts, row.key.split(":", 1)[1]
    return best_code


class TestImPrewashSplit(unittest.TestCase):
    def setUp(self):
        init_db()
        # 状态目录是全局的：每个用例用独立随机码、独立临时目录，互不干扰
        self.workdir = tempfile.mkdtemp(prefix="impre_work_")
        with SessionLocal() as session:
            bulk_add_phone_profiles(
                session,
                [(p, "苹果") for p in APPLE_PHONES] + [("13700000009", "安卓")],
            )

    def test_split_puts_apple_in_submit_and_android_aside(self):
        """苹果号进送群清单，其余（含库里没有的号）都算安卓。"""
        path = os.path.join(self.workdir, "名单.txt")
        _write(path, APPLE_PHONES + ANDROID_PHONES)

        res = impre.split_files(
            [{"name": "名单.txt", "path": path, "lines": 4, "phones": 4}], self.workdir
        )

        self.assertEqual(res["stats"]["phones"], 4)
        self.assertEqual(res["stats"]["apple"], 2)
        self.assertEqual(res["stats"]["android"], 2)
        self.assertEqual(len(res["apple_files"]), 1)
        self.assertEqual(len(res["android_files"]), 1)
        self.assertEqual(res["apple_files"][0]["name"], "名单_苹果_2.txt")
        self.assertEqual(res["android_files"][0]["name"], "名单_安卓_2.txt")
        with open(res["apple_files"][0]["path"], encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), APPLE_PHONES)
        self.assertEqual(res["android"], ANDROID_PHONES)

    def test_split_keeps_repeated_phones(self):
        """不去重：用户给多少行就洗多少行；重复号码照洗、照按行计费。"""
        path = os.path.join(self.workdir, "重复.txt")
        _write(path, ["13800000001", "13800000001", "13900000003", "13900000003"])

        res = impre.split_files(
            [{"name": "重复.txt", "path": path, "lines": 4, "phones": 4}], self.workdir
        )

        self.assertEqual(res["stats"]["phones"], 4)
        self.assertEqual(res["stats"]["apple"], 2)
        self.assertEqual(res["stats"]["android"], 2)
        self.assertEqual(res["android"], ["13900000003", "13900000003"])
        with open(res["apple_files"][0]["path"], encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), ["13800000001", "13800000001"])

    def test_split_all_android_has_no_apple_files(self):
        """整批全是安卓时不应该有任何送群文件（调用方据此免单）。"""
        path = os.path.join(self.workdir, "全安卓.txt")
        _write(path, ANDROID_PHONES)

        res = impre.split_files(
            [{"name": "全安卓.txt", "path": path, "lines": 2, "phones": 2}], self.workdir
        )

        self.assertEqual(res["apple_files"], [])
        self.assertEqual(len(res["android_files"]), 1)
        self.assertEqual(res["android"], ANDROID_PHONES)


class TestImPrewashMerge(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_merge_adds_collision_android_and_washed_android(self):
        """「撞库的安卓」+「洗完的安卓」直接相加（不去重，重叠的也留着）。"""
        code = "Mrg0001"
        impre.save_state(code, USER_ID, "名单", ANDROID_PHONES + ["13800000001"], 1)
        impre.add_washed_android(code, ["13900000004"] + WASHED_ANDROID)

        merged = impre.build_merged(code)

        self.assertIsNotNone(merged)
        self.assertEqual(merged["pre"], 3)
        self.assertEqual(merged["post"], 2)
        # 不去重：13900000004 两边都有就出现两次
        self.assertEqual(merged["total"], 5)
        self.assertEqual(merged["name"], "名单_安卓合计_5.txt")
        with open(merged["path"], encoding="utf-8") as fh:
            self.assertEqual(
                fh.read().split(),
                [
                    "13900000003",
                    "13900000004",
                    "13800000001",
                    "13900000004",
                    "13900000005",
                ],
            )

    def test_merge_without_washed_android_still_returns_collision_part(self):
        """群里一个安卓都没判出来时，撞库安卓也要发给用户。"""
        code = "Mrg0002"
        impre.save_state(code, USER_ID, "名单", ANDROID_PHONES, 1)

        merged = impre.build_merged(code)

        self.assertEqual(merged["pre"], 2)
        self.assertEqual(merged["post"], 0)
        self.assertEqual(merged["total"], 2)

    def test_mark_sent_is_sticky(self):
        """安卓合计每个任务只发一次（重启后也不会重发）。"""
        code = "Mrg0003"
        impre.save_state(code, USER_ID, "名单", ANDROID_PHONES, 1)
        self.assertFalse(impre.already_sent(code))
        impre.mark_sent(code)
        self.assertTrue(impre.already_sent(code))

    def test_unknown_code_has_no_state(self):
        self.assertIsNone(impre.load_state("NoSuch1"))
        self.assertIsNone(impre.build_merged("NoSuch1"))
        self.assertFalse(impre.state_exists("NoSuch1"))
        # 非法随机码（含路径符号）一律当没有，防止写坏目录
        self.assertIsNone(impre.load_state("../etc/passwd"))


class TestAndroidResultDetection(unittest.TestCase):
    def test_group_result_names(self):
        """群里结果名形如 xxxx-苹果-927-安卓-29.txt：看倒数第二段判类型。"""
        self.assertTrue(impre.is_android_result("FBhFN4E_r6EYSW0_123-苹果-927-安卓-29.txt"))
        self.assertFalse(impre.is_android_result("FBhFN4E_r6EYSW0_123-苹果-927-苹果-898.txt"))
        self.assertTrue(impre.is_android_result("FBhFN4E_名单_安卓_12.txt"))
        self.assertFalse(impre.is_android_result("FBhFN4E_名单_苹果_12.txt"))

    def test_user_filename_with_android_word_is_not_misread(self):
        """用户自己的文件名里带「安卓」时，苹果结果不能被误判成安卓结果。"""
        self.assertFalse(impre.is_android_result("Ab12Cd3_3月安卓名单_苹果_500.txt"))
        self.assertFalse(impre.is_android_result("Ab12Cd3_安卓客户_苹果_77.txt"))

    def test_caption_hint(self):
        self.assertTrue(impre.is_android_result("Ab12Cd3_结果.txt", caption="安卓明细"))


class TestImPrewashSubmitFlow(unittest.TestCase):
    """端到端：只送苹果去群洗，但收费按原始行数正常收；回传安卓后自动补发合计。"""

    def setUp(self):
        init_db()
        self.workdir = tempfile.mkdtemp(prefix="impre_flow_")
        with SessionLocal() as session:
            get_or_create_user(session, USER_ID)
            add_wash_balance(session, USER_ID, 100.0)
            set_config(session, "alt_user_id", str(ALT_ID))
            set_config(session, "wash_chat_id", str(-100123456))
            bulk_add_phone_profiles(session, [(p, "苹果") for p in APPLE_PHONES])

        path = os.path.join(self.workdir, "客户名单.txt")
        _write(path, APPLE_PHONES + ANDROID_PHONES)
        self.state = FakeState(
            {
                "workdir": self.workdir,
                "files": [
                    {
                        "name": "客户名单.txt",
                        "path": path,
                        "lines": 4,
                        "phones": 4,
                    }
                ],
                "_state": WashStates.waiting_file.state,
            }
        )
        # 报告/合计的安静期在测试里不需要等
        self._orig_report_quiet = wash_module.REPORT_QUIET_SECONDS
        self._orig_final_quiet = wash_module.IM_FINALIZE_QUIET_SECONDS
        wash_module.REPORT_QUIET_SECONDS = 0.0
        wash_module.IM_FINALIZE_QUIET_SECONDS = 0.0

        from utils import telethon_client

        self._tc = telethon_client
        self._orig_userbot = telethon_client.userbot
        telethon_client.userbot = SimpleNamespace(is_connected=lambda: True)

    def tearDown(self):
        wash_module.REPORT_QUIET_SECONDS = self._orig_report_quiet
        wash_module.IM_FINALIZE_QUIET_SECONDS = self._orig_final_quiet
        self._tc.userbot = self._orig_userbot

    def _submit(self, bot):
        cq = FakeCq()
        asyncio.run(
            wash_module._wash_submit_impl(
                cq, self.state, bot, svc_code="im", svc_label="🍎 苹果 iMessage"
            )
        )
        return cq

    def test_only_apple_is_forwarded_but_charged_normally(self):
        bot = FakeBot()
        cq = self._submit(bot)

        code = _latest_code()
        self.assertTrue(code, "没有生成洗数据随机码")

        # 只有「苹果」那份发给小号，安卓不送群
        to_alt = [d for d in bot.docs if d[0] == ALT_ID]
        self.assertEqual(len(to_alt), 1, to_alt)
        self.assertTrue(to_alt[0][1].endswith("_苹果_2.txt"), to_alt[0][1])
        self.assertTrue(to_alt[0][1].startswith(f"{code}_"), to_alt[0][1])
        self.assertEqual(to_alt[0][2], "im")

        with SessionLocal() as session:
            pending = get_config(session, f"wash_pending:{code}", "")
        # 收费照常：上传 4 行就按 4 行预扣、按 4 行结算（不因为只洗苹果而少收）
        self.assertTrue(pending.startswith(f"{USER_ID}:4:"), pending)
        # 预扣金额 = 4 行的阶梯价，而不是 2 行的
        charged = float(pending.split(":")[2])
        with SessionLocal() as session:
            from database import calc_wash_cost

            self.assertAlmostEqual(charged, round(calc_wash_cost(session, 4), 4), places=4)

        # 撞库安卓已登记，等群洗结果回来合并
        state = impre.load_state(code)
        self.assertIsNotNone(state)
        self.assertEqual(state["pre"], 2)

        shown = " ".join(cq.message.edits)
        self.assertIn("本地撞库", shown)
        self.assertIn("计费行数：4 行", shown)
        self.assertIn("送群洗：2 行", shown)

    def test_android_reply_triggers_android_total(self):
        bot = FakeBot()
        self._submit(bot)
        code = _latest_code()

        async def flow():
            handler = wash_module.alt_user_reply
            msg = FakeMessage(code=code, name="客户名单-苹果-2-安卓-2.txt")
            await handler(msg, bot)
            # 等「安静期」结束后的合计投递任务
            await asyncio.sleep(0.2)

        asyncio.run(flow())

        to_user = [d for d in bot.docs if d[0] == USER_ID]
        self.assertTrue(to_user, "用户一个文件都没收到")
        merged = [d for d in to_user if "安卓合计" in d[1]]
        self.assertTrue(merged, to_user)
        # 撞库 2 个 + 群里判定的 1 个 = 3 个
        self.assertIn("安卓合计_3", merged[0][1])
        self.assertTrue(impre.already_sent(code))

    def test_toggle_off_sends_whole_file(self):
        """后台关掉预撞库（im_prewash=0）时退回老流程：整批文件直接送群洗。"""
        with SessionLocal() as session:
            set_config(session, "im_prewash", "0")
        bot = FakeBot()
        self._submit(bot)
        code = _latest_code()

        to_alt = [d for d in bot.docs if d[0] == ALT_ID]
        self.assertEqual(len(to_alt), 1, to_alt)
        self.assertIn("客户名单", to_alt[0][1])
        self.assertNotIn("苹果", to_alt[0][1])
        with SessionLocal() as session:
            pending = get_config(session, f"wash_pending:{code}", "")
        self.assertTrue(pending.startswith(f"{USER_ID}:4:"), pending)
        self.assertIsNone(impre.load_state(code))

        with SessionLocal() as session:
            set_config(session, "im_prewash", "1")


def _all_codes() -> set:
    with SessionLocal() as session:
        rows = session.scalars(
            select(AppConfig).where(AppConfig.key.like("wash_code:%"))
        )
        return {r.key.split(":", 1)[1] for r in rows}


class TestImPrewashEdgeCases(unittest.TestCase):
    """边界：一份全安卓的文件、重复投递、空文件、其它洗数据类型不受影响。"""

    def setUp(self):
        init_db()
        self.workdir = tempfile.mkdtemp(prefix="impre_edge_")
        with SessionLocal() as session:
            get_or_create_user(session, USER_ID)
            add_wash_balance(session, USER_ID, 100.0)
            set_config(session, "alt_user_id", str(ALT_ID))
            set_config(session, "wash_chat_id", str(-100123456))
            set_config(session, "im_prewash", "1")
            bulk_add_phone_profiles(session, [(p, "苹果") for p in APPLE_PHONES])

        self._orig_report_quiet = wash_module.REPORT_QUIET_SECONDS
        self._orig_final_quiet = wash_module.IM_FINALIZE_QUIET_SECONDS
        wash_module.REPORT_QUIET_SECONDS = 0.0
        wash_module.IM_FINALIZE_QUIET_SECONDS = 0.0

        from utils import telethon_client

        self._tc = telethon_client
        self._orig_userbot = telethon_client.userbot
        telethon_client.userbot = SimpleNamespace(is_connected=lambda: True)

    def tearDown(self):
        wash_module.REPORT_QUIET_SECONDS = self._orig_report_quiet
        wash_module.IM_FINALIZE_QUIET_SECONDS = self._orig_final_quiet
        self._tc.userbot = self._orig_userbot
        with SessionLocal() as session:
            set_config(session, "im_prewash", "1")

    def _file(self, name, lines):
        path = os.path.join(self.workdir, name)
        _write(path, lines)
        return {"name": name, "path": path, "lines": len(lines), "phones": len(lines)}

    def _submit_files(self, files, svc_code="im", label="🍎 苹果 iMessage"):
        before = _all_codes()
        st = FakeState(
            {"workdir": self.workdir, "files": files, "_state": WashStates.waiting_file.state}
        )
        cq = FakeCq()
        bot = FakeBot()
        asyncio.run(
            wash_module._wash_submit_impl(cq, st, bot, svc_code=svc_code, svc_label=label)
        )
        return cq, bot, (_all_codes() - before)

    def test_fully_android_file_is_still_billed(self):
        """整批照常收费：整份都是安卓的文件也算行数（不因为它没送群就免单）。"""
        a = self._file("混合.txt", APPLE_PHONES + ["13900000003"])   # 3 行：苹果 2 + 安卓 1
        b = self._file("全安卓.txt", ["13900000006", "13900000007"])  # 2 行：全安卓

        cq, _bot, new = self._submit_files([a, b])
        self.assertEqual(len(new), 1, new)
        code = next(iter(new))

        with SessionLocal() as session:
            pending = get_config(session, f"wash_pending:{code}", "")
        # 上传 5 行就收 5 行；安卓那份的行数摊到送群文件上，分摊比例之和也对得上
        self.assertTrue(pending.startswith(f"{USER_ID}:5:"), pending)
        bill = json.loads(pending.split(":", 4)[4])["files"]
        self.assertEqual(sum(bill), 5, bill)
        self.assertEqual(impre.load_state(code)["pre"], 3)  # 1 + 2
        shown = " ".join(cq.message.edits)
        self.assertIn("计费行数：5 行", shown)

    def test_duplicate_android_result_not_counted_twice(self):
        """同一个结果文件被重复投递：号码只记一次，安卓合计不会翻倍。"""
        f = self._file("客户名单.txt", APPLE_PHONES + ANDROID_PHONES)
        _cq, bot, new = self._submit_files([f])
        code = next(iter(new))

        async def flow():
            msg = FakeMessage(code=code, name="客户名单-苹果-2-安卓-2.txt")
            await wash_module.alt_user_reply(msg, bot)
            await wash_module.alt_user_reply(msg, bot)  # 群消息重投
            await asyncio.sleep(0.2)

        asyncio.run(flow())
        merged = [d for d in bot.docs if d[0] == USER_ID and "安卓合计" in d[1]]
        self.assertTrue(merged, bot.docs)
        # 撞库 2 + 洗完 1 = 3（重复投递不再加一次）
        self.assertIn("安卓合计_3", merged[0][1])

    def test_empty_batch_tells_user_no_phones(self):
        """空文件/认不出号码：明确提示，不扣费、不送群。"""
        from database import get_wash_balance

        with SessionLocal() as session:
            before = get_wash_balance(session, USER_ID)
        empty = self._file("空.txt", [])
        cq, bot, new = self._submit_files([empty])
        self.assertEqual(new, set(), "空文件不该产生任务")
        self.assertEqual([d for d in bot.docs if d[0] == ALT_ID], [])
        shown = " ".join(cq.message.edits)
        self.assertIn("没有识别到手机号", shown)
        with SessionLocal() as session:
            self.assertAlmostEqual(get_wash_balance(session, USER_ID), before)

    def test_empty_apple_library_falls_back_to_whole_file(self):
        """本地库没有苹果号时绝不预分：否则整批会被当安卓免费放走、根本送不进群。"""
        from database import PhoneProfile

        with SessionLocal() as session:
            for row in list(session.query(PhoneProfile)):
                session.delete(row)
            session.commit()
        with SessionLocal() as session:
            self.assertEqual(impre.library_size(session), 0)

        f = self._file("名单.txt", APPLE_PHONES + ANDROID_PHONES)
        _cq, bot, new = self._submit_files([f])
        code = next(iter(new))
        to_alt = [d for d in bot.docs if d[0] == ALT_ID]
        self.assertEqual(len(to_alt), 1, to_alt)
        self.assertNotIn("苹果", to_alt[0][1])  # 整份文件，不拆
        with SessionLocal() as session:
            pending = get_config(session, f"wash_pending:{code}", "")
        self.assertTrue(pending.startswith(f"{USER_ID}:4:"), pending)
        self.assertIsNone(impre.load_state(code))

        # 恢复库，别影响后面的用例
        with SessionLocal() as session:
            bulk_add_phone_profiles(session, [(p, "苹果") for p in APPLE_PHONES])

    def test_all_android_batch_is_free_and_not_forwarded(self):
        """整批没苹果号：不送群、不扣费，直接把撞库安卓发回。"""
        from database import get_wash_balance

        f = self._file("纯安卓.txt", ANDROID_PHONES)
        with SessionLocal() as session:
            before = get_wash_balance(session, USER_ID)
        cq, bot, new = self._submit_files([f])
        self.assertEqual(new, set(), "没送群就不该有任务")
        self.assertEqual([d for d in bot.docs if d[0] == ALT_ID], [])
        sent = [d for d in bot.docs if d[0] == CHAT_ID]
        self.assertEqual(len(sent), 1, bot.docs)
        self.assertIn("安卓", sent[0][1])
        self.assertIn("没有苹果号", " ".join(cq.message.edits))
        with SessionLocal() as session:
            self.assertAlmostEqual(get_wash_balance(session, USER_ID), before)

    def test_double_submit_is_rejected(self):
        """提交过程中再点一次不会重复扣费（预撞库让提交变慢，窗口更大）。"""
        import time as _time

        from database import get_wash_balance

        f = self._file("名单.txt", APPLE_PHONES)
        with SessionLocal() as session:
            balance_before = get_wash_balance(session, USER_ID)
        st = FakeState(
            {
                "workdir": self.workdir,
                "files": [f],
                "submitting_until": _time.time() + 60,
                "_state": WashStates.waiting_file.state,
            }
        )
        cq, bot = FakeCq(), FakeBot()
        asyncio.run(
            wash_module._wash_submit_impl(cq, st, bot, svc_code="im", svc_label="🍎 苹果 iMessage")
        )
        self.assertTrue(
            any("正在提交中" in (t or "") for t, _show in cq.answers), cq.answers
        )
        self.assertEqual([d for d in bot.docs if d[0] == ALT_ID], [])
        with SessionLocal() as session:
            self.assertAlmostEqual(get_wash_balance(session, USER_ID), balance_before)

    def test_other_service_untouched(self):
        """其它洗数据类型（支付宝）不受预撞库影响：整份文件照旧送群。"""
        f = self._file("支付宝.txt", APPLE_PHONES + ANDROID_PHONES)
        _cq, bot, new = self._submit_files([f], svc_code="ZFB", label="💙 支付宝")
        code = next(iter(new))
        to_alt = [d for d in bot.docs if d[0] == ALT_ID]
        self.assertEqual(len(to_alt), 1, to_alt)
        self.assertEqual(to_alt[0][2], "ZFB")
        with SessionLocal() as session:
            pending = get_config(session, f"wash_pending:{code}", "")
        self.assertTrue(pending.startswith(f"{USER_ID}:4:"), pending)
        self.assertIsNone(impre.load_state(code))


if __name__ == "__main__":
    unittest.main(verbosity=2)
