"""
配置文件 - 通过 .env 文件或环境变量加载所有配置，避免硬编码敏感信息

优先级：环境变量 > .env 文件 > 内置默认值
"""

import os
from pathlib import Path

# 尝试加载 .env 文件（不强制要求存在）
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / '.env'
    load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass  # python-dotenv 未安装时跳过，继续从环境变量读取


def _require_env(name: str, default: str | None = None) -> str:
    """从环境变量读取配置，未设置时返回 default；若 default 也为 None 则抛出异常。"""
    value = os.environ.get(name, default)
    if value is None:
        raise EnvironmentError(
            f"必须通过环境变量或 .env 文件设置 {name}，当前未配置。"
            f" 请参考 .env.example 文件进行配置。"
        )
    return value


# ========================
# Telegram API 配置（必须设置）
# ========================
# 支持新变量名 API_ID 和旧变量名 TG_API_ID（向后兼容）
API_ID = os.environ.get('API_ID') or _require_env('TG_API_ID')
API_HASH = os.environ.get('API_HASH') or _require_env('TG_API_HASH')

# 机器人配置
YOUR_BOT_TOKEN = _require_env('BOT_TOKEN')
SOURCE_BOT_USERNAME = _require_env('SOURCE_BOT_USERNAME', '@source_shop_bot')
BUYER_ACCOUNT_SESSION = _require_env('BUYER_ACCOUNT_SESSION', 'buyer_account')

# ========================
# 管理员配置
# ========================
# 支持多个管理员 ID（逗号分隔），例如：123456789,987654321
_admin_ids_str = os.environ.get('ADMIN_IDS', os.environ.get('ADMIN_TELEGRAM_ID', ''))
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in _admin_ids_str.split(',') if x.strip().isdigit()
]
# 兼容旧版单管理员配置
ADMIN_TELEGRAM_ID: int | None = ADMIN_IDS[0] if ADMIN_IDS else None

# ========================
# 加价策略
# ========================
MARKUP_PERCENTAGE = float(os.environ.get('MARKUP_PERCENTAGE', '0.20'))
MARKUP_FIXED = float(os.environ.get('MARKUP_FIXED', '5'))

# ========================
# 库存管理配置
# ========================

# 库存同步间隔（秒），默认 30 分钟
SYNC_INTERVAL = int(os.environ.get('SYNC_INTERVAL', str(30 * 60)))

# 低库存预警阈值
LOW_STOCK_THRESHOLD = int(os.environ.get('LOW_STOCK_THRESHOLD', '5'))

# 库存锁定超时（秒），默认 15 分钟
LOCK_TIMEOUT = int(os.environ.get('LOCK_TIMEOUT', str(15 * 60)))

# 同步失败最大重试次数
MAX_SYNC_RETRIES = int(os.environ.get('MAX_SYNC_RETRIES', '3'))

# 数据库路径
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'shop_proxy.db')

# ========================
# 余额管理配置
# ========================

# 低余额预警阈值（U）
LOW_BALANCE_THRESHOLD = float(os.environ.get('LOW_BALANCE_THRESHOLD', '50'))

# 是否启用自动充值
AUTO_RECHARGE_ENABLED = os.environ.get('AUTO_RECHARGE_ENABLED', 'false').lower() in ('true', '1', 'yes')

# 自动充值缓冲金额（U），避免频繁充值
RECHARGE_BUFFER_AMOUNT = float(os.environ.get('RECHARGE_BUFFER_AMOUNT', '50'))

# 余额检查间隔（秒），默认 30 分钟
BALANCE_CHECK_INTERVAL = int(os.environ.get('BALANCE_CHECK_INTERVAL', str(30 * 60)))

# 代购账号状态检查间隔（秒），默认 1 小时
ACCOUNT_STATUS_CHECK_INTERVAL = int(os.environ.get('ACCOUNT_STATUS_CHECK_INTERVAL', str(60 * 60)))

# ========================
# 商品抓取配置
# ========================

# 抓取失败最大重试次数
SCRAPE_RETRY_COUNT = int(os.environ.get('SCRAPE_RETRY_COUNT', '3'))

# 按钮点击后等待响应的延迟（秒）
SCRAPE_DELAY = int(os.environ.get('SCRAPE_DELAY', '3'))

# 商品分类识别关键词（逗号分隔）
SCRAPE_CATEGORY_KEYWORDS: list[str] = [
    stripped
    for kw in os.environ.get('SCRAPE_CATEGORY_KEYWORDS', 'TG,协议,老号,session').split(',')
    if (stripped := kw.strip())
]
