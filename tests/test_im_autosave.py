"""洗完的苹果自动入库 回归测试。

需求：用户每次洗完苹果 iMessage，群里判定成「苹果」的号码要自动写进本地号码库，
下次别人送来同样的号，本地撞库直接命中（省群洗的钱）。

约定：
1. 新号插入、老号（原来是安卓）改成苹果；
2. 只认「苹果 iMessage」的任务 —— 支付宝/微信洗出来的也是手机号，不能当苹果入库；
3. 后台可以把自动入库关掉（im_autosave=0）。

运行：python tests/test_im_autosave.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="imsave_db_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 't.db')}"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")

import handlers.wash as wash_module  # noqa: E402
from database import (  # noqa: E402
    SessionLocal,
    bulk_upsert_phone_profiles,
    bulk_add_phone_profiles,
    get_config,
    init_db,
    lookup_phone_profiles,
    set_config,
)

CODE = "Sav0001"
NEW_PHONES = ["13800000011", "13800000012"]


class FakeBot:
    def __init__(self, content: str):
        self.content = content

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="remote")

    async def download_file(self, file_path, destination):
        with open(destination, "w", encoding="utf-8") as fh:
            fh.write(self.content)


def _set_meta(service: str) -> None:
    import json

    with SessionLocal() as session:
        set_config(
            session, f"wash_meta:{CODE}", json.dumps({"service": service}, ensure_ascii=False)
        )


def _kinds(phones) -> dict:
    with SessionLocal() as session:
        return lookup_phone_profiles(session, phones)


class TestAutosave(unittest.TestCase):
    def setUp(self):
        init_db()
        with SessionLocal() as session:
            set_config(session, "im_autosave", "1")
            # 每个用例从空库开始，避免上一个用例写入的号码干扰断言
            from database import PhoneProfile

            session.query(PhoneProfile).delete()
            session.commit()

    def test_bulk_upsert_inserts_and_updates_kind(self):
        with SessionLocal() as session:
            bulk_add_phone_profiles(session, [("13900000001", "安卓")])
        with SessionLocal() as session:
            stat = bulk_upsert_phone_profiles(
                session, [("13900000001", "苹果"), ("13900000002", "苹果")]
            )
        kinds = _kinds(["13900000001", "13900000002"])
        self.assertEqual(kinds["13900000001"], "苹果", "老号应该从安卓改成苹果")
        self.assertEqual(kinds["13900000002"], "苹果", "新号应该插进去")
        self.assertEqual(stat["new"], 1)
        self.assertEqual(stat["updated"], 1)
        self.assertEqual(stat["skipped"], 0)
        self.assertEqual(stat["total"], 2)

    def test_import_dedups_batch_and_existing_rows(self):
        """入库要先去重：批内重复只算一次，库里已有的不再写。"""
        with SessionLocal() as session:
            bulk_add_phone_profiles(
                session, [("13900000001", "苹果"), ("13900000002", "苹果")]
            )
        with SessionLocal() as session:
            stat = bulk_upsert_phone_profiles(
                session,
                [
                    ("13900000001", "苹果"),   # 库里已有，跳过
                    ("13900000002", "苹果"),   # 库里已有，跳过
                    ("13900000003", "苹果"),   # 新号
                    ("13900000003", "苹果"),   # 批内重复，只算一次
                    ("13900000004", "苹果"),   # 新号
                ],
            )
        self.assertEqual(stat["total"], 4, "批内重复要去重")
        self.assertEqual(stat["new"], 2)
        self.assertEqual(stat["skipped"], 2)
        with SessionLocal() as session:
            from sqlalchemy import func, select

            from database import PhoneProfile

            total = int(session.scalar(select(func.count()).select_from(PhoneProfile)) or 0)
        self.assertEqual(total, 4, "库里不该出现重复行")

    def test_reimport_is_idempotent(self):
        """同一批洗好的苹果重复入库：第二次全是「已存在跳过」，不新增。"""
        with SessionLocal() as session:
            first = bulk_upsert_phone_profiles(session, [(p, "苹果") for p in NEW_PHONES])
        with SessionLocal() as session:
            second = bulk_upsert_phone_profiles(session, [(p, "苹果") for p in NEW_PHONES])
        self.assertEqual(first["new"], 2)
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], 2)

    def test_apple_result_is_saved(self):
        _set_meta("🍎 苹果 iMessage")
        bot = FakeBot("\n".join(NEW_PHONES))
        asyncio.run(
            wash_module._remember_washed_apples(
                bot, SimpleNamespace(file_id="fid"), CODE
            )
        )
        kinds = _kinds(NEW_PHONES)
        self.assertEqual(kinds.get(NEW_PHONES[0]), "苹果")
        self.assertEqual(kinds.get(NEW_PHONES[1]), "苹果")

    def test_other_service_is_not_saved(self):
        """支付宝的结果也是手机号，但它不是苹果号，不能入库。"""
        _set_meta("💙 支付宝")
        bot = FakeBot("\n".join(NEW_PHONES))
        asyncio.run(
            wash_module._remember_washed_apples(
                bot, SimpleNamespace(file_id="fid"), CODE
            )
        )
        self.assertEqual(_kinds(NEW_PHONES), {}, "非苹果业务的结果不该入库")

    def test_switch_off_does_not_save(self):
        _set_meta("🍎 苹果 iMessage")
        with SessionLocal() as session:
            set_config(session, "im_autosave", "0")
        bot = FakeBot("\n".join(NEW_PHONES))
        asyncio.run(
            wash_module._remember_washed_apples(
                bot, SimpleNamespace(file_id="fid"), CODE
            )
        )
        self.assertEqual(_kinds(NEW_PHONES), {})

    def test_no_meta_does_not_save(self):
        """没有任务记录的随机码（比如别人的旧结果）不往库里写。"""
        bot = FakeBot("\n".join(NEW_PHONES))
        asyncio.run(
            wash_module._remember_washed_apples(
                bot, SimpleNamespace(file_id="fid"), "Nometa1"
            )
        )
        self.assertEqual(_kinds(NEW_PHONES), {})

    def test_download_failure_is_silent(self):
        """下载失败不能把交付流程搞崩，也不能写半截数据。"""

        class BadBot:
            async def get_file(self, file_id):
                raise RuntimeError("boom")

        _set_meta("🍎 苹果 iMessage")
        asyncio.run(
            wash_module._remember_washed_apples(
                BadBot(), SimpleNamespace(file_id="fid"), CODE
            )
        )
        self.assertEqual(_kinds(NEW_PHONES), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
