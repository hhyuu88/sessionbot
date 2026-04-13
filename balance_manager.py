"""
余额管理与自动充值模块

功能：
1. 查询代购账号在源机器人的余额
2. 余额不足自动充值（或通知管理员）
3. 充值记录数据库存储
4. 余额低于阈值预警
5. 余额监控循环
"""

import asyncio
import re
import sqlite3
from datetime import datetime
from typing import Callable, Optional

from telethon import TelegramClient, events

import config


# ========================
# 数据库操作
# ========================

def _get_db():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def upgrade_balance_db():
    """创建余额管理相关数据库表"""
    conn = _get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS recharge_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            amount          REAL,
            balance_before  REAL,
            balance_after   REAL,
            status          TEXT DEFAULT 'pending',
            payment_method  TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            confirmed_at    TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()


def _create_recharge_record(amount: float, balance_before: float, method: str = '') -> int:
    """创建充值记录，返回记录 ID"""
    conn = _get_db()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO recharge_records (amount, balance_before, status, payment_method)
           VALUES (?, ?, 'pending', ?)''',
        (amount, balance_before, method)
    )
    record_id = c.lastrowid
    conn.commit()
    conn.close()
    return record_id


def _update_recharge_record(record_id: int, status: str, balance_after: float = 0.0):
    """更新充值记录状态"""
    conn = _get_db()
    c = conn.cursor()
    c.execute(
        '''UPDATE recharge_records
           SET status = ?, balance_after = ?, confirmed_at = ?
           WHERE id = ?''',
        (status, balance_after, datetime.now(), record_id)
    )
    conn.commit()
    conn.close()


def get_recharge_history(limit: int = 10) -> list[dict]:
    """获取最近的充值记录"""
    conn = _get_db()
    c = conn.cursor()
    c.execute(
        'SELECT * FROM recharge_records ORDER BY created_at DESC LIMIT ?',
        (limit,)
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ========================
# 余额查询
# ========================

async def get_source_bot_balance(
    buyer_client: TelegramClient,
    source_bot: str,
    timeout: float = 15.0,
) -> Optional[float]:
    """
    向源机器人发送余额查询命令并解析响应

    :return: 余额（U），解析失败返回 None
    """
    future: asyncio.Future = asyncio.get_event_loop().create_future()

    @buyer_client.on(events.NewMessage(from_users=source_bot))
    async def _balance_handler(event):
        if not future.done():
            future.set_result(event.raw_text)

    try:
        await buyer_client.send_message(source_bot, '/balance')
        raw_text = await asyncio.wait_for(future, timeout=timeout)
        balance = _parse_balance_from_text(raw_text)
        return balance
    except asyncio.TimeoutError:
        print('[余额查询] 等待源机器人响应超时')
        return None
    except Exception as e:
        print(f'[余额查询] 异常: {e}')
        return None
    finally:
        buyer_client.remove_event_handler(_balance_handler)


def _parse_balance_from_text(text: str) -> Optional[float]:
    """
    从源机器人响应消息中解析余额金额

    常见格式（模糊匹配，适配不同源机器人）：
      - "余额：50.00 U"
      - "Balance: 50.00U"
      - "当前余额 50U"
      - "balance: 50"
    """
    patterns = [
        r'余额[：:]\s*([\d.]+)',
        r'[Bb]alance[：:\s]+([\d.]+)',
        r'当前余额\s*([\d.]+)',
        r'([\d.]+)\s*[Uu]$',
        r'([\d.]+)\s*USDT',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    # 兜底：寻找第一个浮点数或整数
    m = re.search(r'([\d]+(?:\.[\d]+)?)', text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# ========================
# 充值金额计算
# ========================

def calculate_recharge_amount(required: float, current: float) -> float:
    """
    智能计算充值金额

    策略：充值缺口 + 缓冲金额（避免频繁充值）
    """
    deficit = max(required - current, 0)
    return deficit + config.RECHARGE_BUFFER_AMOUNT


# ========================
# 充值执行
# ========================

async def recharge_to_source_bot(
    buyer_client: TelegramClient,
    source_bot: str,
    amount: float,
    notify_callback: Callable,
    timeout: float = 300.0,
) -> bool:
    """
    向源机器人发起充值请求

    流程：
      1. 发送充值命令
      2. 等待源机器人返回支付信息
      3. 如是加密货币地址 → 通知管理员；如是二维码 → 通知管理员扫码
      4. 等待充值到账确认（余额变动）

    :return: True 表示充值成功
    """
    # 查询当前余额用于记录
    balance_before = await get_source_bot_balance(buyer_client, source_bot) or 0.0
    record_id = _create_recharge_record(amount, balance_before, 'pending')

    future: asyncio.Future = asyncio.get_event_loop().create_future()

    @buyer_client.on(events.NewMessage(from_users=source_bot))
    async def _payment_handler(event):
        if not future.done():
            future.set_result(event.raw_text)

    try:
        await buyer_client.send_message(source_bot, f'/recharge {amount:.2f}')
        payment_text = await asyncio.wait_for(future, timeout=30.0)

        # 判断支付类型并通知管理员
        payment_info = _extract_payment_info(payment_text)
        if payment_info:
            msg = (
                f"💳 **充值请求已发起**\n\n"
                f"充值金额: {amount:.2f} U\n"
                f"支付方式: {payment_info.get('type', '未知')}\n"
                f"地址/信息:\n{payment_info.get('address', payment_text[:200])}\n\n"
                f"请完成支付后系统将自动确认"
            )
        else:
            msg = (
                f"💳 **充值请求已发起**\n\n"
                f"充值金额: {amount:.2f} U\n"
                f"源机器人响应:\n{payment_text[:300]}\n\n"
                f"请根据提示完成充值"
            )
        await notify_callback(msg)

        # 等待余额变动确认
        success = await _wait_for_recharge_confirmation(
            buyer_client, source_bot, amount, timeout
        )

        if success:
            new_balance = await get_source_bot_balance(buyer_client, source_bot)
            _update_recharge_record(record_id, 'success', new_balance or 0.0)
            return True
        else:
            _update_recharge_record(record_id, 'failed', 0.0)
            return False

    except asyncio.TimeoutError:
        _update_recharge_record(record_id, 'failed', 0.0)
        await notify_callback(
            f"❌ **充值超时**\n充值金额: {amount:.2f} U\n请手动检查充值状态"
        )
        return False
    except Exception as e:
        _update_recharge_record(record_id, 'failed', 0.0)
        await notify_callback(
            f"❌ **充值异常**\n充值金额: {amount:.2f} U\n错误: {e}"
        )
        return False
    finally:
        buyer_client.remove_event_handler(_payment_handler)


def _extract_payment_info(text: str) -> Optional[dict]:
    """从源机器人消息中提取支付信息"""
    # 以太坊/USDT TRC20/ERC20 地址
    crypto_patterns = [
        r'T[A-Za-z0-9]{33}',       # TRC20
        r'0x[a-fA-F0-9]{40}',      # ERC20
        r'[13][a-km-zA-HJ-NP-Z1-9]{25,34}',  # BTC
    ]
    for pat in crypto_patterns:
        m = re.search(pat, text)
        if m:
            return {'type': 'crypto', 'address': m.group(0)}

    # 二维码链接
    qr_match = re.search(r'https?://\S+', text)
    if qr_match:
        return {'type': 'qrcode', 'address': qr_match.group(0)}

    return None


# 充值到账确认容差（允许 5% 的误差，例如手续费）
_RECHARGE_CONFIRMATION_TOLERANCE = 0.95


async def _wait_for_recharge_confirmation(
    buyer_client: TelegramClient,
    source_bot: str,
    expected_amount: float,
    timeout: float,
) -> bool:
    """等待充值到账确认（轮询余额变化）"""
    initial_balance = await get_source_bot_balance(buyer_client, source_bot) or 0.0
    end_time = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < end_time:
        await asyncio.sleep(30)
        new_balance = await get_source_bot_balance(buyer_client, source_bot)
        if new_balance is not None and new_balance >= initial_balance + expected_amount * _RECHARGE_CONFIRMATION_TOLERANCE:
            return True

    return False


# ========================
# 检查余额并自动充值
# ========================

async def check_and_auto_recharge(
    buyer_client: TelegramClient,
    source_bot: str,
    required_amount: float,
    notify_callback: Callable,
) -> bool:
    """
    检查余额，不足时按配置决定是否自动充值

    :param required_amount: 本次操作所需金额
    :return: True 表示余额充足（或充值成功）
    """
    current_balance = await get_source_bot_balance(buyer_client, source_bot)

    if current_balance is None:
        await notify_callback(
            f"⚠️ **无法查询代购账号余额**\n"
            f"本次操作需要 {required_amount:.2f} U，请手动检查账号状态"
        )
        return False

    if current_balance >= required_amount:
        return True

    # 余额不足
    recharge_amount = calculate_recharge_amount(required_amount, current_balance)
    shortage = required_amount - current_balance

    if not config.AUTO_RECHARGE_ENABLED:
        await notify_callback(
            f"⚠️ **代购账号余额不足！**\n\n"
            f"当前余额: {current_balance:.2f} U\n"
            f"需要金额: {required_amount:.2f} U\n"
            f"缺口: {shortage:.2f} U\n\n"
            f"自动充值已禁用，请手动充值 {recharge_amount:.2f} U\n"
            f"（含 {config.RECHARGE_BUFFER_AMOUNT:.0f} U 缓冲）"
        )
        return False

    # 执行自动充值
    await notify_callback(
        f"💰 **余额不足，正在自动充值...**\n\n"
        f"当前余额: {current_balance:.2f} U\n"
        f"需要金额: {required_amount:.2f} U\n"
        f"充值金额: {recharge_amount:.2f} U"
    )

    success = await recharge_to_source_bot(
        buyer_client, source_bot, recharge_amount, notify_callback
    )

    if success:
        await notify_callback(
            f"✅ **自动充值成功！**\n充值金额: {recharge_amount:.2f} U"
        )
    else:
        await notify_callback(
            f"❌ **自动充值失败！**\n请手动充值后继续操作"
        )

    return success


# ========================
# 余额监控循环
# ========================

async def run_balance_monitor(
    buyer_client: TelegramClient,
    source_bot: str,
    notify_callback: Callable,
    interval: int | None = None,
):
    """
    后台循环：定期检查余额，低余额时通知管理员

    :param interval: 检查间隔（秒），默认使用 config.BALANCE_CHECK_INTERVAL
    """
    check_interval = interval or config.BALANCE_CHECK_INTERVAL
    print(f'💰 余额监控已启动，检查间隔 {check_interval} 秒')

    while True:
        await asyncio.sleep(check_interval)
        try:
            balance = await get_source_bot_balance(buyer_client, source_bot)
            if balance is None:
                print('[余额监控] 无法获取余额')
                continue

            print(f'[余额监控] 当前余额: {balance:.2f} U')

            if balance < config.LOW_BALANCE_THRESHOLD:
                await notify_callback(
                    f"⚠️ **代购账号余额不足！**\n\n"
                    f"当前余额: {balance:.2f} U\n"
                    f"预警阈值: {config.LOW_BALANCE_THRESHOLD:.2f} U\n\n"
                    f"建议尽快充值，避免影响代购流程"
                )
        except Exception as e:
            print(f'[余额监控] 检查异常: {e}')
