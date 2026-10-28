"""注册所有路由。"""
from aiogram import Dispatcher

from handlers import (
    add_area_code,
    buy,
    classify,
    dedup,
    extract,
    lottery,
    membership,
    merge,
    phone_check,
    rename,
    sort,
    split,
    start,
    vcf,
    wash,
    xlsx_convert,
    zip_extract,
)


def register_all_routers(dp: Dispatcher) -> None:
    # 先注册洗数据回传路由，避免被其他文件 handler 拦截
    dp.include_router(wash.wash_reply_router)
    dp.include_router(start.router)
    dp.include_router(membership.router)
    dp.include_router(buy.router)
    dp.include_router(lottery.router)
    dp.include_router(phone_check.router)
    dp.include_router(extract.router)
    dp.include_router(add_area_code.router)
    dp.include_router(split.router)
    dp.include_router(zip_extract.router)
    dp.include_router(rename.router)
    dp.include_router(vcf.router)
    dp.include_router(dedup.router)
    dp.include_router(classify.router)
    dp.include_router(merge.router)
    dp.include_router(sort.router)
    dp.include_router(xlsx_convert.router)
    # wash 路由放最后，避免通用文件 handler 拦截其他功能的文件
    dp.include_router(wash.router)
