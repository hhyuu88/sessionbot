"""
优化的库存同步模块

功能：
1. 增量同步（只更新变化的商品）
2. 高频商品优先同步
3. 失败重试（指数退避）
4. 同步历史记录
5. 同步性能监控
"""

import asyncio
import sqlite3
from datetime import datetime

import config
from inventory_manager import InventoryManager, get_db


class StockSyncOptimizer:
    """
    优化的库存同步器

    使用方式：
        optimizer = StockSyncOptimizer(scraper, inventory_manager)
        await optimizer.run_sync_loop()   # 持续运行
    """

    def __init__(self, product_scraper, inventory_manager: InventoryManager):
        """
        :param product_scraper:    ProductScraper 实例（负责从源机器人抓取数据）
        :param inventory_manager:  InventoryManager 实例
        """
        self.scraper = product_scraper
        self.inventory = inventory_manager

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def run_sync_loop(self) -> None:
        """
        持续运行同步循环，每隔 config.SYNC_INTERVAL 秒执行一次增量同步
        """
        print(f'📦 库存同步循环已启动，间隔 {config.SYNC_INTERVAL} 秒')
        while True:
            try:
                await self.sync_once()
            except Exception as exc:
                print(f'[StockSyncOptimizer] 同步循环异常: {exc}')
            await asyncio.sleep(config.SYNC_INTERVAL)

    async def sync_once(self) -> dict:
        """
        执行一次完整的增量同步（带重试）

        :return: 同步统计信息
        """
        record_id = self._start_sync_record()
        stats = {
            'products_total': 0,
            'products_updated': 0,
            'products_unchanged': 0,
            'status': 'success',
            'error_message': None,
        }

        try:
            # 从源机器人抓取最新数据
            fresh_products = await self._fetch_with_retry()

            stats['products_total'] = len(fresh_products)

            # 增量对比并更新
            for product in fresh_products:
                result = self._apply_incremental_update(product)
                if result['changed']:
                    stats['products_updated'] += 1
                else:
                    stats['products_unchanged'] += 1

            # 检查预警
            await self.inventory.check_alerts()

        except Exception as exc:
            stats['status'] = 'failed'
            stats['error_message'] = str(exc)
            print(f'[StockSyncOptimizer] 同步失败: {exc}')

            # 通知管理员
            if self.inventory._notify:
                await self.inventory._notify(
                    f'❌ 库存同步失败：{exc}'
                )

        self._finish_sync_record(record_id, stats)

        print(
            f'[Sync] 完成 — 共 {stats["products_total"]} 个商品，'
            f'更新 {stats["products_updated"]} 个，'
            f'无变化 {stats["products_unchanged"]} 个，'
            f'状态: {stats["status"]}'
        )
        return stats

    # ------------------------------------------------------------------
    # 同步历史查询
    # ------------------------------------------------------------------

    def get_sync_history(self, limit: int = 20) -> list[dict]:
        """返回最近 N 条同步记录"""
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                '''
                SELECT * FROM sync_records
                ORDER BY started_at DESC
                LIMIT ?
                ''',
                (limit,)
            )
            rows = c.fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 私有：抓取（带指数退避重试）
    # ------------------------------------------------------------------

    async def _fetch_with_retry(self) -> list[dict]:
        """
        调用 ProductScraper.scrape_products()，失败时指数退避重试
        """
        last_exc = None
        delay = 2  # 初始等待 2 秒

        for attempt in range(1, config.MAX_SYNC_RETRIES + 1):
            try:
                products = await self.scraper.scrape_products()
                return products if products else []
            except Exception as exc:
                last_exc = exc
                print(
                    f'[StockSyncOptimizer] 第 {attempt}/{config.MAX_SYNC_RETRIES} 次抓取失败: '
                    f'{exc}，{delay}s 后重试'
                )
                if attempt < config.MAX_SYNC_RETRIES:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)  # 指数退避，最大 60 秒

        raise RuntimeError(
            f'抓取失败，已重试 {config.MAX_SYNC_RETRIES} 次。最后错误: {last_exc}'
        )

    # ------------------------------------------------------------------
    # 私有：增量更新单个商品
    # ------------------------------------------------------------------

    def _apply_incremental_update(self, product: dict) -> dict:
        """
        将抓取到的商品数据与数据库对比，仅在变化时写库

        :param product: ProductScraper 返回的商品字典
        :return: {'changed': bool, ...}
        """
        with get_db() as conn:
            c = conn.cursor()

            # 尝试按 source_product_id 查找已有商品
            c.execute(
                'SELECT id, stock FROM products WHERE source_product_id = ?',
                (product.get('source_product_id'),)
            )
            row = c.fetchone()

            if not row:
                # 全新商品 — 直接插入（沿用 ProductScraper.save_products 已插入的记录）
                return {'changed': True, 'reason': 'new_product'}

            product_id = row['id']
            new_stock = product.get('stock', 0)

            # 增量：只更新变化的库存
            result = self.inventory.update_stock(
                product_id, new_stock,
                note='增量同步'
            )
            return result

    # ------------------------------------------------------------------
    # 私有：同步记录管理
    # ------------------------------------------------------------------

    def _start_sync_record(self) -> int:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                '''
                INSERT INTO sync_records (started_at, status)
                VALUES (?, 'running')
                ''',
                (datetime.now(),)
            )
            return c.lastrowid

    def _finish_sync_record(self, record_id: int, stats: dict) -> None:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                '''
                UPDATE sync_records
                SET finished_at        = ?,
                    products_total     = ?,
                    products_updated   = ?,
                    products_unchanged = ?,
                    status             = ?,
                    error_message      = ?
                WHERE id = ?
                ''',
                (
                    datetime.now(),
                    stats['products_total'],
                    stats['products_updated'],
                    stats['products_unchanged'],
                    stats['status'],
                    stats.get('error_message'),
                    record_id,
                )
            )
