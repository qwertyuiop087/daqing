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
    MAX_FILE_SIZE: int = int(os.getenv("MAX_FILE_SIZE_MB", "100")) * 1024 * 1024   # 单个上传文件大小上限
    MAX_SPLIT_FILES: int = int(os.getenv("MAX_SPLIT_FILES", "500"))                # 单次切割最大份数
    MAX_SPLIT_LINES: int = int(os.getenv("MAX_SPLIT_LINES", "200000"))             # 每份最大行数
    MAX_BATCH_FILES: int = int(os.getenv("MAX_BATCH_FILES", "10"))                 # 每次批量发送文件数（Telegram 媒体组上限 10）
    MAX_PHONES: int = int(os.getenv("MAX_PHONES", "50000"))                       # 单次最多处理/插入手机号数
    MAX_UPLOAD_FILES: int = int(os.getenv("MAX_UPLOAD_FILES", "100"))             # 合并/重命名最多上传文件数
    MAX_ZIP_SIZE: int = int(os.getenv("MAX_ZIP_SIZE_MB", "100")) * 1024 * 1024     # ZIP 文件大小上限
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
    VERSION: str = _clean_env(os.getenv("BOT_VERSION", "2026-10-28.12"))


settings = Settings()
