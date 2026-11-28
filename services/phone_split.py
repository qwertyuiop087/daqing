"""按手机号拆包：找到号码在第几行，把名单切成「前段 / 后段」。

和 `services/file_ops.py` 里的切割不同：那边是按行数切，
这边是**按内容切** —— 给一个手机号，它所在的那行就是切点。

规矩（跟客户实际用法对齐）：
· 前段 = 第 1 行 ~ 号码那行（含）
· 后段 = 号码那行之后
· 号码出现在多行时取第一次出现的那行
· 原文件的换行符（CRLF / LF）和行内容原样保留，不改数据
· .md 里的 Markdown 表格会先转成制表符分隔，输出的仍是 .txt

这层只做纯文本处理，不碰 Telegram，方便单测。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# 只认 11 位大陆手机号；前后不能挨着别的数字，避免从银行卡/身份证里截
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 解码顺序：先试带 BOM 的 UTF-8，再常见中文编码，最后兜底不乱码
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5")


def decode_with_name(data: bytes) -> tuple[str, str]:
    """按常见编码依次尝试，返回 (文本, 编码名)。"""
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    from services.file_ops import decode_bytes

    return decode_bytes(data), "utf-8(容错)"


def rows_of(text: str) -> tuple[list[str], str]:
    """拆行，返回 (行列表, 换行符)。行尾多余空行丢弃，换行符保持原样。"""
    nl = "\r\n" if "\r\n" in text else "\n"
    rows = text.split(nl)
    while rows and rows[-1] == "":
        rows.pop()
    return rows, nl


def render(rows: list[str], nl: str) -> str:
    """把行拼回文本（末尾补一个换行，和原文件习惯一致）。"""
    return nl.join(rows) + nl if rows else ""


_SEP_CELL = re.compile(r"^:?-{3,}:?$")


def _strip_pipes(rows: list[str]) -> list[str] | None:
    """Markdown 表格 → 制表符分隔；不是表格返回 None。"""
    if not rows:
        return None
    piped = [r for r in rows if r.strip().startswith("|")]
    if len(piped) * 2 <= len(rows):
        return None
    out: list[str] = []
    for r in rows:
        s = r.strip()
        if not s:
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and all(_SEP_CELL.match(c) or not c for c in cells):
                continue  # 分隔行 |---|---|
            out.append("\t".join(cells))
        else:
            out.append(s)
    return out


def normalize(rows: list[str], is_md: bool = False) -> tuple[list[str], bool, bool]:
    """文本规整，返回 (行, 是否转过 Markdown 表格, 是否去过行尾空格)。"""
    table = False
    cleaned = False
    if is_md:
        piped = _strip_pipes(rows)
        if piped is not None:
            rows, table = piped, True
    # 大多数行都以两个空格结尾 = Markdown 的硬换行，切出来是脏的，统一去掉
    if rows and sum(1 for r in rows if r.endswith("  ")) * 2 > len(rows):
        rows, cleaned = [r.rstrip() for r in rows], True
    return rows, table, cleaned


def extract_phones(text: str) -> list[str]:
    """取出文本里全部手机号（去重、保留出现顺序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in PHONE_RE.finditer(text or ""):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def find_phone_row(rows: list[str], phone) -> int | None:
    """手机号所在行的 0-based 下标；找不到返回 None（取第一次出现）。"""
    p = str(phone)
    if not p:
        return None
    for i, r in enumerate(rows):
        if p in r:
            return i
    return None


@dataclass
class SplitResult:
    phone: str
    index: int                     # 0-based 命中行
    hit_row: str
    total: int
    before: list[str] = field(default_factory=list)   # 含命中行
    after: list[str] = field(default_factory=list)    # 命中行之后

    @property
    def line_no(self) -> int:
        return self.index + 1

    @property
    def before_count(self) -> int:
        return len(self.before)

    @property
    def after_count(self) -> int:
        return len(self.after)


def split_by_phone(rows: list[str], phone) -> SplitResult | None:
    idx = find_phone_row(rows, phone)
    if idx is None:
        return None
    return SplitResult(
        phone=str(phone),
        index=idx,
        hit_row=rows[idx],
        total=len(rows),
        before=rows[: idx + 1],
        after=rows[idx + 1:],
    )


def build_names(orig: str, custom: str | None, seg: str) -> dict:
    """结果文件名。

    · 只要一段 → `原名.txt`（名字就是客户自己的包名）
    · 两段都要 → 后段 `原名.txt`、前段 `原名_前.txt`（前段是「号码之前」那段）
    """
    from services.file_ops import sanitize_filename

    raw = (custom or "").strip()
    if raw:
        base = sanitize_filename(raw)
        base = re.sub(r"\.txt$", "", base, flags=re.I).strip() or "结果"
    else:
        name = sanitize_filename(orig or "结果.txt")
        base, ext = os.path.splitext(name)
        # 有些包名本身就是 xxx.txt.txt，去掉重复的尾巴
        if ext.lower() == ".txt" and base.lower().endswith(".txt"):
            base = base[:-4].rstrip(" _-·")
        base = base or "结果"
    if seg == "before":
        return {"before": f"{base}.txt"}
    if seg == "after":
        return {"after": f"{base}.txt"}
    return {"after": f"{base}.txt", "before": f"{base}_前.txt"}
