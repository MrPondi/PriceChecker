# database.py
import asyncio
from collections import defaultdict
from typing import Optional

import tldextract
from aiohttp import ClientSession, ClientTimeout
from databases import Database
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    select,
)

from src.core.cache import AsyncLRUCache
from src.features.notifications import NotificationManager
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class ConnectionPool:
    """Database connection pool manager"""

    _instances: dict[str, Database] = {}
    _locks: dict[str, asyncio.Lock] = {}

    @classmethod
    async def get_connection(cls, database_url: str) -> Database:
        """Get a database connection from the pool"""
        if database_url not in cls._locks:
            cls._locks[database_url] = asyncio.Lock()

        async with cls._locks[database_url]:
            if database_url not in cls._instances:
                db = Database(database_url)
                await db.connect()
                cls._instances[database_url] = db
                logger.info(f"Created new database connection for {database_url}")

            return cls._instances[database_url]

    @classmethod
    async def close_all(cls) -> None:
        """Close all database connections"""
        for url, db in cls._instances.items():
            logger.info(f"Closing database connection for {url}")
            await db.disconnect()

        cls._instances.clear()


class DatabaseManager:
    """Manages price tracking database operations"""

    def __init__(self, database_url: str = "sqlite:///data/product_prices.db") -> None:
        self.metadata = MetaData()
        self.timeout = ClientTimeout(total=30, sock_connect=15)
        self.database_url = database_url
        self.price_history = self._define_price_history_table()

        self.price_cache = AsyncLRUCache(
            max_size=200, ttl=600, cache_name="prices"
        )  # 10 minute TTL
        self.competitor_urls_cache = AsyncLRUCache(
            max_size=100, ttl=1800, cache_name="competitor_urls"
        )  # 30 minute TTL

    def _define_price_history_table(self) -> Table:
        """Define SQLAlchemy table structure for price history"""
        return Table(
            "price_history",
            self.metadata,
            Column("id", Integer, primary_key=True),
            Column("product_name", String(255), nullable=False),
            Column("url", String(512), nullable=False),
            Column("price", Float),
            Column("regular_price", Float),
            Column("sale_price", Float),
            Column("timestamp", DateTime, server_default="CURRENT_TIMESTAMP"),
            Index("idx_url_timestamp", "url", "timestamp"),
            Index("idx_product_name", "product_name"),
        )

    async def initialize(self) -> None:
        """Initialize database connection and create tables"""
        self.db = await ConnectionPool.get_connection(self.database_url)
        await self._create_tables()

    async def _create_tables(self) -> None:
        """Create tables and indexes if they don't exist"""
        await self.db.connect()
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT NOT NULL,
                url TEXT NOT NULL,
                price REAL,
                regular_price REAL,
                sale_price REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_url_timestamp ON price_history(url, timestamp)"  # noqa: E501
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_name ON price_history(product_name)"
        )

    async def insert_price_data(self, data: dict) -> None:
        """Insert new price data into the database"""
        query = self.price_history.insert().values(
            product_name=data["product_name"],
            url=data["url"],
            price=data.get("price"),
            regular_price=data.get("regular_price"),
            sale_price=data.get("sale_price"),
        )
        await self.db.execute(query)

        # Invalidate cache entries for this product/url
        cache_key = f"price:{data['product_name']}:{data['url']}"
        # We don't need to await this since we're just invalidating
        asyncio.create_task(self.price_cache.set(cache_key, None))

        # Also invalidate target price cache if this is a target site
        target_cache_key = f"target_price:{data['product_name']}"
        asyncio.create_task(self.price_cache.set(target_cache_key, None))

    async def update_price_database(self, entries: list[dict]) -> set[tuple[str, str]]:
        """Update database with new prices and return changed URLs"""
        changed_urls = set()

        async with self.db.transaction():
            for entry in entries:
                if "error" in entry or "data" not in entry:
                    continue

                product_name = entry["product_name"]
                url = entry["url"]
                data = entry["data"]
                price = data.get("price")

                # Get last price from database
                query = (
                    select(self.price_history.c.price)
                    .where(self.price_history.c.url == url)
                    .order_by(self.price_history.c.timestamp.desc())
                    .limit(1)
                )

                result = await self.db.fetch_one(query)
                old_price = result[self.price_history.c.price] if result else None

                if price == old_price:
                    logger.debug(f"Price unchanged for {product_name} at {url}")
                    continue

                # Insert new price entry
                await self.insert_price_data(
                    {
                        "product_name": product_name,
                        "url": url,
                        "price": price,
                        "regular_price": data.get("regular_price"),
                        "sale_price": data.get("sale_price"),
                    }
                )

                changed_urls.add((product_name, url))
                logger.info(
                    f"Price changed for {product_name} at {url}: {old_price} → {price}"
                )

        return changed_urls

    async def get_latest_price(self, product_name: str, url: str) -> Optional[float]:
        """Get latest price for a product URL with caching"""
        cache_key = f"price:{product_name}:{url}"

        # Check cache first
        cached_price = await self.price_cache.get(cache_key)
        if cached_price is not None:
            logger.debug(f"Cache hit for price of {product_name} at {url}")
            return cached_price

        # If not in cache, query database
        query = (
            select(self.price_history.c.price)
            .where(self.price_history.c.url == url)
            .order_by(self.price_history.c.timestamp.desc())
            .limit(1)
        )

        result = await self.db.fetch_one(query)
        price = result[self.price_history.c.price] if result else None

        # Cache the result
        if price is not None:
            await self.price_cache.set(cache_key, price)

        return price

    async def get_target_price(
        self, product_name: str, target_site: str
    ) -> Optional[float]:
        """Get target site price for a product with caching"""
        cache_key = f"target_price:{product_name}"

        # Check cache first
        cached_price = await self.price_cache.get(cache_key)
        if cached_price is not None:
            logger.debug(f"Cache hit for target price of {product_name}")
            return cached_price

        # If not in cache, query database
        target_query = (
            select(self.price_history.c.price)
            .where(
                (self.price_history.c.product_name == product_name)
                & (self.price_history.c.url.like(f"%{target_site}%"))
            )
            .order_by(self.price_history.c.timestamp.desc())
            .limit(1)
        )

        target_result = await self.db.fetch_one(target_query)
        target_price = (
            target_result[self.price_history.c.price] if target_result else None
        )

        # Cache the result
        if target_price is not None:
            await self.price_cache.set(cache_key, target_price)

        return target_price

    async def get_competitor_urls(
        self, product_name: str, target_site: str
    ) -> list[str]:
        """Get all competitor URLs for a product with caching"""
        cache_key = f"competitor_urls:{product_name}"

        # Check cache first
        cached_urls = await self.competitor_urls_cache.get(cache_key)
        if cached_urls is not None:
            logger.debug(f"Cache hit for competitor URLs of {product_name}")
            return cached_urls

        # If not in cache, query database
        query = (
            select(self.price_history.c.url)
            .distinct()
            .where(
                (self.price_history.c.product_name == product_name)
                & (~self.price_history.c.url.like(f"%{target_site}%"))
            )
        )

        results = await self.db.fetch_all(query)
        competitor_urls = [row[self.price_history.c.url] for row in results]

        # Cache the result
        await self.competitor_urls_cache.set(cache_key, competitor_urls)

        return competitor_urls

    async def check_price_against_target(
        self,
        notification_mgr: NotificationManager,
        session: ClientSession,
        product: str,
        url: str,
        target_site: str,
    ) -> None:
        """Check if price is lower than target site's price"""
        try:
            # Get target price
            target_price = await self.get_target_price(product, target_site)

            if not target_price:
                logger.debug(f"No target price found for {product}")
                return

            # Get current price
            current_price = await self.get_latest_price(product, url)

            if current_price and current_price < target_price:
                domain = tldextract.extract(url).registered_domain
                message = (
                    f"{product}: {domain} has lower price ({current_price:.2f}) "
                    f"than {target_site} ({target_price:.2f})"
                )
                await notification_mgr.send_alert(session, message)

        except Exception as e:
            logger.error(f"Price check failed for {url}: {str(e)}")

    async def check_all_competitors(
        self,
        notification_mgr: NotificationManager,
        session: ClientSession,
        product: str,
        target_site: str,
    ) -> None:
        """Check all competitors after target site price change"""
        try:
            # Get competitor URLs
            competitor_urls = await self.get_competitor_urls(product, target_site)

            for url in competitor_urls:
                await self.check_price_against_target(
                    notification_mgr, session, product, url, target_site
                )

        except Exception as e:
            logger.error(f"Competitor check failed for {product}: {str(e)}")

    async def process_price_changes(
        self,
        notification_mgr: NotificationManager,
        changed_urls: set[tuple[str, str]],
        target_site: str,
    ) -> None:
        """Handle price change notifications and comparisons"""
        logger.info("Processing price changes...")

        async with ClientSession(timeout=self.timeout) as session:
            product_groups = defaultdict(list)
            for product, url in changed_urls:
                product_groups[product].append(url)

            for product, urls in product_groups.items():
                target_urls = [url for url in urls if target_site in url]

                if target_urls:
                    await self.check_all_competitors(
                        notification_mgr, session, product, target_site
                    )
                else:
                    for url in urls:
                        await self.check_price_against_target(
                            notification_mgr,
                            session,
                            product,
                            url,
                            target_site,
                        )
