"""
配置文件 - 支持环境变量覆盖，避免硬编码敏感信息
"""

import os


def _require_env(name: str, default: str | None = None) -> str:
    """从环境变量读取配置，未设置时返回 default；若 default 也为 None 则抛出异常。"""
    value = os.environ.get(name, default)
    if value is None:
        raise EnvironmentError(
            f"必须通过环境变量 {name} 提供配置，当前未设置。"
        )
    return value


# ========================
# Telegram API 配置（必须通过环境变量设置）
# ========================
API_ID = _require_env('TG_API_ID')
API_HASH = _require_env('TG_API_HASH')

# 机器人配置
YOUR_BOT_TOKEN = _require_env('BOT_TOKEN')
SOURCE_BOT_USERNAME = _require_env('SOURCE_BOT_USERNAME', '@source_shop_bot')
BUYER_ACCOUNT_SESSION = _require_env('BUYER_ACCOUNT_SESSION', 'buyer_account')

# 管理员 Telegram ID（接收库存预警通知），未设置则不发送通知
_admin_id_str = os.environ.get('ADMIN_TELEGRAM_ID')
ADMIN_TELEGRAM_ID: int | None = int(_admin_id_str) if _admin_id_str else None

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
