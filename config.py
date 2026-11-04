"""全局配置：从环境变量 / .env 文件读取。"""
import os

from dotenv import load_dotenv

load_dotenv()


def _clean_env(s: str) -> str:
    """清洗环境变量：自动把全角符号替换成半角，去掉首尾空格和引号。"""
    if not s:
        return ""
    s = s.strip()
    # 去掉首尾的引号（中英文）
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    # 全角符号 → 半角
    replacements = {
        "：": ":",   # 中文冒号
        "，": ",",   # 中文逗号
        "　": " ",   # 中文空格
        "（": "(",   # 中文括号
        "）": ")",
        "＝": "=",   # 中文等号
        "／": "/",   # 中文斜杠
        "＠": "@",   # 中文@
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s


class Settings:
    # Telegram Bot Token（必填，从 @BotFather 获取）
    BOT_TOKEN: str = _clean_env(os.getenv("BOT_TOKEN", ""))

    # 管理员 Telegram 数字 ID，多个用英文逗号分隔（例如 123456789,987654321）
    ADMIN_IDS: list[int] = [
        int(x) for x in _clean_env(os.getenv("ADMIN_IDS", "")).split(",") if x.strip().isdigit()
    ]

    # 总管理员 Telegram 数字 ID，多个用英文逗号分隔（例如 100000001,100000002）
    # 总管理员拥有最高权限：可管理管理员（增删）、调整洗数据价格下限与累计洗数据配额。
    SUPER_ADMIN_IDS: list[int] = [
        int(x) for x in _clean_env(os.getenv("SUPER_ADMIN_IDS", "")).split(",") if x.strip().isdigit()
    ]

    # 数据库连接串。留空则使用本地 SQLite（bot_data.db）。
    # Railway 上建议创建 Postgres 并把连接串填到这里，保证数据持久化。
    DATABASE_URL: str = _clean_env(os.getenv("DATABASE_URL", ""))

    # Webhook 模式：填入 Railway 分配的域名（如 https://xxx.up.railway.app）后启用 Webhook；
    # 留空则使用长轮询模式（适合本地调试）。
    WEBHOOK_DOMAIN: str = os.getenv("WEBHOOK_DOMAIN", "").rstrip("/")
    WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

    # 服务端口（Railway 会自动注入 PORT）
    PORT: int = int(os.getenv("PORT", "8080"))

    # ---------------- 功能限制（可按需调整） ----------------
    # Telegram 机器人 API 实际只能下载 ≤20MB 的文件，所以默认上限统一为 20MB
    MAX_FILE_SIZE: int = int(os.getenv("MAX_FILE_SIZE_MB", "20")) * 1024 * 1024   # 单个上传文件大小上限
    MAX_SPLIT_FILES: int = int(os.getenv("MAX_SPLIT_FILES", "500"))                # 单次切割最大份数
    MAX_SPLIT_LINES: int = int(os.getenv("MAX_SPLIT_LINES", "200000"))             # 每份最大行数
    MAX_BATCH_FILES: int = int(os.getenv("MAX_BATCH_FILES", "10"))                 # 每次批量发送文件数（Telegram 媒体组上限 10）
    MAX_PHONES: int = int(os.getenv("MAX_PHONES", "50000"))                       # 单次最多处理/插入手机号数
    MAX_UPLOAD_FILES: int = int(os.getenv("MAX_UPLOAD_FILES", "100"))             # 合并/重命名最多上传文件数
    MAX_ZIP_SIZE: int = int(os.getenv("MAX_ZIP_SIZE_MB", "20")) * 1024 * 1024      # ZIP 文件大小上限
    MAX_ZIP_TXT_FILES: int = int(os.getenv("MAX_ZIP_TXT_FILES", "500"))           # ZIP 内最多提取 TXT 数量

    # ---------------- 空号检测配置 ----------------
    # 空号检测API地址（留空则此功能不可用）
    PHONE_CHECK_API_URL: str = _clean_env(os.getenv("PHONE_CHECK_API_URL", ""))
    # 空号检测API密钥
    PHONE_CHECK_API_KEY: str = _clean_env(os.getenv("PHONE_CHECK_API_KEY", ""))

    # ---------------- 小号登录（洗数据中转用） ----------------
    # Telegram API ID（从 my.telegram.org 获取，放环境变量）
    TELETHON_API_ID: int = int(os.getenv("TELETHON_API_ID", "0") or 0)
    # Telegram API Hash（从 my.telegram.org 获取，放环境变量）
    TELETHON_API_HASH: str = _clean_env(os.getenv("TELETHON_API_HASH", ""))
    # 小号手机号（带国家码，如 +8613800138000）
    TELETHON_PHONE: str = _clean_env(os.getenv("TELETHON_PHONE", ""))
    # 会话字符串（登录后自动存数据库，不用手动填）
    TELETHON_SESSION: str = _clean_env(os.getenv("TELETHON_SESSION", ""))

    # 版本号：/version 命令会显示，用于确认线上部署的是不是最新包
    VERSION: str = _clean_env(os.getenv("BOT_VERSION", "2026-10-28.50"))


settings = Settings()


# ---------------- 版本更新说明 ----------------
# /version 只显示「当前 VERSION 对应的那一条」。
# 升版本时把新版本放在最前面，版本号必须和上面的 VERSION 完全一致（有测试校验）。
CHANGELOG: list[tuple[str, list[str]]] = [
    (
        "2026-10-28.50",
        [
            "批量洗数据按结果逐个结算，部分未回传按比例退款",
        ],
    ),
    (
        "2026-10-28.49",
        [
            "洗数据支持一次上传多个文件，按总行数计费更便宜",
            "发送失败自动退回预扣费用",
        ],
    ),
    (
        "2026-10-28.48",
        [
            "洗数据改为提交时预扣费，防止并发任务透支余额",
            "任务超时未回传自动退款并记退款流水",
            "充值入账加行锁，防止手动点击与自动到账重复入账",
        ],
    ),
    (
        "2026-10-28.47",
        [
            "洗数据读取改为自动识别 GBK 编码，中文编码文件不再读乱",
        ],
    ),
    (
        "2026-10-28.46",
        [
            "介绍页恢复到 2026-10-28.42 时的版本",
        ],
    ),
    (
        "2026-10-28.45",
        [
            "介绍页回退到上一版（简洁版式）",
        ],
    ),
    (
        "2026-10-28.44",
        [
            "介绍页重做质感：大字号标题、发丝线网格、序号编排",
            "首屏换成高保真产品演示（文件卡片 / 进度 / 结果列表）",
            "新增指标条与页脚分栏",
        ],
    ),
    (
        "2026-10-28.43",
        [
            "介绍页按同一套设计规范重做：线性图标、单一强调色、统一栅格",
            "介绍页移除渐变、发光、光标特效与抽奖转盘，改为纯内容版式",
            "套餐与中奖通知继续与机器人实时同步",
        ],
    ),
    (
        "2026-10-28.42",
        [
            "/version 的更新列表改为随版本自动显示",
            "后台界面按设计规范重做：线性图标、单一强调色、统一栅格",
            "品牌统一为「错落雨 / CUOLUOYU」，旧默认值自动迁移",
        ],
    ),
    (
        "2026-10-28.41",
        [
            "后台、介绍页、机器人回退页统一改名为「错落雨 / CUOLUOYU」",
        ],
    ),
    (
        "2026-10-28.36",
        [
            "新增全局报错通知：机器人异常会私聊提醒管理员",
            "新增后台操作记录：登录、改配置、调成员全部留痕",
            "新增洗数据小号掉线通知",
            "上传文件上限统一为 20MB（Telegram 实际限制）",
        ],
    ),
]
