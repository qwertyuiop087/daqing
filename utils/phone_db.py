"""号码识别核心：运营商判断 + 地区归属地查询。

- 运营商：3位号段硬编码查表（毫秒级）
- 地区：7位号段查内存Map（51万行号段库，启动时懒加载）
"""
from __future__ import annotations

import gzip
import os
import re
from pathlib import Path

# ──────────────────────────────────────────────
# 运营商号段表（前3位）
# ──────────────────────────────────────────────
CARRIER_PREFIXES: dict[str, list[str]] = {
    "中国移动": [
        "134", "135", "136", "137", "138", "139",
        "147", "148", "150", "151", "152",
        "157", "158", "159", "165", "172", "178",
        "182", "183", "184", "187", "188",
        "195", "197", "198",
    ],
    "中国联通": [
        "130", "131", "132",
        "145", "146", "155", "156",
        "166", "167", "171", "175", "176",
        "185", "186",
    ],
    "中国电信": [
        "133", "149", "153",
        "173", "174", "177",
        "180", "181", "189",
        "190", "191", "193", "199",
    ],
}

# 反向映射：3位号段 → 运营商
_PREFIX_TO_CARRIER: dict[str, str] = {}
for _carrier, _list in CARRIER_PREFIXES.items():
    for _p in _list:
        _PREFIX_TO_CARRIER[_p] = _carrier

# 手机号正则（提取用）
_PHONE_RE = re.compile(r"(?:\+?\s*86[\s\-().]*)?(1[3-9](?:[\s\-().]*\d){9})")
_DIGITS_RE = re.compile(r"\D")

# ──────────────────────────────────────────────
# 地区号段库（7位）懒加载
# ──────────────────────────────────────────────
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_CSV = _DATA_DIR / "phone_db.csv"
_DB_GZ = _DATA_DIR / "phone_db.csv.gz"

_region_map: dict[str, dict[str, str]] | None = None
_provinces: list[str] | None = None
_cities: list[str] | None = None


def _open_db_file():
    """优先读 gzip 压缩包，没有就读原 CSV。"""
    if _DB_GZ.exists():
        return gzip.open(_DB_GZ, "rt", encoding="utf-8")
    if _DB_CSV.exists():
        return open(_DB_CSV, "r", encoding="utf-8")
    raise FileNotFoundError(f"找不到号段数据库：{_DB_GZ} 或 {_DB_CSV}")


def _load_region_db() -> None:
    """加载CSV号段库到内存（首次调用时执行）。"""
    global _region_map, _provinces, _cities
    if _region_map is not None:
        return

    _region_map = {}
    prov_set: set[str] = set()
    city_set: set[str] = set()

    with _open_db_file() as f:
        header = f.readline()  # 跳过表头
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            phone7 = parts[1].strip()
            province = parts[2].strip()
            city = parts[3].strip()
            isp = parts[4].strip() if len(parts) > 4 else ""
            if not phone7 or len(phone7) < 7 or not province:
                continue
            _region_map[phone7[:7]] = {
                "province": province,
                "city": city or "未知",
                "isp": isp,
            }
            prov_set.add(province)
            if city:
                city_set.add(city)

    _provinces = sorted(prov_set)
    _cities = sorted(city_set)


def identify_carrier(phone: str) -> str:
    """根据手机号前3位判断运营商。

    特殊规则：1349 是电信卫星号，不按 134 算移动。
    表外号段（含170等虚商）统一归为"虚拟号"。
    """
    if not phone or len(phone) < 3:
        return "虚拟号"
    if phone.startswith("1349"):
        return "中国电信"
    return _PREFIX_TO_CARRIER.get(phone[:3], "虚拟号")


def lookup_region(phone: str) -> dict[str, str]:
    """根据手机号前7位查归属地。返回 {province, city, isp}。"""
    _load_region_db()
    if not phone or len(phone) < 7:
        return {"province": "未知", "city": "未知", "isp": ""}
    return _region_map.get(phone[:7], {"province": "未知", "city": "未知", "isp": ""})  # type: ignore[union-attr]


def normalize_phone(raw: str) -> str:
    """归一化：去掉+86前缀、空格、横线、括号等，返回纯数字。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    s = re.sub(r"[\s\-()]", "", s)
    if s.startswith("+86"):
        s = s[3:]
    elif s.startswith("86") and len(s) == 13:
        s = s[2:]
    return s


def is_mobile(phone: str) -> bool:
    """判断是否为合法11位手机号。"""
    return bool(re.match(r"^1[3-9]\d{9}$", phone))


_RE_PHONE = re.compile(r"(?<![\d])(?:\+?86)?[ -]?1[3-9](?:[ -]?\d){9}(?![\d])")


def extract_phones(text: str) -> list[str]:
    """从文本中提取全部手机号（保留出现顺序，含重复）。
    照抄老版本逻辑：只提取独立的手机号，不会从银行卡/身份证等更长数字串里截取。
    """
    result = []
    for m in _RE_PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) > 11:
            digits = digits[-11:]
        if len(digits) == 11 and digits[0] == "1" and digits[1] in "3456789":
            result.append(digits)
    return result


def phone_from_record(record: str) -> str:
    """从一行文本中提取第一个合法手机号。"""
    norm = normalize_phone(record)
    if is_mobile(norm):
        return norm
    found = extract_phones(str(record))
    return found[0] if found else ""


# ──────────────────────────────────────────────
# 批量分类
# ──────────────────────────────────────────────
def classify_phones(
    phones: list[str],
    *,
    group_by: str = "carrier",   # "carrier" 按运营商 / "province" 按省份 / "city" 按城市
) -> dict[str, list[str]]:
    """批量分类号码。

    Args:
        phones: 手机号列表（已归一化的11位数字串）
        group_by: 分组方式
            - "carrier"：按运营商分（移动/联通/电信/虚拟号）
            - "province"：按省份分
            - "city"：按城市分（省+市联合键）

    Returns:
        分组字典 {组名: [号码列表]}，已过滤空组。
    """
    _load_region_db()
    groups: dict[str, list[str]] = {}

    for p in phones:
        if group_by == "carrier":
            key = identify_carrier(p)
        elif group_by == "province":
            info = lookup_region(p)
            key = info["province"] or "未知"
        elif group_by == "city":
            info = lookup_region(p)
            prov = info["province"] or "未知"
            city = info["city"] or "未知"
            key = f"{prov}-{city}"
        else:
            raise ValueError(f"未知分组方式: {group_by}")

        if key not in groups:
            groups[key] = []
        groups[key].append(p)

    return groups


def classify_with_stats(phones: list[str]) -> dict:
    """批量分类并返回详细统计（运营商 + 省份 + 城市）。

    Returns:
        {
            "total": 总数,
            "by_carrier": {运营商: [号码列表]},
            "carrier_counts": {运营商: 数量},
            "province_counts": {省份: 数量},
            "top_provinces": [(省份, 数量), ...],  # 按数量降序前10
        }
    """
    _load_region_db()
    by_carrier: dict[str, list[str]] = {
        "中国移动": [], "中国联通": [], "中国电信": [], "虚拟号": [],
    }
    prov_counts: dict[str, int] = {}

    for p in phones:
        carrier = identify_carrier(p)
        by_carrier[carrier].append(p)

        info = lookup_region(p)
        prov = info["province"] or "未知"
        prov_counts[prov] = prov_counts.get(prov, 0) + 1

    carrier_counts = {k: len(v) for k, v in by_carrier.items()}
    top_provinces = sorted(prov_counts.items(), key=lambda x: -x[1])[:10]

    return {
        "total": len(phones),
        "by_carrier": by_carrier,
        "carrier_counts": carrier_counts,
        "province_counts": prov_counts,
        "top_provinces": top_provinces,
    }
