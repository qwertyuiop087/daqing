"""纯文件处理逻辑：只依赖标准库，便于本地单元测试。"""
import calendar
import datetime
import io
import os
import random
import re
import zipfile


# ---------------- 编码处理 ----------------


def decode_bytes(data: bytes) -> str:
    """优先 UTF-8 解码，失败回退 GBK 等编码，保证中文文件不乱码。

    同时剥掉文件开头的 UTF-8 BOM（记事本保存的文件常带 BOM，
    否则首行第一个字段会被 \ufeff 污染而无法识别）。
    """
    for enc in ("utf-8", "gbk", "gb18030", "utf-16", "latin-1"):
        try:
            text = data.decode(enc)
            if text.startswith("\ufeff"):
                text = text[1:]
            return text
        except (UnicodeDecodeError, ValueError):
            continue
    text = data.decode("utf-8", errors="ignore")
    return text[1:] if text.startswith("\ufeff") else text


def read_file(path: str) -> bytes:
    """读取文件字节并确保关闭句柄（避免长时间运行泄漏文件描述符）。"""
    with open(path, "rb") as f:
        return f.read()


def sanitize_filename(name: str) -> str:
    """清理用户上传的文件名，防止路径穿越与非法字符。"""
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip()
    return name or "file.txt"


# ---------------- 文件切割 ----------------


def split_lines(content: str, lines_per_file: int) -> list[str]:
    """把文本按行数切分为多份，返回各份内容列表。"""
    if lines_per_file <= 0:
        raise ValueError("每份行数必须大于 0")
    lines = content.splitlines()
    if not lines:
        return []
    return [
        "\n".join(lines[i:i + lines_per_file])
        for i in range(0, len(lines), lines_per_file)
    ]


def insert_phones_randomly(files: list[str], phones: list[str]) -> tuple[list[str], list[str]]:
    """把每个手机号随机插入某一份文件的随机一行（作为新行插入）。

    返回 (新文件内容列表, 报告行列表)。
    报告行格式：文件N 第X行 插入: 手机号
    """
    if not files:
        return [], []
    new_files = list(files)
    report: list[str] = []
    for phone in phones:
        f_idx = random.randrange(len(new_files))
        lines = new_files[f_idx].splitlines()
        insert_pos = random.randint(0, len(lines))  # 允许插到末尾
        lines.insert(insert_pos, phone)
        new_files[f_idx] = "\n".join(lines)
        report.append(f"文件{f_idx + 1} 第{insert_pos + 1}行 插入: {phone}")
    return new_files, report


def insert_phones_per_file(
    files: list[str], phones: list[str], per_file: int
) -> tuple[list[str], list[str]] | None:
    """每份文件随机插入指定数量的手机号（从号码池随机抽取）。

    号码数量不足 len(files) * per_file 时，自动循环复用号码池，直到插满；
    号码池为空时返回 None（由调用方提示）。
    返回 (新文件内容列表, 报告行列表)。
    报告行格式：文件N 第X行 插入: 手机号
    """
    if not files or per_file <= 0:
        return list(files), []
    if not phones:
        return None
    pool = list(phones)
    random.shuffle(pool)
    new_files = list(files)
    report: list[str] = []
    idx = 0
    for f_idx, content in enumerate(files):
        lines = content.splitlines()
        for _ in range(per_file):
            phone = pool[idx % len(pool)]
            idx += 1
            insert_pos = random.randint(0, len(lines))  # 允许插到末尾
            lines.insert(insert_pos, phone)
            report.append(f"文件{f_idx + 1} 第{insert_pos + 1}行 插入: {phone}")
        new_files[f_idx] = "\n".join(lines)
    return new_files, report


# ---------------- ZIP 解压 ----------------


def extract_txt_from_zip(
    zip_bytes: bytes,
    max_txt: int = 100,
    max_size: int = 50 * 1024 * 1024,
    max_total_size: int = 200 * 1024 * 1024,
) -> list[tuple[str, str]]:
    """安全解压 ZIP，只提取其中的 TXT 文件。

    返回 [(文件名, 文本内容)]，重名文件自动加 (1)(2) 后缀。
    防 Zip Slip：拒绝包含 .. 路径或路径穿越的条目。
    防内存炸弹：单文件超过 max_size 跳过，累计解压字节超过 max_total_size 停止。
    """
    result: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if not name.lower().endswith((".txt", ".text")):
                continue
            parts = name.split("/")
            if ".." in parts or not parts[-1]:
                continue
            if info.file_size > max_size:
                continue
            data = zf.read(info.filename)
            total += len(data)
            if total > max_total_size:
                break
            base = sanitize_filename(parts[-1])
            seen[base] = seen.get(base, 0) + 1
            if seen[base] > 1:
                stem, ext = os.path.splitext(base)
                base = f"{stem}({seen[base] - 1}){ext}"
            result.append((base, decode_bytes(data)))
            if len(result) >= max_txt:
                break
    return result


# ---------------- 号码处理 ----------------


def parse_numbers(text: str) -> list[str]:
    """从文本中解析号码列表（一行一个，自动跳过空行）。"""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def normalize_phone_digits(line: str) -> str:
    """提取一行中的纯数字，用于去重判断。

    若为 13 位且以 86 开头（如 +86 138... / 86138...），去掉国家码 86，
    与本地 11 位号码视为同一号码。
    """
    digits = re.sub(r"\D", "", line)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits


def dedup_numbers(lines: list[str]) -> tuple[list[str], int]:
    """号码去重：按纯数字去重，保留首次出现的原始行。

    返回 (去重后有效号码行, 被移除的重复数量)。空行/无数字行不计入。
    """
    seen: set[str] = set()
    unique: list[str] = []
    removed = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        key = normalize_phone_digits(line)
        if not key:
            continue
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        unique.append(line)
    return unique, removed


def numbers_to_vcf(numbers: list[str], contact_prefix: str = "联系人") -> str:
    """号码列表转 VCF 通讯录文件内容。"""
    blocks: list[str] = []
    for i, num in enumerate(numbers, 1):
        blocks.append(
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            f"FN:{contact_prefix}{i}\n"
            f"TEL;TYPE=CELL:{num}\n"
            "END:VCARD"
        )
    return "\n".join(blocks)


# ---------------- 合并文本 ----------------


def merge_texts(files: list[tuple[str, str]], with_headers: bool = True) -> str:
    """合并多个 (文件名, 内容)。with_headers=True 时在每段前加文件名分隔标记。"""
    parts: list[str] = []
    for name, text in files:
        content = text.rstrip("\n")
        if with_headers:
            parts.append(f"===== {name} =====")
        parts.append(content)
    # 不加 headers 时直接拼接，不留空行；加 headers 时用空行分隔更清晰
    sep = "\n\n" if with_headers else "\n"
    return sep.join(parts)


# ---------------- 定制分包：字段识别 + 按指定顺序重排 ----------------


FIELD_NAMES = ("手机号", "银行卡", "身份证", "金额", "时间", "名字", "其他")

# 用户输入的字段别名 → 规范名
FIELD_ALIASES = {
    "手机号": "手机号", "手机": "手机号", "电话": "手机号", "号码": "手机号",
    "手机号码": "手机号", "电话号码": "手机号",
    "银行卡": "银行卡", "银行卡号": "银行卡", "卡号": "银行卡", "银行": "银行卡",
    "身份证": "身份证", "身份证号": "身份证", "证件": "身份证", "证件号": "身份证",
    "金额": "金额", "钱": "金额", "价格": "金额", "金额数": "金额",
    "时间": "时间", "日期": "时间", "时间日期": "时间", "日期时间": "时间",
    "名字": "名字", "姓名": "名字", "名称": "名字", "客户名": "名字", "称呼": "名字",
    "其他": "其他", "其它": "其他", "未知": "其他",
}

_RE_DATE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?"
    r"|\d{4}年\d{1,2}月\d{1,2}日?"
    r"|\d{1,2}:\d{2}(:\d{2})?"
)
_RE_AMOUNT = re.compile(
    r"^[¥￥$]?\s*-?\s*(\d{1,3}(,\d{3})*|\d{1,12})(\.\d{1,4})?\s*(元|块|万)?$"
)

# 地址类关键词：含这些字的中文串基本是地址/备注，不是人名
_ADDR_KEYWORDS = (
    "省", "市", "县", "区", "镇", "乡", "村", "路", "街", "号", "楼", "栋",
    "室", "单元", "地址", "大道", "巷", "组", "队", "社区", "广场",
    "花园", "大厦", "公寓", "小区", "街道",
)


def _valid_ymd(ymd: str) -> bool:
    if not (ymd.isdigit() and len(ymd) == 8):
        return False
    y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
    if not (1900 <= y <= 2100 and 1 <= m <= 12):
        return False
    return 1 <= d <= calendar.monthrange(y, m)[1]


def _is_valid_id(compact: str) -> bool:
    """18 位（末位可为 X）或 15 位，且出生日期段合法、不是未来日期。"""
    if len(compact) == 18 and compact[:17].isdigit() and compact[17] in "0123456789Xx":
        ymd = compact[6:14]
        # 生日必须合法，且不能晚于今天（18 位银行卡号里恰好出现"合法未来日期"
        # 段的（如 622136203010137980，2030-10-13）不能误判为身份证）
        return _valid_ymd(ymd) and ymd <= datetime.date.today().strftime("%Y%m%d")
    if len(compact) == 15 and compact.isdigit():
        return _valid_ymd("19" + compact[6:12])
    return False


def _looks_like_amount(s: str, digits: str) -> bool:
    """金额识别：有明确金额符号/单位/小数点/千分位的一律算金额；
    纯整数无任何标记的，只接受 1~9 位（最大到亿级），
    10 位及以上的纯数字更可能是异常号码，归到其他字段，避免错放金额位。
    """
    if not _RE_AMOUNT.match(s):
        return False
    if re.search(r"[¥￥$元块万,.]", s):
        return True
    return bool(digits.isdigit() and 1 <= len(digits) <= 9)


def detect_field_type(line: str) -> str:
    """识别一行内容的字段类型：手机号/银行卡/身份证/金额/时间/名字/其他。

    判断优先级：身份证 → 手机号 → 银行卡 → 时间 → 金额 → 名字 → 其他。
    """
    s = line.strip()
    if not s:
        return "其他"
    compact = re.sub(r"[\s\-()（）]", "", s)          # 去掉空格/横杠/括号，用于号码类判断
    digits = re.sub(r"\D", "", s)                     # 纯数字

    # 1) 身份证（18位，最后一位可以是X/x）
    if _is_valid_id(compact):
        return "身份证"

    # 2) 手机号（11 位，1 开头，第二位 3-9，可带 +86 / 空格 / 横杠）
    if re.fullmatch(r"(\+86)?1[3-9]\d{9}", compact):
        return "手机号"

    # 3) 银行卡（16~21 位纯数字；身份证已在第 1 步优先识别，
    #    走到这里的 18 位数字必不是合法身份证，按银行卡处理）
    if re.fullmatch(r"\d+", compact) and 16 <= len(compact) <= 21:
        if len(compact) != 11:
            return "银行卡"

    # 4) 时间（含 YYYYMMDD 紧凑日期格式，或常见日期格式）
    if len(digits) == 8 and _valid_ymd(digits):
        return "时间"
    if _RE_DATE.search(s):
        return "时间"

    # 5) 金额（含 ¥/$/元/万 或小数；纯整数 10 位以上不归金额，避免异常号码错位）
    if _looks_like_amount(s, digits):
        return "金额"

    # 6) 名字（可含 ·，支持少数民族长名字）
    # 排除带"银行"字样的（银行名）。
    # 关键：路/楼/栋/号/室/组/队 等字虽也出现在地址里，但同样常见于人名
    # （路炜、韩国栋、王华楼、吕路婷…），因此短中文串（≤4字）直接按人名处理，
    # 不做地址关键词检查；地址关键词只用于排除 5 字以上的长中文串。
    if re.fullmatch(r"[\u4e00-\u9fff·]{1,10}", s) and "银行" not in s:
        if "·" in s:
            # 含·的少数民族名（古丽·阿依、赛乃木·艾尔西丁）：按·分段，每段 1~6 字
            parts = s.split("·")
            if all(1 <= len(p) <= 6 for p in parts):
                return "名字"
        elif len(s) <= 4:
            # 1~4 字纯中文即人名（覆盖单字名、复姓、双名）
            return "名字"
        elif len(s) == 5 and not any(k in s for k in _ADDR_KEYWORDS):
            # 5 字无·中文（如 买买提明），不含地址词才按人名
            return "名字"
        # 6 字以上无·中文（如"广东省湛江市"、"这是一段长备注"）归其他

    # 7) 带特殊符号的字段（@、引号、括号等）直接归为"其他"
    if re.search(r'[@"\'()（）\[\]【】<>《》]', s):
        return "其他"

    # 8) 带"银行"字样的，归为其他（多余字段）
    if "银行" in s:
        return "其他"

    return "其他"


# ---------------- 定制分包：行内多字段识别 + 调换顺序 ----------------

# 分隔符候选（优先级高的靠前，用于识别一行里多个字段用什么分隔）
_DELIM_PREF = {"\t": 60, "|": 50, " ": 40, ",": 30, "，": 20, ";": 10, "；": 10}


def detect_delimiter(lines: list[str]) -> str:
    """检测多字段记录行的分隔符（制表符/竖线/空格/逗号等）。

    规则：选择能把最多行切成段数一致的那个分隔符；得分相同取优先级高的。
    """
    best_d, best_score = " ", -1.0
    for d, pref in _DELIM_PREF.items():
        counts: list[int] = []
        matched = 0
        for ln in lines:
            if d in ln:
                n = len(ln.split(d))
                if n >= 2:
                    counts.append(n)
                    matched += 1
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        score = counts.count(mode) * 100.0 + pref
        if score > best_score:
            best_d, best_score = d, score
    return best_d


# 粘连 token：中文开头（1~5 字）+ 数字结尾（≥4 位），如 “刘玲500240198612311624”
_MIXED_TOKEN = re.compile(r"^([\u4e00-\u9fff·]{1,5})(\d[\d\s\-]{3,})$")


def split_record(line: str, delim: str) -> list[str]:
    """把一行按分隔符切成字段 token（去掉空段）。

    自动把“名字+号码”粘连的 token（如 刘玲500240198612311624）
    拆成两个独立字段，以便分别识别和调序。
    """
    line = line.strip()
    if delim == " ":
        parts = [t for t in re.split(r"\s+", line) if t]
    else:
        parts = [t.strip() for t in line.split(delim) if t.strip()]
    expanded: list[str] = []
    for p in parts:
        # 清理 BOM / 零宽字符，避免字段识别被不可见字符干扰
        p = re.sub(r"[\ufeff\u200b\u200c\u200d]", "", p)
        if not p:
            continue
        m = _MIXED_TOKEN.match(p)
        if m:
            expanded.append(m.group(1))  # 名字部分
            expanded.append(m.group(2))  # 数字部分（再走正常字段识别）
        else:
            expanded.append(p)
    return expanded


def analyze_record_order(lines: list[str], delim: str, treat_6digit_as_bank: bool = False) -> tuple[list[str], dict[str, int], list[list[str]]]:
    """分析整批记录行的字段结构。

    逐行识别每个 token 的字段类型，统计各类型总数量，
    并收集批内出现过的所有行内顺序（行间顺序可能不一致，如
    一行 手机号→名字→银行卡，另一行 名字→手机号→银行卡）。
    返回 (主流顺序, 各字段数量字典, 行内顺序变体列表)。

    特殊规则：treat_6digit_as_bank=True 且整批没有 16-21 位银行卡时，
    才把不规则的 6 位纯数字（后三位非 000）当成银行卡（优先于金额）。
    """
    positions: dict[int, list[str]] = {}
    counts: dict[str, int] = {}
    variants: list[list[str]] = []
    seen_variants: set[tuple[str, ...]] = set()

    # 先逐行识别
    for ln in lines:
        tokens = split_record(ln, delim)
        types = [detect_field_type(t) for t in tokens]
        for i, t in enumerate(types):
            positions.setdefault(i, []).append(t)
            if t != "其他":
                counts[t] = counts.get(t, 0) + 1
        line_order = list(dict.fromkeys(t for t in types if t != "其他"))
        key = tuple(line_order)
        if line_order and key not in seen_variants:
            seen_variants.add(key)
            variants.append(line_order)

    # 特殊规则：开启后，整批没有识别到 16-21 位银行卡时，
    # 才把不规则 6 位纯数字（后三位非 000）当成银行卡（优先于金额）
    if treat_6digit_as_bank and "银行卡" not in counts:
        # 统计每一列的6位纯数字数量（不规则的，后三位不是000的）
        six_digit_counts = {}
        for col, types_list in positions.items():
            # 这一列是6位纯数字的行数
            six_digit_count = 0
            for i, t in enumerate(types_list):
                # 检查是不是6位纯数字，且后三位不是000（不规则）
                tokens = split_record(lines[i], delim)
                if col < len(tokens):
                    val = tokens[col].strip()
                    if re.fullmatch(r"\d{6}", val) and not val.endswith("000"):
                        six_digit_count += 1
            if six_digit_count > 0:
                six_digit_counts[col] = six_digit_count

        # 如果有不规则6位纯数字的列，把它们当成银行卡
        if six_digit_counts:
            # 找数量最多的那列
            best_col = max(six_digit_counts, key=six_digit_counts.get)
            # 把这一列的类型改成银行卡，同时从金额计数里减掉
            changed_count = 0
            for i in range(len(lines)):
                tokens = split_record(lines[i], delim)
                if best_col < len(tokens):
                    val = tokens[best_col].strip()
                    if re.fullmatch(r"\d{6}", val) and not val.endswith("000"):
                        # 如果原来的类型是金额，就从金额里减掉
                        if positions[best_col][i] == "金额":
                            counts["金额"] = counts.get("金额", 0) - 1
                        positions[best_col][i] = "银行卡"
                        changed_count += 1
            counts["银行卡"] = changed_count

            # 重新生成顺序
            order: list[str] = []
            for i in sorted(positions):
                top = max(set(positions[i]), key=positions[i].count)
                if top != "其他" and top not in order:
                    order.append(top)
            return order, counts, variants

    order: list[str] = []
    for i in sorted(positions):
        top = max(set(positions[i]), key=positions[i].count)
        if top != "其他" and top not in order:
            order.append(top)
    return order, counts, variants


def parse_desired_order(text: str):
    """解析用户输入的期望顺序。

    返回 (规范字段名列表, None)；若含无法识别的词，返回 (None, 该词)。
    """
    tokens = [t for t in re.split(r"[\s、,，;；/→>—\-]+", text.strip()) if t]
    result: list[str] = []
    for tok in tokens:
        canon = FIELD_ALIASES.get(tok)
        if canon is None:
            return None, tok
        if canon not in result:
            result.append(canon)
    return result, None


def _clean_amount(token: str) -> str:
    """清理金额字段：把 .00 / .000 这种全零小数尾缀去掉。
    例如：100.000 -> 100，50.00 -> 50；
    含有效小数的原样保留（100.50 -> 100.50，不截断尾零）。
    """
    s = token.strip()
    # 匹配 数字.数字 的格式
    m = re.fullmatch(r"^([\d,]+)\.(\d+)$", s)
    if not m:
        return s
    int_part, dec_digits = m.group(1), m.group(2)
    # 小数部分全是 0 才去掉小数；有任何非零数字则原样保留
    if set(dec_digits) == {"0"}:
        return int_part
    return s


def reorder_record_fields(tokens: list[str], desired: list[str], del_other: bool = False, treat_6digit_as_bank: bool = False) -> list[str]:
    """调换一行内字段 token 的顺序。

    - 按用户指定顺序取对应类型的 token（同类型保持原先后顺序）；
    - 用户未提到的已知字段按原顺序跟在后面；
    - "其他"（无法识别）token 始终放最后，如果 del_other=True 则直接删除。
    - 金额字段自动清理 .000 这类无意义小数尾缀。
    - 如果 treat_6digit_as_bank=True，把6位纯数字当成银行卡（优先于金额）。
    """
    types = []
    # 预扫描：该行是否已经有 16~21 位的完整银行卡号。
    # 有的话，6 位纯数字（如 199500、131493）一定是金额，不能再升级成银行卡，
    # 否则金额和银行卡会互相错位。
    has_real_bank = any(
        detect_field_type(t.strip()) == "银行卡" for t in tokens if t.strip()
    )
    for t in tokens:
        tt = detect_field_type(t)
        # 如果要把6位数字当银行卡，优先于金额
        # 规则：纯6位数字、后三位不是000（不是整千整万的金额）、且本行没有完整银行卡号，才当银行卡
        if treat_6digit_as_bank and not has_real_bank and re.fullmatch(r"\d{6}", t.strip()):
            val = t.strip()
            # 后三位是000，说明是整千金额，不当银行卡
            if not val.endswith("000"):
                tt = "银行卡"
        types.append(tt)

    used = [False] * len(tokens)
    out: list[str] = []
    for want in desired:
        for i, tt in enumerate(types):
            if not used[i] and tt == want:
                val = tokens[i]
                if tt == "金额":
                    val = _clean_amount(val)
                out.append(val)
                used[i] = True
    rest_known: list[str] = []
    rest_other: list[str] = []
    for i, tt in enumerate(types):
        if not used[i]:
            val = tokens[i]
            if tt == "金额":
                val = _clean_amount(val)
            (rest_known if tt != "其他" else rest_other).append(val)
    if del_other:
        return out + rest_known
    return out + rest_known + rest_other


def reorder_file_lines(lines: list[str], delim: str, desired: list[str], del_other: bool = False, treat_6digit_as_bank: bool = False) -> list[str]:
    """把整个文件每行的字段都按指定顺序调换，返回新行列表。"""
    return [delim.join(reorder_record_fields(split_record(ln, delim), desired, del_other, treat_6digit_as_bank)) for ln in lines]


# ---------------- xlsx → txt 保真转换 ----------------

def _xlsx_cell_to_text(value) -> str:
    """把 xlsx 单元格的值转成文本，尽量保持原样。

    - 整数/整数型浮点直接输出，不用科学计数法（如 1400 → "1400"）；
    - 小数按实际值输出（如 100.5 → "100.5"）；
    - 日期 → YYYY-MM-DD，日期时间 → YYYY-MM-DD HH:MM:SS；
    - 布尔 → TRUE / FALSE；
    - 空单元格 → ""。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime.datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat()
    return str(value)


def xlsx_to_lines(data: bytes) -> list[str]:
    """把 xlsx / xls 字节内容转成制表符分隔的文本行（内容尽量原样保留）。

    - 每行 = 工作表的一行，各列用制表符连接；
    - 空单元格保留为空字段，整行全空跳过；
    - 单元格内的换行/制表符替换为空格，保证一行一条记录、不乱；
    - 遍历全部工作表（第一个 sheet 之后的数据也会被读取）。
    """
    from io import BytesIO

    # 先判断是 xls 还是 xlsx：xlsx 是 zip 格式，文件头是 PK
    is_xls = data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    lines: list[str] = []

    def _append_row(cells) -> None:
        if not any(cells):
            return
        line = "\t".join(cells)
        line = line.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        lines.append(line)

    if is_xls:
        # 旧版 xls 格式，用 xlrd
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        try:
            for sheet_idx in range(wb.nsheets):
                ws = wb.sheet_by_index(sheet_idx)
                for row_idx in range(ws.nrows):
                    row = ws.row_values(row_idx)
                    _append_row([_xlsx_cell_to_text(v) for v in row])
        finally:
            wb.release_resources()
    else:
        # xlsx 格式，用 openpyxl
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(data), data_only=True)
        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    _append_row([_xlsx_cell_to_text(v) for v in row])
        finally:
            wb.close()

    return lines
