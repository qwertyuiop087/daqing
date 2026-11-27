"""苹果 iMessage 洗数据「预撞库」：先按本地库把号码拆成苹果 / 安卓，再送群洗。

业务背景（老板原话）：洗数据里有「苹果 iM」和「苹果 iM 撞库」两个服务。
撞库只查本地号码库（phone_profiles），不花钱也不占群里的工位；
送群洗的苹果 iM 才是真金白银按行数计费。

现在的流程改成：

1. 用户点「🍎 苹果 iMessage」提交（不是「苹果 iM 撞库」那一项）时，
   先用本地库把这批号码拆成两份：
   · 苹果（本地库命中）→ 带随机码发给小号 → 洗数据群洗；
   · 安卓（本地库没命中，含库里没有的号）→ 不送群，留在本地；
2. 群洗回来的结果照常发给用户，那就是「洗好的苹果」；
3. 群里判定出来的安卓（文件名尾巴带「安卓」的结果）跟第 1 步的
   撞库安卓相加，合成一份「安卓合计」发回给用户。

为什么只送苹果：安卓号送群洗是白花钱，本地库已经能判掉；
而且用户真正要的是苹果号的清洗结果，安卓那份只是附带的产出。

状态按 7 位随机码落盘（`%TEMP%/dq_im_prewash` 下的 meta/pre/post 三个文件），
机器人重启后回传的结果还能继续合并，7 天后自动清理。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

# 状态目录：放在系统临时目录，和用户的 tmp_workdir 分开，
# 这样上传目录被 cleanup_state 删掉之后合并状态还在。
STATE_DIR = os.path.join(tempfile.gettempdir(), "dq_im_prewash")
# 状态保留时长：群洗结果一般几分钟内回来，留 7 天足够排查
STATE_TTL_SECONDS = 7 * 24 * 60 * 60

_PREFIX_RE = re.compile(r"^[a-zA-Z0-9]{7}_")
_FIELD_SPLIT_RE = re.compile(r"[-_—－\s]+")
_DIGITS_RE = re.compile(r"^\d+$")
_CODE_RE = re.compile(r"^[A-Za-z0-9]{7}$")
_ANDROID_TOKENS = ("安卓", "android")
# 群里可能用的安卓标签（倒数第二段），全部小写比较
_ANDROID_LABELS = {"安卓", "安卓机", "安卓号", "安卓版", "andriod", "android"}

# 同一随机码的多个结果文件是并发到达的，合并状态必须串行读改写
_LOCK = threading.Lock()


# ---------------- 开关 ----------------

def enabled(session) -> bool:
    """读取「苹果 iM 预撞库」开关（后台可配，默认开启）。"""
    from database import get_config

    try:
        raw = (get_config(session, "im_prewash", "1") or "").strip().lower()
    except Exception:
        return True
    return raw not in ("0", "false", "off", "no", "关", "关闭")


def library_size(session) -> int:
    """本地库里有多少个「苹果」号（撞库前的兜底检查用）。

    库里一个苹果号都没有时，整批都会被判成安卓 —— 那样用户的苹果数据
    会被当成「全是安卓」免费放走，根本送不进洗数据群。
    所以苹果名单为空时调用方要跳过预撞库，走原来整批送群的流程。
    """
    from sqlalchemy import func, select

    from database import PhoneProfile

    try:
        return int(
            session.scalar(
                select(func.count())
                .select_from(PhoneProfile)
                .where(PhoneProfile.kind == "苹果")
            )
            or 0
        )
    except Exception:
        return 0


# ---------------- 号码读写 ----------------

def _read_phones(path: str) -> list[str]:
    """读一个 txt，按出现顺序取出全部手机号。"""
    from services.file_ops import decode_bytes, read_file
    from utils.phone_db import extract_phones

    return extract_phones(decode_bytes(read_file(path)))


def _write_lines(path: str, lines: list[str]) -> None:
    """写文本文件（一行一个号码，UTF-8）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def file_stem(name: str) -> str:
    """取文件名主体（去掉扩展名与路径），最多 50 字，避免文件名过长。"""
    from services.file_ops import sanitize_filename

    base = os.path.basename(str(name or "").replace("\\", "/"))
    stem = os.path.splitext(base)[0] or "result"
    return (sanitize_filename(stem) or "result")[:50]


def _unique_name(name: str, used: set) -> str:
    """同一批里两个同名文件不能互相覆盖，重名自动加 (2)(3)。"""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem}({n}){ext}" in used:
        n += 1
    final = f"{stem}({n}){ext}"
    used.add(final)
    return final


# ---------------- 本地撞库拆分 ----------------

def split_files(files: list, workdir: str) -> dict:
    """把这批上传文件按本地号码库拆成「苹果（送群洗）/ 安卓（不送群）」。

    files: 上传时登记的 [{"name", "path", "lines", "phones"}, ...]
    返回：
        apple_files    送群洗的苹果文件（可直接替换 files 传给转发流程）
        android_files  撞库安卓文件（全部安卓时直接发给用户）
        android        撞库安卓号码（原样保留，含重复，合并「安卓合计」用）
        stats          统计：files / phones / apple / android
    """
    from database import SessionLocal, lookup_phone_profiles

    os.makedirs(workdir, exist_ok=True)

    # 逐文件处理（不要先把整批号码都读进内存）：
    # 20 个 20MB 的文件全部留在内存里会把进程撑爆，
    # 这里读完一个文件就写盘、只留安卓那份待合并。
    apple_files: list[dict] = []
    android_files: list[dict] = []
    android_all: list[str] = []
    used_names: set = set()
    apple_total = 0
    phone_total = 0

    for idx, f in enumerate(files):
        # 不去重：用户给多少行就洗多少行、算多少行的钱，重复号码照洗
        phones = [p for p in _read_phones(f.get("path") or "") if p]
        phone_total += len(phones)
        if not phones:
            continue

        kinds: dict[str, str] = {}
        with SessionLocal() as session:
            kinds = lookup_phone_profiles(session, phones)

        stem = file_stem(f.get("name") or "input.txt")
        apple = [p for p in phones if kinds.get(p) == "苹果"]
        android = [p for p in phones if kinds.get(p) != "苹果"]
        bill_lines = len(phones)
        apple_total += len(apple)
        android_all.extend(android)
        del phones
        if apple:
            name = _unique_name(f"{stem}_苹果_{len(apple)}.txt", used_names)
            path = os.path.join(workdir, f"pre{idx}_苹果_{name}")
            _write_lines(path, apple)
            apple_files.append(
                {
                    "name": name,
                    "path": path,
                    "lines": len(apple),
                    "phones": len(apple),
                    # 计费口径：这个文件对应源文件的原始行数（不去重、不因为只洗苹果而少收）
                    "bill_lines": bill_lines,
                    "src": f.get("name") or "",
                }
            )
        if android:
            name = _unique_name(f"{stem}_安卓_{len(android)}.txt", used_names)
            path = os.path.join(workdir, f"pre{idx}_安卓_{name}")
            _write_lines(path, android)
            android_files.append(
                {
                    "name": name,
                    "path": path,
                    "lines": len(android),
                    "phones": len(android),
                }
            )

    return {
        "apple_files": apple_files,
        "android_files": android_files,
        "android": android_all,
        "stats": {
            "files": len(files),
            "phones": phone_total,
            "apple": apple_total,
            "android": len(android_all),
        },
    }


# ---------------- 状态落盘 ----------------

def _safe_code(code: str) -> str:
    """随机码只能是 7 位字母数字，别的一律当没有（防路径穿越）。"""
    code = str(code or "")
    return code if _CODE_RE.match(code) else ""


def _paths(code: str) -> tuple[str, str, str]:
    return (
        os.path.join(STATE_DIR, f"{code}.json"),
        os.path.join(STATE_DIR, f"{code}.pre.txt"),
        os.path.join(STATE_DIR, f"{code}.post.txt"),
    )


def _ensure_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _cleanup_expired_locked() -> None:
    """清掉超过保留期的状态（只在写状态时顺手做，不额外跑后台任务）。"""
    try:
        cutoff = time.time() - STATE_TTL_SECONDS
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                continue
    except Exception:
        pass


def _write_meta(path: str, meta: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(tmp, path)


def save_state(code: str, user_id: int, stem: str, android: list, submitted: int) -> bool:
    """提交成功后登记本任务的撞库安卓，等群洗结果回来做合并（原样保留，不去重）。"""
    code = _safe_code(code)
    if not code:
        return False
    android = [p for p in (android or []) if p]
    meta_path, pre_path, post_path = _paths(code)
    with _LOCK:
        try:
            _ensure_dir()
            _cleanup_expired_locked()
            _write_lines(pre_path, android)
            # 新任务必须从空的「洗完安卓」开始：同码残留（上次跑测试/极端撞码）不能算进来
            _write_lines(post_path, [])
            _write_meta(
                meta_path,
                {
                    "user": int(user_id),
                    "stem": stem or "result",
                    "submitted": int(submitted or 0),
                    "pre": len(android),
                    "post": 0,
                    "ts": int(time.time()),
                },
            )
            return True
        except Exception as e:
            print(f"[im-pre] 登记撞库安卓失败 {code}: {e}")
            return False


def load_state(code: str) -> dict | None:
    """读本任务的合并状态；没有就返回 None。"""
    code = _safe_code(code)
    if not code:
        return None
    meta_path, _pre, _post = _paths(code)
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def state_exists(code: str) -> bool:
    return load_state(code) is not None


def ensure_state(code: str, user_id: int, stem: str, submitted: int = 0) -> bool:
    """给「没走预撞库」的任务补一份合并状态。

    什么时候会用到：本地库没有苹果号 / 用户关掉了预撞库 / 老任务 ——
    这时机器人没登记过撞库安卓，但群里照样会判出安卓，
    那份也得发回给用户，所以第一次见到安卓结果时把状态补上（撞库那份为空）。
    """
    if state_exists(code):
        return True
    return save_state(code, user_id, stem or "result", [], submitted)


def add_washed_android(code: str, phones: list) -> int:
    """登记群里判定出来的安卓（洗完的安卓），返回本次条数。

    追加写、不去重：多个结果文件并发到达时不会互相覆盖，
    号码按群里给的原始顺序原样记下来。
    """
    code = _safe_code(code)
    phones = [p for p in (phones or []) if p]
    if not code or not phones:
        return 0
    meta_path, _pre, post_path = _paths(code)
    with _LOCK:
        if not os.path.exists(meta_path):
            return 0
        try:
            with open(post_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(phones))
                fh.write("\n")
        except Exception as e:
            print(f"[im-pre] 记录洗完安卓失败 {code}: {e}")
            return 0
        # 顺手把累计条数写进 meta，方便排查
        try:
            meta = load_state(code) or {}
            meta["post"] = int(meta.get("post") or 0) + len(phones)
            _write_meta(meta_path, meta)
        except Exception:
            pass
    return len(phones)


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except Exception:
        return []


def build_merged(code: str) -> dict | None:
    """合成「安卓合计 = 撞库安卓 + 洗完安卓」文件，返回 {path,name,pre,post,total}。

    直接相加、不去重：撞库那份在前，群里判出来的接在后面，一行不多一行不少。
    """
    code = _safe_code(code)
    if not code:
        return None
    meta = load_state(code)
    if not meta:
        return None
    _meta_path, pre_path, post_path = _paths(code)
    with _LOCK:
        pre = _read_lines(pre_path)
        post = _read_lines(post_path)
        merged = pre + post
        if not merged:
            return None

        path = os.path.join(STATE_DIR, f"{code}.android.txt")
        try:
            _ensure_dir()
            _write_lines(path, merged)
        except Exception as e:
            print(f"[im-pre] 写安卓合计失败 {code}: {e}")
            return None

    stem = file_stem(meta.get("stem") or "result")
    return {
        "path": path,
        # 文件名不写「合计」：客户看到的就应该是「原名_安卓_N.txt」这种普通结果名
        "name": f"{stem}_安卓_{len(merged)}.txt",
        "pre": len(pre),
        "post": len(post),
        "total": len(merged),
    }


def _sent_key(code: str) -> str:
    return f"im_prewash_sent:{code}"


def already_sent(code: str) -> bool:
    """安卓合计是否已经发过（重启后也不会重复发）。"""
    from database import SessionLocal, get_config

    code = _safe_code(code)
    if not code:
        return False
    try:
        with SessionLocal() as session:
            return bool(get_config(session, _sent_key(code), ""))
    except Exception:
        return False


def mark_sent(code: str) -> None:
    from database import SessionLocal, set_config

    code = _safe_code(code)
    if not code:
        return
    try:
        with SessionLocal() as session:
            set_config(session, _sent_key(code), str(int(time.time())))
    except Exception as e:
        print(f"[im-pre] 标记安卓合计已发失败 {code}: {e}")


# ---------------- 结果文件识别 ----------------

# 群里的结果名常把两种数量都写进去：`原名-苹果-285-安卓-205.txt`。
# 这时候光看名字分不出这份文件装的是哪一半，得靠行数对号入座。
_COUNT_PAIR_RE = re.compile(r"苹果[^0-9]{0,4}(\d+).{0,8}?安卓[^0-9]{0,4}(\d+)")
_SINGLE_ANDROID_COUNT_RE = re.compile(r"安卓[^0-9]{0,4}(\d+)")


def count_pair(filename: str) -> tuple[int, int] | None:
    """从结果文件名里取 (苹果数, 安卓数)；名字里没同时写两个数量就返回 None。"""
    tail = _PREFIX_RE.sub("", str(filename or ""))
    m = _COUNT_PAIR_RE.search(tail)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None


def needs_count_probe(filename: str) -> bool:
    """名字里同时写了苹果数和安卓数时，必须下载数一下行数才知道是哪一半。"""
    return count_pair(filename) is not None


def is_android_result(
    filename: str, caption: str = "", line_count: int | None = None
) -> bool:
    """判断群回传的这个文件是不是「安卓」那份。

    判断顺序：
    1. 名字里同时有「苹果-N-安卓-M」且给了行数：按行数对号入座
       （行数≈M 就是安卓那份，≈N 就是苹果那份）。这一步最关键 ——
       群把两种数量都写进文件名时，光看最后一段会把苹果那份也判成安卓；
    2. 否则看最后一段的标签：`…-安卓-29.txt` 这种倒数第二段是
       「安卓 / 安卓机 / android」才算（「安卓对照表」这种描述段不算）；
    3. caption 里有「安卓」也算；
    4. 兜底：整个名字以「安卓」结尾。
    """
    tail = _PREFIX_RE.sub("", str(filename or ""))
    stem = os.path.splitext(tail)[0]

    pair = count_pair(filename)
    if pair and line_count:
        apple_n, android_n = pair
        if android_n == line_count and android_n != apple_n:
            return True
        if apple_n == line_count and android_n != apple_n:
            return False

    fields = [f for f in _FIELD_SPLIT_RE.split(stem) if f]
    if len(fields) >= 2 and _DIGITS_RE.match(fields[-1]):
        label = fields[-2].strip().lower()
        if label in _ANDROID_LABELS:
            return True
        if any(label.endswith(tok) for tok in _ANDROID_TOKENS):
            return True
    if any(tok in str(caption or "").lower() for tok in _ANDROID_TOKENS):
        return True
    # 兜底：整个名字就是「xxx_安卓」
    return stem.strip().endswith("安卓")


def clean_stem(filename: str) -> str:
    """去掉结果名里的 `_苹果_285-安卓_205` 尾巴，只留用户原本的名字。"""
    tail = _PREFIX_RE.sub("", str(filename or ""))
    stem = os.path.splitext(tail)[0]
    stem = re.sub(r"[-_—]?\s*苹果[^0-9]{0,4}\d+(\s*[-_—]\s*安卓[^0-9]{0,4}\d+)?$", "", stem)
    stem = re.sub(r"[-_—]?\s*安卓[^0-9]{0,4}\d+$", "", stem)
    return file_stem(stem or tail)
