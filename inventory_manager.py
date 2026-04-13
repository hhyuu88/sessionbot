"""
库存管理核心模块

功能：
1. 实时库存检查
2. 库存锁定 / 释放（超卖保护）
3. 自动上架 / 下架
4. 库存变更日志
5. 低库存 / 异常预警
"""

import sqlite3
import asyncio
from datetime import datetime, timedelta
from contextlib import contextmanager

import config


# ========================
# 辅助：数据库上下文管理器
# ========================

@contextmanager
def get_db():
    """获取数据库连接，使用上下文管理器自动关闭"""
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ========================
# 数据库 Schema 升级
# ========================

def upgrade_database():
    """在原有 schema 基础上添加库存管理所需的字段和表"""
    with get_db() as conn:
        c = conn.cursor()

        # 1. 为 products 添加 status 字段（如果不存在）
        c.execute("PRAGMA table_info(products)")
        existing_columns = {row['name'] for row in c.fetchall()}

        if 'status' not in existing_columns:
            c.execute(
                "ALTER TABLE products ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )

        # 2. 库存锁定表
        c.execute('''
            CREATE TABLE IF NOT EXISTS inventory_locks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL,
                order_id    INTEGER NOT NULL,
                quantity    INTEGER NOT NULL DEFAULT 1,
                locked_at   TIMESTAMP NOT NULL,
                expires_at  TIMESTAMP NOT NULL,
                released    INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (product_id) REFERENCES products (id)
            )
        ''')

        # 3. 库存变更日志表
        c.execute('''
            CREATE TABLE IF NOT EXISTS inventory_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL,
                change_type TEXT NOT NULL,   -- sync / lock / release / manual
                delta       INTEGER NOT NULL, -- positive = 增加, negative = 减少
                stock_after INTEGER NOT NULL,
                order_id    INTEGER,
                note        TEXT,
                created_at  TIMESTAMP NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products (id)
            )
        ''')

        # 4. 同步记录表
        c.execute('''
            CREATE TABLE IF NOT EXISTS sync_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at      TIMESTAMP NOT NULL,
                finished_at     TIMESTAMP,
                products_total  INTEGER NOT NULL DEFAULT 0,
                products_updated INTEGER NOT NULL DEFAULT 0,
                products_unchanged INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'running',  -- running / success / failed
                error_message   TEXT
            )
        ''')

        # 5. 为 products 添加 locked_stock 字段（如果不存在）
        c.execute("PRAGMA table_info(products)")
        existing_columns = {row['name'] for row in c.fetchall()}
        if 'locked_stock' not in existing_columns:
            c.execute(
                "ALTER TABLE products ADD COLUMN locked_stock INTEGER NOT NULL DEFAULT 0"
            )


# ========================
# InventoryManager 类
# ========================

class InventoryManager:
    """
    库存管理器

    职责：
    - 查询可用库存（stock - locked_stock）
    - 锁定库存（创建订单时）
    - 释放库存（超时 / 失败时）
    - 扣减库存（订单完成时）
    - 更新库存（同步时）
    - 商品上架 / 下架
    - 预警通知
    """

    def __init__(self, notify_callback=None):
        """
        :param notify_callback: async callable(message: str) — 发送管理员通知
        """
        self._notify = notify_callback

    # ------------------------------------------------------------------
    # 库存查询
    # ------------------------------------------------------------------

    def get_available_stock(self, product_id: int) -> int:
        """返回可用库存（总库存 - 已锁定库存）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                'SELECT stock, locked_stock FROM products WHERE id = ?',
                (product_id,)
            )
            row = c.fetchone()
            if not row:
                return 0
            return max(0, row['stock'] - row['locked_stock'])

    def get_product_status(self, product_id: int) -> str | None:
        """返回商品 status 字段（'active' / 'inactive'）"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT status FROM products WHERE id = ?', (product_id,))
            row = c.fetchone()
            return row['status'] if row else None

    # ------------------------------------------------------------------
    # 库存锁定 / 释放
    # ------------------------------------------------------------------

    def lock_stock(self, product_id: int, order_id: int, quantity: int = 1) -> bool:
        """
        锁定库存（下单时调用）

        :return: True 表示锁定成功，False 表示库存不足
        """
        with get_db() as conn:
            c = conn.cursor()

            # 使用 UPDATE ... WHERE 原子地减少可用库存
            c.execute(
                '''
                UPDATE products
                SET locked_stock = locked_stock + ?
                WHERE id = ?
                  AND (stock - locked_stock) >= ?
                  AND status = 'active'
                ''',
                (quantity, product_id, quantity)
            )

            if c.rowcount == 0:
                return False  # 库存不足或商品下架

            # 记录锁定
            now = datetime.now()
            expires_at = now + timedelta(seconds=config.LOCK_TIMEOUT)
            c.execute(
                '''
                INSERT INTO inventory_locks
                    (product_id, order_id, quantity, locked_at, expires_at, released)
                VALUES (?, ?, ?, ?, ?, 0)
                ''',
                (product_id, order_id, quantity, now, expires_at)
            )

            # 写日志
            c.execute('SELECT stock, locked_stock FROM products WHERE id = ?', (product_id,))
            row = c.fetchone()
            self._write_log(
                c, product_id, 'lock', -quantity,
                row['stock'] - row['locked_stock'], order_id,
                f'锁定库存，订单 {order_id}'
            )

            return True

    def release_lock(self, order_id: int, product_id: int | None = None) -> bool:
        """
        释放库存锁定（支付超时 / 代购失败时调用）

        :param order_id:   订单 ID
        :param product_id: 可选，指定商品 ID（不指定则自动查询）
        :return: True 表示有锁被释放
        """
        with get_db() as conn:
            c = conn.cursor()

            # 查询未释放的锁
            if product_id is not None:
                c.execute(
                    '''
                    SELECT id, product_id, quantity FROM inventory_locks
                    WHERE order_id = ? AND product_id = ? AND released = 0
                    ''',
                    (order_id, product_id)
                )
            else:
                c.execute(
                    '''
                    SELECT id, product_id, quantity FROM inventory_locks
                    WHERE order_id = ? AND released = 0
                    ''',
                    (order_id,)
                )

            locks = c.fetchall()
            if not locks:
                return False

            for lock in locks:
                c.execute(
                    'UPDATE inventory_locks SET released = 1 WHERE id = ?',
                    (lock['id'],)
                )
                c.execute(
                    'UPDATE products SET locked_stock = MAX(0, locked_stock - ?) WHERE id = ?',
                    (lock['quantity'], lock['product_id'])
                )
                # 写日志
                c.execute('SELECT stock, locked_stock FROM products WHERE id = ?', (lock['product_id'],))
                row = c.fetchone()
                self._write_log(
                    c, lock['product_id'], 'release', lock['quantity'],
                    row['stock'] - row['locked_stock'], order_id,
                    f'释放库存，订单 {order_id}'
                )

            return True

    def confirm_purchase(self, order_id: int, product_id: int, quantity: int = 1) -> bool:
        """
        订单完成，从库存中永久扣减（并释放对应锁）

        :return: True 表示成功
        """
        with get_db() as conn:
            c = conn.cursor()

            # 释放锁定
            c.execute(
                '''
                SELECT id, quantity FROM inventory_locks
                WHERE order_id = ? AND product_id = ? AND released = 0
                ''',
                (order_id, product_id)
            )
            lock = c.fetchone()
            locked_qty = lock['quantity'] if lock else quantity

            if lock:
                c.execute(
                    'UPDATE inventory_locks SET released = 1 WHERE id = ?',
                    (lock['id'],)
                )

            # 实际扣减 stock 并还原 locked_stock
            c.execute(
                '''
                UPDATE products
                SET stock        = MAX(0, stock - ?),
                    locked_stock = MAX(0, locked_stock - ?)
                WHERE id = ?
                ''',
                (locked_qty, locked_qty, product_id)
            )

            # 检查是否需要自动下架
            c.execute('SELECT stock FROM products WHERE id = ?', (product_id,))
            row = c.fetchone()
            if row and row['stock'] == 0:
                self._set_status(c, product_id, 'inactive', '库存归零，自动下架')

            # 写日志
            c.execute('SELECT stock, locked_stock FROM products WHERE id = ?', (product_id,))
            row = c.fetchone()
            self._write_log(
                c, product_id, 'purchase', -locked_qty,
                row['stock'] - row['locked_stock'] if row else 0,
                order_id, f'订单 {order_id} 完成，扣减库存'
            )

            return True

    # ------------------------------------------------------------------
    # 库存更新（同步时调用）
    # ------------------------------------------------------------------

    def update_stock(self, product_id: int, new_stock: int, note: str = '同步更新') -> dict:
        """
        更新商品库存，自动处理上下架逻辑

        :return: {'changed': bool, 'old_stock': int, 'new_stock': int, 'status_changed': bool}
        """
        with get_db() as conn:
            c = conn.cursor()

            c.execute('SELECT stock, status FROM products WHERE id = ?', (product_id,))
            row = c.fetchone()
            if not row:
                return {'changed': False, 'old_stock': 0, 'new_stock': new_stock,
                        'status_changed': False}

            old_stock = row['stock']
            old_status = row['status']

            if old_stock == new_stock:
                return {'changed': False, 'old_stock': old_stock, 'new_stock': new_stock,
                        'status_changed': False}

            # 更新库存
            c.execute(
                'UPDATE products SET stock = ?, last_updated = ? WHERE id = ?',
                (new_stock, datetime.now(), product_id)
            )

            delta = new_stock - old_stock
            # 写日志
            self._write_log(c, product_id, 'sync', delta, new_stock, None, note)

            # 自动上下架
            status_changed = False
            if new_stock == 0 and old_status == 'active':
                self._set_status(c, product_id, 'inactive', '库存归零，自动下架')
                status_changed = True
            elif new_stock > 0 and old_status == 'inactive':
                self._set_status(c, product_id, 'active', '库存恢复，自动上架')
                status_changed = True

            return {
                'changed': True,
                'old_stock': old_stock,
                'new_stock': new_stock,
                'status_changed': status_changed,
            }

    # ------------------------------------------------------------------
    # 上架 / 下架
    # ------------------------------------------------------------------

    def set_product_active(self, product_id: int, active: bool) -> None:
        """手动设置商品上下架状态"""
        with get_db() as conn:
            c = conn.cursor()
            status = 'active' if active else 'inactive'
            note = '手动上架' if active else '手动下架'
            self._set_status(c, product_id, status, note)

    # ------------------------------------------------------------------
    # 超时锁释放（后台定时任务调用）
    # ------------------------------------------------------------------

    async def release_expired_locks(self) -> int:
        """
        释放所有超时的库存锁

        :return: 释放的锁数量
        """
        released_count = 0
        with get_db() as conn:
            c = conn.cursor()
            now = datetime.now()
            c.execute(
                '''
                SELECT id, product_id, order_id, quantity FROM inventory_locks
                WHERE released = 0 AND expires_at < ?
                ''',
                (now,)
            )
            expired = c.fetchall()

            for lock in expired:
                c.execute(
                    'UPDATE inventory_locks SET released = 1 WHERE id = ?',
                    (lock['id'],)
                )
                c.execute(
                    'UPDATE products SET locked_stock = MAX(0, locked_stock - ?) WHERE id = ?',
                    (lock['quantity'], lock['product_id'])
                )
                c.execute(
                    'SELECT stock, locked_stock FROM products WHERE id = ?',
                    (lock['product_id'],)
                )
                row = c.fetchone()
                self._write_log(
                    c, lock['product_id'], 'release', lock['quantity'],
                    row['stock'] - row['locked_stock'] if row else 0,
                    lock['order_id'],
                    f'支付超时，自动释放锁定（订单 {lock["order_id"]}）'
                )
                released_count += 1

        if released_count > 0 and self._notify:
            await self._notify(
                f'⏰ 已自动释放 {released_count} 个超时库存锁定'
            )

        return released_count

    # ------------------------------------------------------------------
    # 预警检查
    # ------------------------------------------------------------------

    async def check_alerts(self) -> None:
        """
        检查低库存和库存异常，向管理员发送通知
        """
        if not self._notify:
            return

        alerts = []

        with get_db() as conn:
            c = conn.cursor()

            # 低库存预警
            c.execute(
                '''
                SELECT id, name, stock FROM products
                WHERE status = 'active'
                  AND stock > 0
                  AND stock <= ?
                ''',
                (config.LOW_STOCK_THRESHOLD,)
            )
            low_stock = c.fetchall()
            for row in low_stock:
                alerts.append(
                    f'⚠️ 低库存预警：{row["name"]}（ID {row["id"]}）'
                    f' 剩余 {row["stock"]} 件，已低于阈值 {config.LOW_STOCK_THRESHOLD}'
                )

            # 突然清零预警（近 10 分钟内从非零变为零）
            ten_minutes_ago = datetime.now() - timedelta(minutes=10)
            c.execute(
                '''
                SELECT DISTINCT il.product_id, p.name
                FROM inventory_log il
                JOIN products p ON il.product_id = p.id
                WHERE il.stock_after = 0
                  AND il.change_type = 'sync'
                  AND il.created_at >= ?
                ''',
                (ten_minutes_ago,)
            )
            sudden_zero = c.fetchall()
            for row in sudden_zero:
                alerts.append(
                    f'🚨 库存异常：{row["name"]}（ID {row["product_id"]}）'
                    f' 库存突然清零，请检查！'
                )

        for alert in alerts:
            await self._notify(alert)

    # ------------------------------------------------------------------
    # 库存快照
    # ------------------------------------------------------------------

    def take_snapshot(self) -> list[dict]:
        """返回当前所有商品的库存快照"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                '''
                SELECT id, name, stock, locked_stock, status, last_updated
                FROM products
                ORDER BY id
                '''
            )
            rows = c.fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _set_status(cursor, product_id: int, status: str, note: str) -> None:
        cursor.execute(
            'UPDATE products SET status = ? WHERE id = ?',
            (status, product_id)
        )
        cursor.execute(
            '''
            INSERT INTO inventory_log
                (product_id, change_type, delta, stock_after, note, created_at)
            SELECT id, 'manual', 0, stock, ?, ?
            FROM products WHERE id = ?
            ''',
            (note, datetime.now(), product_id)
        )

    @staticmethod
    def _write_log(cursor, product_id: int, change_type: str,
                   delta: int, stock_after: int,
                   order_id: int | None, note: str) -> None:
        cursor.execute(
            '''
            INSERT INTO inventory_log
                (product_id, change_type, delta, stock_after, order_id, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (product_id, change_type, delta, stock_after, order_id, note, datetime.now())
        )
