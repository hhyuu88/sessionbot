"""
管理员后台模块

功能：
1. /admin 命令 - 主面板（系统状态概览）
2. 管理员权限验证装饰器
3. 代购账号管理（登录、状态检查）
4. 余额管理（查询、手动充值）
5. 订单统计
6. 系统设置入口
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable, Optional

from telethon import TelegramClient, events, Button

import config
from buyer_account_manager import (
    BuyerAccountLoginManager,
    check_buyer_account_status,
    log_admin_action,
)
from balance_manager import (
    get_source_bot_balance,
    get_recharge_history,
    recharge_to_source_bot,
    calculate_recharge_amount,
)


# ========================
# 权限验证
# ========================

def admin_required(func: Callable) -> Callable:
    """
    管理员权限验证装饰器

    用法：
        @client.on(events.NewMessage(pattern='/admin'))
        @admin_required
        async def admin_command(event):
            ...
    """
    @wraps(func)
    async def wrapper(event, *args, **kwargs):
        sender_id = event.sender_id
        if not config.ADMIN_IDS:
            await event.respond("❌ 系统未配置管理员，请设置 ADMIN_IDS 环境变量")
            return
        if sender_id not in config.ADMIN_IDS:
            await event.respond("❌ 无权限访问，仅限管理员使用")
            return
        return await func(event, *args, **kwargs)
    return wrapper


# ========================
# 数据库查询辅助
# ========================

def _get_db():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_today_stats() -> dict:
    """获取今日订单统计"""
    conn = _get_db()
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')

    c.execute(
        '''SELECT COUNT(*) as count, COALESCE(SUM(profit), 0) as profit
           FROM orders
           WHERE DATE(created_at) = ? AND status = 'completed' ''',
        (today,)
    )
    row = c.fetchone()
    conn.close()
    return {
        'count': row['count'] if row else 0,
        'profit': row['profit'] if row else 0.0,
    }


def _get_total_stats() -> dict:
    """获取全部订单汇总"""
    conn = _get_db()
    c = conn.cursor()

    c.execute(
        '''SELECT
               COUNT(*) as total_orders,
               COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) as completed,
               COALESCE(SUM(CASE WHEN status='completed' THEN profit ELSE 0 END), 0) as total_profit
           FROM orders'''
    )
    row = c.fetchone()
    conn.close()
    return {
        'total_orders': row['total_orders'] if row else 0,
        'completed': row['completed'] if row else 0,
        'total_profit': row['total_profit'] if row else 0.0,
    }


def _get_product_stats() -> dict:
    """获取商品库存统计"""
    conn = _get_db()
    c = conn.cursor()

    c.execute(
        '''SELECT
               COUNT(*) as total,
               COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), 0) as active,
               COALESCE(SUM(CASE WHEN stock = 0 THEN 1 ELSE 0 END), 0) as out_of_stock,
               COALESCE(SUM(CASE WHEN stock > 0 AND stock <= ? THEN 1 ELSE 0 END), 0) as low_stock
           FROM products''',
        (config.LOW_STOCK_THRESHOLD,)
    )
    row = c.fetchone()
    conn.close()
    return {
        'total': row['total'] if row else 0,
        'active': row['active'] if row else 0,
        'out_of_stock': row['out_of_stock'] if row else 0,
        'low_stock': row['low_stock'] if row else 0,
    }


def _get_order_stats_period(days: int) -> dict:
    """获取指定天数的订单统计"""
    conn = _get_db()
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    c.execute(
        '''SELECT
               COUNT(*) as count,
               COALESCE(SUM(CASE WHEN status='completed' THEN profit ELSE 0 END), 0) as profit,
               COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) as completed,
               COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0) as failed
           FROM orders
           WHERE DATE(created_at) >= ?''',
        (since,)
    )
    row = c.fetchone()
    conn.close()
    return {
        'count': row['count'] if row else 0,
        'profit': row['profit'] if row else 0.0,
        'completed': row['completed'] if row else 0,
        'failed': row['failed'] if row else 0,
    }


# ========================
# 主面板文本生成
# ========================

async def _build_admin_panel_text(
    buyer_client: TelegramClient,
    source_bot: str,
) -> str:
    """构建 /admin 主面板文本"""
    today = _get_today_stats()
    products = _get_product_stats()

    # 查询余额（超时 8 秒）
    try:
        balance = await asyncio.wait_for(
            get_source_bot_balance(buyer_client, source_bot),
            timeout=8.0
        )
        balance_str = f'{balance:.2f} U' if balance is not None else '查询失败'
        balance_warning = f'\n⚠️ 余额不足预警！' if (
            balance is not None and balance < config.LOW_BALANCE_THRESHOLD
        ) else ''
    except asyncio.TimeoutError:
        balance_str = '查询超时'
        balance_warning = ''

    # 账号状态（超时 10 秒）
    try:
        status, reason = await asyncio.wait_for(
            check_buyer_account_status(buyer_client, source_bot),
            timeout=10.0
        )
        status_icon = '✅' if status == '正常' else '⚠️'
        status_str = f'{status_icon} {status}'
        if reason:
            status_str += f'（{reason[:30]}）'
    except asyncio.TimeoutError:
        status_str = '⏳ 检查超时'

    # 库存警告
    stock_warnings = []
    if products['out_of_stock'] > 0:
        stock_warnings.append(f'  • {products["out_of_stock"]} 个商品已售罄')
    if products['low_stock'] > 0:
        stock_warnings.append(f'  • {products["low_stock"]} 个商品库存不足')
    warnings_text = '\n' + '\n'.join(stock_warnings) if stock_warnings else ' 无'

    return (
        f"🎛️ **管理员控制台**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **今日数据**\n"
        f"  订单数：{today['count']}  利润：{today['profit']:.2f} U\n\n"
        f"💰 **代购账号余额**\n"
        f"  {balance_str}{balance_warning}\n\n"
        f"🤖 **代购账号状态**\n"
        f"  {status_str}\n\n"
        f"📦 **商品库存**\n"
        f"  在架：{products['active']}  总计：{products['total']}\n\n"
        f"⚠️ **系统警告**{warnings_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_更新时间: {datetime.now().strftime('%H:%M:%S')}_"
    )


# ========================
# AdminPanel 主类
# ========================

class AdminPanel:
    """
    管理员后台面板

    用法：
        panel = AdminPanel(bot_client, buyer_client, source_bot)
        await panel.register_handlers()
    """

    def __init__(
        self,
        bot_client: TelegramClient,
        buyer_client: TelegramClient,
        source_bot: str,
        notify_callback: Optional[Callable] = None,
    ):
        self.bot = bot_client
        self.buyer = buyer_client
        self.source_bot = source_bot
        self._notify = notify_callback or self._default_notify
        self._login_manager = BuyerAccountLoginManager(config.API_ID, config.API_HASH)
        # 等待手动充值金额输入的管理员集合
        self._awaiting_recharge: dict[int, bool] = {}

    async def _default_notify(self, msg: str):
        for admin_id in config.ADMIN_IDS:
            try:
                await self.bot.send_message(admin_id, msg, parse_mode='md')
            except Exception as e:
                print(f'[AdminPanel] 通知发送失败: {e}')

    # ------------------------------------------------------------------
    # 注册所有处理器
    # ------------------------------------------------------------------

    async def register_handlers(self):
        """注册 bot 事件处理器"""

        @self.bot.on(events.NewMessage(pattern='/admin'))
        @admin_required
        async def admin_main(event):
            await self._handle_admin_main(event)

        @self.bot.on(events.CallbackQuery(pattern=b'admin_'))
        async def admin_callback(event):
            await self._handle_callback(event)

        @self.bot.on(events.NewMessage)
        async def catch_all_messages(event):
            """捕获登录流程中的消息（仅处理管理员消息）"""
            admin_id = event.sender_id
            # 仅处理已配置的管理员消息，忽略普通用户
            if admin_id not in config.ADMIN_IDS:
                return

            # 优先处理登录流程
            if self._login_manager.is_in_login_flow(admin_id):
                await self._login_manager.handle_message(event, admin_id)
                return

            # 等待手动充值金额
            if self._awaiting_recharge.get(admin_id):
                await self._handle_manual_recharge_amount(event, admin_id)

    # ------------------------------------------------------------------
    # /admin 主面板
    # ------------------------------------------------------------------

    async def _handle_admin_main(self, event):
        admin_id = event.sender_id
        log_admin_action(admin_id, 'open_admin', '打开管理后台')

        text = await _build_admin_panel_text(self.buyer, self.source_bot)
        buttons = [
            [Button.inline('👤 代购账号管理', b'admin_accounts')],
            [Button.inline('🔍 账号状态检查', b'admin_check_status')],
            [Button.inline('💰 余额管理', b'admin_balance')],
            [Button.inline('📊 订单统计', b'admin_orders')],
            [Button.inline('⚙️ 系统设置', b'admin_settings')],
            [Button.inline('🔄 刷新', b'admin_refresh')],
        ]
        await event.respond(text, buttons=buttons, parse_mode='md')

    # ------------------------------------------------------------------
    # 回调路由
    # ------------------------------------------------------------------

    async def _handle_callback(self, event):
        sender_id = event.sender_id
        if sender_id not in config.ADMIN_IDS:
            await event.answer('❌ 无权限', alert=True)
            return

        data = event.data.decode()

        handlers = {
            'admin_refresh': self._cb_refresh,
            'admin_accounts': self._cb_accounts,
            'admin_login_new': self._cb_login_new,
            'admin_check_status': self._cb_check_status,
            'admin_balance': self._cb_balance,
            'admin_manual_recharge': self._cb_manual_recharge,
            'admin_orders': self._cb_orders,
            'admin_settings': self._cb_settings,
            'admin_back': self._cb_back_to_main,
        }

        handler = handlers.get(data)
        if handler:
            await handler(event)
        else:
            await event.answer('功能开发中...')

    # ------------------------------------------------------------------
    # 刷新主面板
    # ------------------------------------------------------------------

    async def _cb_refresh(self, event):
        text = await _build_admin_panel_text(self.buyer, self.source_bot)
        buttons = [
            [Button.inline('👤 代购账号管理', b'admin_accounts')],
            [Button.inline('🔍 账号状态检查', b'admin_check_status')],
            [Button.inline('💰 余额管理', b'admin_balance')],
            [Button.inline('📊 订单统计', b'admin_orders')],
            [Button.inline('⚙️ 系统设置', b'admin_settings')],
            [Button.inline('🔄 刷新', b'admin_refresh')],
        ]
        try:
            await event.edit(text, buttons=buttons, parse_mode='md')
        except Exception:
            pass
        await event.answer('已刷新')

    # ------------------------------------------------------------------
    # 代购账号管理
    # ------------------------------------------------------------------

    async def _cb_accounts(self, event):
        text = (
            "👤 **代购账号管理**\n\n"
            "当前 Session：`" + config.BUYER_ACCOUNT_SESSION + "`\n\n"
            "选择操作："
        )
        buttons = [
            [Button.inline('🔑 登录新代购账号', b'admin_login_new')],
            [Button.inline('🔍 检查当前账号状态', b'admin_check_status')],
            [Button.inline('« 返回主面板', b'admin_back')],
        ]
        await event.edit(text, buttons=buttons, parse_mode='md')

    async def _cb_login_new(self, event):
        admin_id = event.sender_id
        await event.answer()
        # 通知管理员切换到私聊进行登录操作
        await self._login_manager.start_login(event, admin_id)

    # ------------------------------------------------------------------
    # 账号状态检查
    # ------------------------------------------------------------------

    async def _cb_check_status(self, event):
        await event.answer('正在检查账号状态...')
        try:
            status, reason = await asyncio.wait_for(
                check_buyer_account_status(self.buyer, self.source_bot),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            status, reason = '检查超时', ''

        me = None
        try:
            me = await self.buyer.get_me()
        except Exception:
            pass

        phone = f'+{getattr(me, "phone", "未知")}' if me else '未知'
        username = getattr(me, 'username', '') or ''

        status_icon = {'正常': '✅', '受限': '⚠️', '检查超时': '⏳'}.get(status, '❌')

        text = (
            f"🔍 **代购账号状态**\n\n"
            f"手机号：{phone}\n"
            f"用户名：{'@' + username if username else '无'}\n"
            f"状态：{status_icon} {status}\n"
        )
        if reason:
            text += f"原因：{reason[:100]}\n"
        text += f"\n检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        if status not in ('正常', '检查超时'):
            text += (
                "\n\n⚠️ 账号状态异常，建议尽快更换代购账号！\n"
                "点击下方按钮登录新账号 →"
            )
        buttons = [
            [Button.inline('🔑 登录新代购账号', b'admin_login_new')],
            [Button.inline('« 返回', b'admin_accounts')],
        ]
        await event.edit(text, buttons=buttons, parse_mode='md')

    # ------------------------------------------------------------------
    # 余额管理
    # ------------------------------------------------------------------

    async def _cb_balance(self, event):
        await event.answer('正在查询余额...')

        try:
            balance = await asyncio.wait_for(
                get_source_bot_balance(self.buyer, self.source_bot),
                timeout=10.0
            )
            balance_str = f'{balance:.2f} U' if balance is not None else '查询失败'
        except asyncio.TimeoutError:
            balance = None
            balance_str = '查询超时'

        # 最近充值记录
        history = get_recharge_history(5)
        history_text = ''
        if history:
            history_text = '\n\n📋 **最近充值记录**\n'
            for r in history:
                status_icon = {'success': '✅', 'failed': '❌', 'pending': '⏳'}.get(
                    r.get('status', ''), '❓'
                )
                history_text += (
                    f"  {status_icon} {r.get('amount', 0):.2f} U  "
                    f"{r.get('created_at', '')[:16]}\n"
                )

        warning = ''
        if balance is not None and balance < config.LOW_BALANCE_THRESHOLD:
            warning = f'\n⚠️ 余额低于预警阈值（{config.LOW_BALANCE_THRESHOLD:.0f} U）！'

        text = (
            f"💰 **余额管理**\n\n"
            f"当前余额：**{balance_str}**{warning}\n"
            f"预警阈值：{config.LOW_BALANCE_THRESHOLD:.0f} U\n"
            f"自动充值：{'✅ 已启用' if config.AUTO_RECHARGE_ENABLED else '❌ 已禁用'}"
            f"{history_text}"
        )
        buttons = [
            [Button.inline('💳 手动充值', b'admin_manual_recharge')],
            [Button.inline('🔄 刷新余额', b'admin_balance')],
            [Button.inline('« 返回主面板', b'admin_back')],
        ]
        await event.edit(text, buttons=buttons, parse_mode='md')

    async def _cb_manual_recharge(self, event):
        admin_id = event.sender_id
        self._awaiting_recharge[admin_id] = True
        await event.answer()
        await event.respond(
            "💳 **手动充值**\n\n"
            "请输入充值金额（U），例如：`100`\n\n"
            "输入 /cancel 取消",
            parse_mode='md'
        )

    async def _handle_manual_recharge_amount(self, event, admin_id: int):
        text = event.raw_text.strip()

        if text.lower() in ('/cancel', 'cancel', '取消'):
            self._awaiting_recharge.pop(admin_id, None)
            await event.respond('已取消充值')
            return

        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError('金额必须大于 0')
        except ValueError:
            await event.respond('❌ 请输入有效的金额数字，例如：`100`', parse_mode='md')
            return

        self._awaiting_recharge.pop(admin_id, None)
        log_admin_action(admin_id, 'manual_recharge', f'手动充值 {amount} U')

        await event.respond(f'⏳ 正在发起充值请求：{amount:.2f} U...')

        success = await recharge_to_source_bot(
            self.buyer, self.source_bot, amount, self._notify
        )

        if success:
            await event.respond(f'✅ 充值成功：{amount:.2f} U')
        else:
            await event.respond(f'❌ 充值失败，请检查账号状态或手动充值')

    # ------------------------------------------------------------------
    # 订单统计
    # ------------------------------------------------------------------

    async def _cb_orders(self, event):
        today = _get_order_stats_period(1)
        week = _get_order_stats_period(7)
        month = _get_order_stats_period(30)
        total = _get_total_stats()

        def rate(s: dict) -> str:
            if s['count'] == 0:
                return 'N/A'
            return f"{s['completed'] / s['count'] * 100:.1f}%"

        text = (
            f"📊 **订单统计**\n\n"
            f"**今日**\n"
            f"  订单：{today['count']}  完成：{today['completed']}  "
            f"失败：{today['failed']}\n"
            f"  利润：{today['profit']:.2f} U  成功率：{rate(today)}\n\n"
            f"**本周（7天）**\n"
            f"  订单：{week['count']}  完成：{week['completed']}  "
            f"失败：{week['failed']}\n"
            f"  利润：{week['profit']:.2f} U  成功率：{rate(week)}\n\n"
            f"**本月（30天）**\n"
            f"  订单：{month['count']}  完成：{month['completed']}  "
            f"失败：{month['failed']}\n"
            f"  利润：{month['profit']:.2f} U  成功率：{rate(month)}\n\n"
            f"**累计**\n"
            f"  总订单：{total['total_orders']}  "
            f"完成：{total['completed']}\n"
            f"  总利润：{total['total_profit']:.2f} U"
        )
        buttons = [[Button.inline('« 返回主面板', b'admin_back')]]
        await event.edit(text, buttons=buttons, parse_mode='md')

    # ------------------------------------------------------------------
    # 系统设置
    # ------------------------------------------------------------------

    async def _cb_settings(self, event):
        text = (
            f"⚙️ **系统设置**\n\n"
            f"当前配置（修改请更新 .env 文件后重启）：\n\n"
            f"📈 加价策略\n"
            f"  百分比: {config.MARKUP_PERCENTAGE * 100:.0f}%\n"
            f"  固定: {config.MARKUP_FIXED:.2f} U\n\n"
            f"🔄 库存同步\n"
            f"  间隔: {config.SYNC_INTERVAL // 60} 分钟\n"
            f"  低库存阈值: {config.LOW_STOCK_THRESHOLD}\n\n"
            f"💰 余额管理\n"
            f"  预警阈值: {config.LOW_BALANCE_THRESHOLD:.0f} U\n"
            f"  自动充值: {'✅ 启用' if config.AUTO_RECHARGE_ENABLED else '❌ 禁用'}\n"
            f"  充值缓冲: {config.RECHARGE_BUFFER_AMOUNT:.0f} U\n\n"
            f"👤 账号监控\n"
            f"  检查间隔: {config.ACCOUNT_STATUS_CHECK_INTERVAL // 60} 分钟"
        )
        buttons = [[Button.inline('« 返回主面板', b'admin_back')]]
        await event.edit(text, buttons=buttons, parse_mode='md')

    # ------------------------------------------------------------------
    # 返回主面板
    # ------------------------------------------------------------------

    async def _cb_back_to_main(self, event):
        text = await _build_admin_panel_text(self.buyer, self.source_bot)
        buttons = [
            [Button.inline('👤 代购账号管理', b'admin_accounts')],
            [Button.inline('🔍 账号状态检查', b'admin_check_status')],
            [Button.inline('💰 余额管理', b'admin_balance')],
            [Button.inline('📊 订单统计', b'admin_orders')],
            [Button.inline('⚙️ 系统设置', b'admin_settings')],
            [Button.inline('🔄 刷新', b'admin_refresh')],
        ]
        await event.edit(text, buttons=buttons, parse_mode='md')
        await event.answer()
