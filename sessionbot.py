"""
号铺机器人代理转卖系统 - 技术方案

功能：
1. 克隆源机器人的商品信息
2. 在你的机器人展示商品（加价）
3. 用户下单后，自动在源机器人代购
4. 自动转发账号给用户

技术栈：
- Python + Telethon (Telegram 机器人)
- SQLite/MySQL (数据库)
- python-telegram-bot (机器人框架)
"""

from telethon import TelegramClient, events, Button
from telethon.tl.types import MessageEntityBotCommand
import sqlite3
import asyncio
import re
from datetime import datetime
import json

# ========================
# 配置
# ========================

# 你的 Telegram API
API_ID = '2040'
API_HASH = 'b18441a1ff607e10a989891a5462e627'

# 机器人配置
YOUR_BOT_TOKEN = 'YOUR_BOT_TOKEN'  # 你的销售机器人 token
SOURCE_BOT_USERNAME = '@source_shop_bot'  # 源号铺机器人
BUYER_ACCOUNT_SESSION = 'buyer_account'  # 代购账号的 session

# 加价策略
MARKUP_PERCENTAGE = 0.20  # 加价 20%
MARKUP_FIXED = 5  # 或固定加价 5 元

# ========================
# 数据库初始化
# ========================

def init_database():
    """初始化数据库"""
    conn = sqlite3.connect('shop_proxy.db')
    c = conn.cursor()
    
    # 商品表
    c.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_product_id TEXT UNIQUE,
            name TEXT,
            description TEXT,
            price REAL,
            stock INTEGER,
            image_url TEXT,
            category TEXT,
            last_updated TIMESTAMP
        )
    ''')
    
    # 订单表
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            product_id INTEGER,
            quantity INTEGER,
            total_price REAL,
            cost_price REAL,
            profit REAL,
            status TEXT,  -- pending, paid, purchasing, delivered, completed, failed
            source_order_id TEXT,
            account_info TEXT,
            created_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    ''')
    
    conn.commit()
    conn.close()

# ========================
# 1. 商品信息克隆模块
# ========================

class ProductScraper:
    """从源机器人抓取商品信息"""
    
    def __init__(self, client, source_bot):
        self.client = client
        self.source_bot = source_bot
    
    async def scrape_products(self):
        """
        抓取源机器人的商品列表
        
        实现方式：
        1. 向源机器人发送 /start 或 /products 命令
        2. 解析返回的商品列表消息
        3. 提取商品信息（名称、价格、库存）
        """
        print("开始抓取商品信息...")
        
        # 向源机器人发送命令
        await self.client.send_message(self.source_bot, '/products')
        
        # 等待响应
        await asyncio.sleep(2)
        
        # 获取最新消息
        messages = await self.client.get_messages(self.source_bot, limit=10)
        
        products = []
        
        for message in messages:
            if not message.text:
                continue
            
            # 解析商品信息（根据源机器人的消息格式调整）
            product = self.parse_product_message(message.text, message)
            
            if product:
                products.append(product)
        
        # 保存到数据库
        self.save_products(products)
        
        print(f"✅ 抓取到 {len(products)} 个商品")
        
        return products
    
    def parse_product_message(self, text, message):
        """
        解析商品消息
        
        示例消息格式：
        📦 TikTok 账号
        💰 价格：$5.00
        📊 库存：100
        📝 描述：带粉丝的 TikTok 账号
        """
        try:
            # 提取商品名称
            name_match = re.search(r'📦\s*(.+?)\\n', text)
            name = name_match.group(1).strip() if name_match else None
            
            # 提取价格
            price_match = re.search(r'💰.*?([\\d.]+)', text)
            price = float(price_match.group(1)) if price_match else None
            
            # 提取库存
            stock_match = re.search(r'📊.*?(\\d+)', text)
            stock = int(stock_match.group(1)) if stock_match else 0
            
            # 提取描述
            desc_match = re.search(r'📝\\s*描述[：:]\\s*(.+)', text)
            description = desc_match.group(1).strip() if desc_match else ''
            
            if name and price:
                return {
                    'name': name,
                    'price': price,
                    'stock': stock,
                    'description': description,
                    'source_product_id': str(hash(name)),  # 生成唯一 ID
                    'image_url': None,  # 如果消息有图片，提取 URL
                    'last_updated': datetime.now()
                }
        
        except Exception as e:
            print(f"解析商品失败: {e}")
        
        return None
    
    def save_products(self, products):
        """保存商品到数据库"""
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        for product in products:
            c.execute('''
                INSERT OR REPLACE INTO products 
                (source_product_id, name, description, price, stock, image_url, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                product['source_product_id'],
                product['name'],
                product['description'],
                product['price'],
                product['stock'],
                product['image_url'],
                product['last_updated']
            ))
        
        conn.commit()
        conn.close()

# ========================
# 2. 你的销售机器人
# ========================

class YourShopBot:
    """你的销售机器人"""
    
    def __init__(self, client):
        self.client = client
    
    async def start(self):
        """启动机器人"""
        
        @self.client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            """欢迎消息"""
            await event.respond(
                "🛍️ 欢迎来到我们的商店！\\n\\n"
                "📦 查看商品：/products\\n"
                "📋 我的订单：/orders\\n"
                "❓ 帮助：/help"
            )
        
        @self.client.on(events.NewMessage(pattern='/products'))
        async def products_handler(event):
            """展示商品列表"""
            products = self.get_products_with_markup()
            
            if not products:
                await event.respond("暂无商品")
                return
            
            # 生成商品列表按钮
            buttons = []
            for product in products:
                buttons.append([
                    Button.inline(
                        f"{product['name']} - ${product['price']:.2f}",
                        data=f"product_{product['id']}"
                    )
                ])
            
            await event.respond(
                "📦 商品列表：\\n\\n选择商品查看详情",
                buttons=buttons
            )
        
        @self.client.on(events.CallbackQuery(pattern=b'product_'))
        async def product_detail_handler(event):
            """商品详情"""
            product_id = int(event.data.decode().split('_')[1])
            product = self.get_product(product_id)
            
            if not product:
                await event.answer("商品不存在")
                return
            
            text = (
                f"📦 {product['name']}\\n\\n"
                f"💰 价格：${product['price']:.2f}\\n"
                f"📊 库存：{product['stock']}\\n"
                f"📝 描述：{product['description']}"
            )
            
            buttons = [
                [Button.inline("🛒 购买", data=f"buy_{product_id}")],
                [Button.inline("« 返回", data="back_to_products")]
            ]
            
            await event.edit(text, buttons=buttons)
        
        @self.client.on(events.CallbackQuery(pattern=b'buy_'))
        async def buy_handler(event):
            """处理购买"""
            product_id = int(event.data.decode().split('_')[1])
            user_id = event.sender_id
            username = event.sender.username
            
            # 创建订单
            order_id = self.create_order(user_id, username, product_id)
            
            await event.answer("订单已创建！")
            await event.edit(
                f"✅ 订单已创建！\\n\\n"
                f"订单号：{order_id}\\n"
                f"请支付后点击下方按钮确认",
                buttons=[
                    [Button.inline("✅ 已支付", data=f"paid_{order_id}")],
                    [Button.inline("❌ 取消订单", data=f"cancel_{order_id}")]
                ]
            )
        
        print("销售机器人已启动")
    
    def get_products_with_markup(self):
        """获取加价后的商品列表"""
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        c.execute('SELECT * FROM products WHERE stock > 0')
        products = []
        
        for row in c.fetchall():
            product = {
                'id': row[0],
                'name': row[2],
                'description': row[3],
                'price': row[4] * (1 + MARKUP_PERCENTAGE),  # 加价
                'stock': row[5]
            }
            products.append(product)
        
        conn.close()
        return products
    
    def get_product(self, product_id):
        """获取单个商品"""
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        c.execute('SELECT * FROM products WHERE id = ?', (product_id,))
        row = c.fetchone()
        conn.close()
        
        if row:
            return {
                'id': row[0],
                'source_product_id': row[1],
                'name': row[2],
                'description': row[3],
                'price': row[4] * (1 + MARKUP_PERCENTAGE),
                'cost_price': row[4],  # 成本价
                'stock': row[5]
            }
        
        return None
    
    def create_order(self, user_id, username, product_id):
        """创建订单"""
        product = self.get_product(product_id)
        
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        c.execute('''
            INSERT INTO orders 
            (user_id, username, product_id, quantity, total_price, cost_price, profit, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            username,
            product_id,
            1,
            product['price'],
            product['cost_price'],
            product['price'] - product['cost_price'],
            'pending',
            datetime.now()
        ))
        
        order_id = c.lastrowid
        conn.commit()
        conn.close()
        
        return order_id

# ========================
# 3. 自动代购模块（核心）
# ========================

class AutoPurchaser:
    """自动代购模块"""
    
    def __init__(self, client, source_bot):
        self.client = client
        self.source_bot = source_bot
    
    async def purchase_for_order(self, order_id):
        """
        为订单自动代购
        
        流程：
        1. 获取订单信息
        2. 用代购账号在源机器人下单
        3. 监控源机器人发货
        4. 接收账号信息
        5. 转发给真实用户
        """
        print(f"开始处理订单 {order_id}...")
        
        # 获取订单
        order = self.get_order(order_id)
        
        if not order:
            print(f"订单 {order_id} 不存在")
            return False
        
        # 更新订单状态
        self.update_order_status(order_id, 'purchasing')
        
        try:
            # 步骤1: 在源机器人下单
            await self.place_order_at_source(order)
            
            # 步骤2: 等待并监控发货
            account_info = await self.wait_for_delivery(order)
            
            if not account_info:
                self.update_order_status(order_id, 'failed')
                return False
            
            # 步骤3: 转发给用户
            await self.deliver_to_user(order, account_info)
            
            # 步骤4: 完成订单
            self.update_order_status(order_id, 'completed')
            self.save_account_info(order_id, account_info)
            
            print(f"✅ 订单 {order_id} 处理完成")
            return True
        
        except Exception as e:
            print(f"❌ 订单 {order_id} 处理失败: {e}")
            self.update_order_status(order_id, 'failed')
            return False
    
    async def place_order_at_source(self, order):
        """在源机器人下单"""
        print(f"在源机器人购买商品...")
        
        # 向源机器人发送购买命令
        # 这里需要根据源机器人的实际命令调整
        await self.client.send_message(
            self.source_bot,
            f"/buy {order['source_product_id']}"
        )
        
        await asyncio.sleep(2)
    
    async def wait_for_delivery(self, order, timeout=300):
        """
        等待源机器人发货
        
        监控源机器人的消息，提取账号信息
        """
        print("等待发货...")
        
        start_time = datetime.now()
        
        while (datetime.now() - start_time).seconds < timeout:
            # 获取源机器人的最新消息
            messages = await self.client.get_messages(self.source_bot, limit=5)
            
            for message in messages:
                if not message.text:
                    continue
                
                # 检查是否是发货消息
                if self.is_delivery_message(message.text):
                    # 提取账号信息
                    account_info = self.extract_account_info(message.text)
                    return account_info
            
            await asyncio.sleep(5)
        
        print("等待发货超时")
        return None
    
    def is_delivery_message(self, text):
        """判断是否是发货消息"""
        keywords = ['账号', '密码', '已发货', '购买成功', 'Account', 'Password']
        return any(keyword in text for keyword in keywords)
    
    def extract_account_info(self, text):
        """从发货消息中提取账号信息"""
        # 根据实际消息格式调整
        # 示例：
        # 账号：user123
        # 密码：pass456
        
        account_match = re.search(r'账号[：:]\\s*(.+)', text)
        password_match = re.search(r'密码[：:]\\s*(.+)', text)
        
        if account_match and password_match:
            return {
                'account': account_match.group(1).strip(),
                'password': password_match.group(1).strip(),
                'full_text': text
            }
        
        return {'full_text': text}
    
    async def deliver_to_user(self, order, account_info):
        """发货给用户"""
        print(f"发货给用户 {order['user_id']}...")
        
        message = (
            f"🎉 您的订单已完成！\\n\\n"
            f"📦 商品：{order['product_name']}\\n"
            f"💰 订单号：{order['id']}\\n\\n"
            f"📝 账号信息：\\n"
            f"{account_info['full_text']}"
        )
        
        await self.client.send_message(order['user_id'], message)
    
    def get_order(self, order_id):
        """获取订单"""
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        c.execute('''
            SELECT o.*, p.name as product_name, p.source_product_id 
            FROM orders o
            JOIN products p ON o.product_id = p.id
            WHERE o.id = ?
        ''', (order_id,))
        
        row = c.fetchone()
        conn.close()
        
        if row:
            return {
                'id': row[0],
                'user_id': row[1],
                'username': row[2],
                'product_id': row[3],
                'product_name': row[12],
                'source_product_id': row[13],
                'total_price': row[5],
                'status': row[8]
            }
        
        return None
    
    def update_order_status(self, order_id, status):
        """更新订单状态"""
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        c.execute('UPDATE orders SET status = ? WHERE id = ?', (status, order_id))
        
        if status == 'completed':
            c.execute('UPDATE orders SET completed_at = ? WHERE id = ?', (datetime.now(), order_id))
        
        conn.commit()
        conn.close()
    
    def save_account_info(self, order_id, account_info):
        """保存账号信息"""
        conn = sqlite3.connect('shop_proxy.db')
        c = conn.cursor()
        
        c.execute(
            'UPDATE orders SET account_info = ? WHERE id = ?',
            (json.dumps(account_info), order_id)
        )
        
        conn.commit()
        conn.close()

# ========================
# 主程序
# ========================

async def main():
    """主程序"""
    
    # 初始化数据库
    init_database()
    
    # 创建客户端（代购账号）
    buyer_client = TelegramClient(BUYER_ACCOUNT_SESSION, API_ID, API_HASH)
    await buyer_client.start()
    
    # 创建销售机器人客户端
    bot_client = TelegramClient('shop_bot', API_ID, API_HASH)
    await bot_client.start(bot_token=YOUR_BOT_TOKEN)
    
    print("系统已启动")
    
    # 初始化模块
    scraper = ProductScraper(buyer_client, SOURCE_BOT_USERNAME)
    shop_bot = YourShopBot(bot_client)
    purchaser = AutoPurchaser(buyer_client, SOURCE_BOT_USERNAME)
    
    # 启动销售机器人
    await shop_bot.start()
    
    # 定时抓取商品（每小时一次）
    async def sync_products():
        while True:
            try:
                await scraper.scrape_products()
            except Exception as e:
                print(f"抓取商品失败: {e}")
            
            await asyncio.sleep(3600)  # 1 小时
    
    # 监控待处理订单
    async def process_orders():
        while True:
            # 查询已支付但未处理的订单
            conn = sqlite3.connect('shop_proxy.db')
            c = conn.cursor()
            c.execute('SELECT id FROM orders WHERE status = ?', ('paid',))
            orders = c.fetchall()
            conn.close()
            
            for order in orders:
                order_id = order[0]
                await purchaser.purchase_for_order(order_id)
            
            await asyncio.sleep(30)  # 每 30 秒检查一次
    
    # 启动后台任务
    asyncio.create_task(sync_products())
    asyncio.create_task(process_orders())
    
    print("所有模块已启动，系统运行中...")
    
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
