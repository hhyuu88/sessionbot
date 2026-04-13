"""
代购账号管理模块

功能：
1. 交互式登录流程（手机号 → 验证码 → 密码/两步验证）
2. 账号状态检查（在线/封禁/受限）
3. 每小时自动状态监控
4. 封禁/异常账号自动通知管理员
5. 账号状态历史记录
"""

import asyncio
import sqlite3
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from telethon.tl.functions.account import GetAuthorizationsRequest

import config


# ========================
# 登录状态机
# ========================

class LoginState(Enum):
    IDLE = 0
    WAITING_PHONE = 1
    WAITING_CODE = 2
    WAITING_PASSWORD = 3
    WAITING_2FA = 4
    COMPLETED = 5
    CANCELLED = 6


# ========================
# 数据库操作
# ========================

def _get_db():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def upgrade_buyer_account_db():
    """创建账号管理相关数据库表"""
    conn = _get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS account_status_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account_phone   TEXT,
            status          TEXT,
            restriction_reason TEXT,
            checked_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id    INTEGER,
            action      TEXT,
            details     TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()


def log_admin_action(admin_id: int, action: str, details: str = ''):
    """记录管理员操作日志"""
    conn = _get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO admin_logs (admin_id, action, details) VALUES (?, ?, ?)',
        (admin_id, action, details)
    )
    conn.commit()
    conn.close()


def log_account_status(phone: str, status: str, reason: str = ''):
    """记录账号状态检查日志"""
    conn = _get_db()
    c = conn.cursor()
    c.execute(
        'INSERT INTO account_status_logs (account_phone, status, restriction_reason) VALUES (?, ?, ?)',
        (phone, status, reason or '')
    )
    conn.commit()
    conn.close()


# ========================
# 账号状态检查
# ========================

async def check_buyer_account_status(
    buyer_client: TelegramClient,
    source_bot: str,
) -> tuple[str, str]:
    """
    检查代购账号状态

    :return: (status, reason)
             status: "正常" / "受限" / "异常/封禁"
             reason: 限制原因或错误信息
    """
    try:
        me = await buyer_client.get_me()
        if me is None:
            return "异常/封禁", "无法获取账号信息"

        phone = getattr(me, 'phone', 'unknown')

        if getattr(me, 'restricted', False):
            reasons = getattr(me, 'restriction_reason', [])
            reason_text = '; '.join(
                getattr(r, 'reason', str(r)) for r in (reasons or [])
            )
            log_account_status(phone, '受限', reason_text)
            return "受限", reason_text

        # 测试是否能向源机器人发送消息
        await buyer_client.send_message(source_bot, '/start')
        log_account_status(phone, '正常', '')
        return "正常", ''

    except Exception as e:
        phone = 'unknown'
        try:
            me = await buyer_client.get_me()
            if me:
                phone = getattr(me, 'phone', 'unknown')
        except Exception:
            pass
        error_info = str(e)
        log_account_status(phone, '异常/封禁', error_info)
        return "异常/封禁", error_info


def _format_status_alert(phone: str, status: str, reason: str) -> str:
    """格式化账号状态异常通知"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return (
        f"⚠️ 代购账号状态异常！\n\n"
        f"账号: +{phone}\n"
        f"状态: {status}\n"
        f"原因: {reason or '未知'}\n"
        f"检测时间: {now}\n\n"
        f"请尽快更换代购账号！\n"
        f"使用 /admin → 代购账号管理 → 登录新账号"
    )


# ========================
# 账号状态监控循环
# ========================

async def run_account_monitor(
    buyer_client: TelegramClient,
    source_bot: str,
    notify_callback: Callable,
    interval: int | None = None,
):
    """
    后台循环：定期检查账号状态，异常时通知管理员

    :param buyer_client:     代购账号 Telethon 客户端
    :param source_bot:       源机器人用户名
    :param notify_callback:  async 回调，接收 (message: str)
    :param interval:         检查间隔（秒），默认使用 config.ACCOUNT_STATUS_CHECK_INTERVAL
    """
    check_interval = interval or config.ACCOUNT_STATUS_CHECK_INTERVAL
    print(f'🔍 账号状态监控已启动，检查间隔 {check_interval} 秒')

    while True:
        await asyncio.sleep(check_interval)
        try:
            status, reason = await check_buyer_account_status(buyer_client, source_bot)
            print(f'[账号监控] 状态: {status}' + (f'，原因: {reason}' if reason else ''))
            if status != '正常':
                me = None
                try:
                    me = await buyer_client.get_me()
                except Exception:
                    pass
                phone = getattr(me, 'phone', 'unknown') if me else 'unknown'
                await notify_callback(_format_status_alert(phone, status, reason))
        except Exception as e:
            print(f'[账号监控] 检查异常: {e}')


# ========================
# 交互式登录管理器
# ========================

class BuyerAccountLoginManager:
    """
    通过 Telegram 对话（FSM）引导管理员完成代购账号登录

    用法：
        manager = BuyerAccountLoginManager(api_id, api_hash)
        # 在 bot message handler 中：
        await manager.handle_message(event, admin_id)
    """

    # 每个管理员独立的登录会话
    _sessions: dict[int, dict] = {}
    # 登录超时（秒）
    LOGIN_TIMEOUT = 300

    def __init__(self, api_id: str | int, api_hash: str):
        self.api_id = int(api_id)
        self.api_hash = api_hash

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def is_in_login_flow(self, admin_id: int) -> bool:
        """判断管理员是否正处于登录流程中"""
        session = self._sessions.get(admin_id)
        return session is not None and session['state'] not in (
            LoginState.IDLE, LoginState.COMPLETED, LoginState.CANCELLED
        )

    async def start_login(self, event, admin_id: int):
        """启动登录流程，提示管理员输入手机号"""
        self._sessions[admin_id] = {
            'state': LoginState.WAITING_PHONE,
            'client': None,
            'phone': None,
            'phone_code_hash': None,
            'started_at': asyncio.get_event_loop().time(),
        }
        log_admin_action(admin_id, 'start_login', '开始登录代购账号')
        await event.respond(
            "📱 **登录代购账号**\n\n"
            "请输入代购账号的手机号（含国际区号）：\n"
            "格式示例：`+8613812345678`\n\n"
            "输入 /cancel 取消登录",
            parse_mode='md'
        )

    async def handle_message(self, event, admin_id: int) -> bool:
        """
        处理登录流程中的用户消息

        :return: True 表示消息已被登录流程处理，False 表示不属于登录流程
        """
        session = self._sessions.get(admin_id)
        if session is None or session['state'] in (
            LoginState.IDLE, LoginState.COMPLETED, LoginState.CANCELLED
        ):
            return False

        text = event.raw_text.strip()

        # 取消命令
        if text.lower() in ('/cancel', 'cancel', '取消'):
            await self._cancel_login(event, admin_id)
            return True

        # 超时检查
        elapsed = asyncio.get_event_loop().time() - session['started_at']
        if elapsed > self.LOGIN_TIMEOUT:
            await self._timeout_login(event, admin_id)
            return True

        state = session['state']

        if state == LoginState.WAITING_PHONE:
            await self._handle_phone(event, admin_id, text)
        elif state == LoginState.WAITING_CODE:
            await self._handle_code(event, admin_id, text)
        elif state == LoginState.WAITING_PASSWORD:
            await self._handle_password(event, admin_id, text)
        elif state == LoginState.WAITING_2FA:
            await self._handle_2fa(event, admin_id, text)

        return True

    # ------------------------------------------------------------------
    # 私有：各步骤处理
    # ------------------------------------------------------------------

    async def _handle_phone(self, event, admin_id: int, phone: str):
        session = self._sessions[admin_id]

        # 基本格式校验（剥离非数字字符后验证）
        digits_only = re.sub(r'[\s\-\(\)]', '', phone[1:])
        if not phone.startswith('+') or not digits_only.isdigit() or len(digits_only) < 7:
            await event.respond(
                "❌ 手机号格式不正确，请重新输入（例如：+8613812345678）"
            )
            return

        session['phone'] = phone

        # 创建新的 Telethon 客户端
        session_name = f"buyer_{phone.replace('+', '').replace(' ', '')}"
        client = TelegramClient(session_name, self.api_id, self.api_hash)
        session['client'] = client

        try:
            await client.connect()
            result = await client.send_code_request(phone)
            session['phone_code_hash'] = result.phone_code_hash
            session['state'] = LoginState.WAITING_CODE

            await event.respond(
                f"✅ 验证码已发送到 {phone}\n\n"
                f"请输入收到的验证码（不含空格）：\n\n"
                f"输入 /cancel 取消登录"
            )
        except PhoneNumberInvalidError:
            await event.respond("❌ 手机号无效，请检查后重新输入")
            session['state'] = LoginState.WAITING_PHONE
        except FloodWaitError as e:
            await event.respond(f"⏳ 发送频率限制，请 {e.seconds} 秒后重试")
            await self._cleanup_session(admin_id)
        except Exception as e:
            await event.respond(f"❌ 发送验证码失败：{e}\n\n请重新输入手机号")
            session['state'] = LoginState.WAITING_PHONE

    async def _handle_code(self, event, admin_id: int, code: str):
        session = self._sessions[admin_id]
        client: TelegramClient = session['client']
        phone: str = session['phone']

        # 过滤空格
        code = code.replace(' ', '').replace('-', '')

        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=session['phone_code_hash'],
            )
            await self._login_success(event, admin_id)

        except SessionPasswordNeededError:
            session['state'] = LoginState.WAITING_2FA
            await event.respond(
                "🔐 该账号已开启两步验证\n\n"
                "请输入您的两步验证密码：\n\n"
                "输入 /cancel 取消登录"
            )

        except PhoneCodeInvalidError:
            await event.respond("❌ 验证码错误，请重新输入验证码：")

        except PhoneCodeExpiredError:
            await event.respond("⌛ 验证码已过期，请重新发起登录")
            await self._cleanup_session(admin_id)

        except Exception as e:
            await event.respond(f"❌ 验证失败：{e}")
            await self._cleanup_session(admin_id)

    async def _handle_password(self, event, admin_id: int, password: str):
        """处理密码（目前与 2FA 合并，保留扩展接口）"""
        await self._handle_2fa(event, admin_id, password)

    async def _handle_2fa(self, event, admin_id: int, password: str):
        session = self._sessions[admin_id]
        client: TelegramClient = session['client']

        try:
            await client.sign_in(password=password)
            await self._login_success(event, admin_id)

        except PasswordHashInvalidError:
            await event.respond("❌ 两步验证密码错误，请重新输入：")

        except Exception as e:
            await event.respond(f"❌ 验证失败：{e}")
            await self._cleanup_session(admin_id)

    async def _login_success(self, event, admin_id: int):
        session = self._sessions[admin_id]
        client: TelegramClient = session['client']
        phone: str = session['phone']

        me = await client.get_me()
        display_name = (
            f"{getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}".strip()
            or getattr(me, 'username', phone)
        )

        session['state'] = LoginState.COMPLETED
        log_admin_action(admin_id, 'login_success', f'成功登录账号 {phone}')

        await event.respond(
            f"✅ **代购账号登录成功！**\n\n"
            f"账号：{display_name}\n"
            f"手机：+{getattr(me, 'phone', phone)}\n\n"
            f"Session 已保存，账号即将进行状态检查...",
            parse_mode='md'
        )

        # 不断开连接，caller 可以使用这个 client
        self._sessions[admin_id]['logged_in_client'] = client

    async def _cancel_login(self, event, admin_id: int):
        log_admin_action(admin_id, 'cancel_login', '取消登录代购账号')
        await self._cleanup_session(admin_id)
        await event.respond("🚫 登录已取消")

    async def _timeout_login(self, event, admin_id: int):
        log_admin_action(admin_id, 'timeout_login', '登录超时')
        await self._cleanup_session(admin_id)
        await event.respond("⌛ 登录超时，请重新开始")

    async def _cleanup_session(self, admin_id: int):
        session = self._sessions.pop(admin_id, None)
        if session:
            client = session.get('client')
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    pass

    def get_logged_in_client(self, admin_id: int) -> Optional[TelegramClient]:
        """获取已登录成功的客户端（用于后续操作）"""
        session = self._sessions.get(admin_id)
        if session and session['state'] == LoginState.COMPLETED:
            return session.get('logged_in_client')
        return None
