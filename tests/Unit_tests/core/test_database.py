import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.core.cache import AsyncLRUCache
from src.core.database import ConnectionPool, DatabaseManager
from src.features.notifications import NotificationManager
from src.utils.logging_config import setup_logging

logger = setup_logging()


@pytest.fixture
def test_db_url() -> Iterable[str]:
    """Return a test database URL"""
    # Create a temporary file
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_name = tmp.name

    # Yield the file path to the tests
    yield f"sqlite:///{tmp_name}"


@pytest_asyncio.fixture
async def db_manager(test_db_url: str) -> AsyncGenerator[DatabaseManager]:
    """Create and initialize a test DatabaseManager instance"""
    manager = DatabaseManager(database_url=test_db_url)

    # Replace caches with non-persistent versions
    manager.price_cache = AsyncLRUCache(max_size=200, ttl=600, cache_name=None)
    manager.competitor_urls_cache = AsyncLRUCache(
        max_size=100, ttl=1800, cache_name=None
    )
    await manager.initialize()
    yield manager

    # Clean up
    await ConnectionPool.close_all()
    test_db_path = test_db_url.replace("sqlite:///", "")
    Path(test_db_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_initialize_creates_tables(db_manager: DatabaseManager) -> None:
    """Test that initialization creates the required tables"""

    # Query for table existence
    query = "SELECT name FROM sqlite_master WHERE type='table' AND name='price_history'"
    tables = [row[0] for row in await db_manager.db.fetch_all(query)]

    assert "price_history" in tables


@pytest.mark.asyncio
async def test_insert_price_data(db_manager: DatabaseManager) -> None:
    """Test inserting price data into the database"""
    test_data = {
        "product_name": "Test Product",
        "url": "https://example.com/product",
        "price": 99.99,
        "regular_price": 129.99,
        "sale_price": 99.99,
    }

    await db_manager.insert_price_data(test_data)

    # Verify data was inserted
    query = db_manager.price_history.select().where(
        db_manager.price_history.c.product_name == "Test Product"
    )
    result = await db_manager.db.fetch_one(query)

    assert result is not None
    assert result[db_manager.price_history.c.product_name] == "Test Product"
    assert result[db_manager.price_history.c.url] == "https://example.com/product"
    assert result[db_manager.price_history.c.price] == 99.99
    assert result[db_manager.price_history.c.regular_price] == 129.99
    assert result[db_manager.price_history.c.sale_price] == 99.99


class TestUpdatePriceDatabase:
    @pytest.mark.asyncio
    async def test_changed_price(self, db_manager: DatabaseManager) -> None:
        """Test updating prices when price has changed"""
        # Insert initial data
        initial_data = {
            "product_name": "Test Product",
            "url": "https://example.com/product",
            "price": 99.99,
            "regular_price": 129.99,
            "sale_price": 99.99,
        }
        await db_manager.insert_price_data(initial_data)

        await asyncio.sleep(1.5)

        # Create new entry with changed price
        entries = [
            {
                "product_name": "Test Product",
                "url": "https://example.com/product",
                "data": {"price": 89.99, "regular_price": 129.99, "sale_price": 89.99},
            }
        ]

        changed_urls = await db_manager.update_price_database(entries)

        # Check that the URL was reported as changed
        assert len(changed_urls) == 1
        assert ("Test Product", "https://example.com/product") in changed_urls

        # Verify the new price is in the database
        price = await db_manager.get_latest_price(
            "Test Product", "https://example.com/product"
        )
        assert price == 89.99

    @pytest.mark.asyncio
    async def test_unchanged_price(self, db_manager: DatabaseManager) -> None:
        """Test updating prices when price hasn't changed"""
        # Insert initial data
        initial_data = {
            "product_name": "Test Product",
            "url": "https://example.com/product",
            "price": 99.99,
            "regular_price": 129.99,
            "sale_price": 99.99,
        }
        await db_manager.insert_price_data(initial_data)

        # Create new entry with same price
        entries = [
            {
                "product_name": "Test Product",
                "url": "https://example.com/product",
                "data": {"price": 99.99, "regular_price": 129.99, "sale_price": 99.99},
            }
        ]

        changed_urls = await db_manager.update_price_database(entries)

        # Check that no URLs were reported as changed
        assert len(changed_urls) == 0

    @pytest.mark.asyncio
    async def test_error_entry(self, db_manager: DatabaseManager) -> None:
        """Test updating prices with an error entry"""
        entries = [
            {
                "product_name": "Test Product",
                "url": "https://example.com/product",
                "error": "Failed to fetch price",
            }
        ]

        changed_urls = await db_manager.update_price_database(entries)

        # Should return empty set as error entries are skipped
        assert len(changed_urls) == 0


class TestGetData:
    @pytest.mark.asyncio
    async def test_get_latest_price(self, db_manager: DatabaseManager) -> None:
        """Test retrieving the latest price for a product URL"""
        # Insert test data
        product_name = "Test Product"
        url = "https://example.com/product"

        test_data = {
            "product_name": product_name,
            "url": url,
            "price": 99.99,
            "regular_price": 129.99,
            "sale_price": 99.99,
        }
        await db_manager.insert_price_data(test_data)

        await asyncio.sleep(1.5)

        # Update with a new price
        updated_data = {
            "product_name": product_name,
            "url": url,
            "price": 89.99,
            "regular_price": 129.99,
            "sale_price": 89.99,
        }
        await db_manager.insert_price_data(updated_data)

        # Get the latest price
        latest_price = await db_manager.get_latest_price(
            "Test Product", "https://example.com/product"
        )

        # Should return the most recent price
        assert latest_price == 89.99

        # Test cache hit
        with patch.object(AsyncLRUCache, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = 79.99
            price = await db_manager.get_latest_price(product_name, url)
            assert price == 79.99
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_target_price(self, db_manager: DatabaseManager) -> None:
        """Test retrieving target site price for a product"""
        # Insert test data for target site
        target_data = {
            "product_name": "Test Product",
            "url": "https://target-site.com/product",
            "price": 109.99,
            "regular_price": 129.99,
            "sale_price": 109.99,
        }
        await db_manager.insert_price_data(target_data)

        # Get the target price
        target_price = await db_manager.get_target_price(
            "Test Product", "target-site.com"
        )

        assert target_price == 109.99

    @pytest.mark.asyncio
    async def test_get_competitor_urls(self, db_manager: DatabaseManager) -> None:
        """Test retrieving competitor URLs for a product"""
        # Insert test data for multiple sites
        target_data = {
            "product_name": "Test Product",
            "url": "https://target-site.com/product",
            "price": 109.99,
        }
        await db_manager.insert_price_data(target_data)

        competitor1_data = {
            "product_name": "Test Product",
            "url": "https://competitor1.com/product",
            "price": 99.99,
        }
        await db_manager.insert_price_data(competitor1_data)

        competitor2_data = {
            "product_name": "Test Product",
            "url": "https://competitor2.com/product",
            "price": 89.99,
        }
        await db_manager.insert_price_data(competitor2_data)

        # Get competitor URLs
        competitor_urls = await db_manager.get_competitor_urls(
            "Test Product", "target-site.com"
        )

        assert len(competitor_urls) == 2
        assert "https://competitor1.com/product" in competitor_urls
        assert "https://competitor2.com/product" in competitor_urls


@pytest.mark.asyncio
async def test_check_price_against_target(db_manager: DatabaseManager) -> None:
    """Test checking if price is lower than target site's price"""
    # Create mock notification manager
    notification_mgr = NotificationManager()
    notification_mgr.send_alert = AsyncMock()

    # Create mock session
    session = MagicMock()

    # Mock get_target_price and get_latest_price
    db_manager.get_target_price = AsyncMock(return_value=109.99)
    db_manager.get_latest_price = AsyncMock(return_value=99.99)

    # Check price against target
    await db_manager.check_price_against_target(
        notification_mgr,
        session,
        "Test Product",
        "https://competitor.com/product",
        "target-site.com",
    )

    # Notification should be sent
    notification_mgr.send_alert.assert_called_once()
    assert "lower price" in notification_mgr.send_alert.call_args[0][1]

    # Test with higher price (should not send notification)
    db_manager.get_latest_price = AsyncMock(return_value=119.99)
    notification_mgr.send_alert.reset_mock()

    await db_manager.check_price_against_target(
        notification_mgr,
        session,
        "Test Product",
        "https://competitor.com/product",
        "target-site.com",
    )

    notification_mgr.send_alert.assert_not_called()


@pytest.mark.asyncio
async def test_check_all_competitors(db_manager: DatabaseManager) -> None:
    """Test checking all competitors against target site"""
    # Mock the notification manager and methods
    notification_mgr = NotificationManager()

    with patch.object(
        db_manager, "get_competitor_urls", new_callable=AsyncMock
    ) as mock_get_urls:
        with patch.object(
            db_manager, "check_price_against_target", new_callable=AsyncMock
        ) as mock_check:
            # Setup mock return values
            mock_get_urls.return_value = [
                "https://competitor1.com/product",
                "https://competitor2.com/product",
            ]

            # Call the method
            session = AsyncMock()
            await db_manager.check_all_competitors(
                notification_mgr, session, "Test Product", "target-site.com"
            )

            # Verify calls
            mock_get_urls.assert_called_once_with("Test Product", "target-site.com")
            assert mock_check.call_count == 2


@pytest.mark.asyncio
async def test_process_price_changes_target_changed(
    db_manager: DatabaseManager,
) -> None:
    """Test processing price changes when target site price changes"""
    # Mock the notification manager and methods
    notification_mgr = NotificationManager()

    with patch.object(
        db_manager, "check_all_competitors", new_callable=AsyncMock
    ) as mock_check_all:
        # Create a set of changed URLs including a target site URL
        changed_urls = {("Test Product", "https://target-site.com/product")}

        # Call the method
        await db_manager.process_price_changes(
            notification_mgr, changed_urls, "target-site.com"
        )

        # Verify check_all_competitors was called
        mock_check_all.assert_called_once()


@pytest.mark.asyncio
async def test_process_price_changes_competitor_changed(
    db_manager: DatabaseManager,
) -> None:
    """Test processing price changes when competitor price changes"""
    # Mock the notification manager and methods
    notification_mgr = NotificationManager()

    with patch.object(
        db_manager, "check_price_against_target", new_callable=AsyncMock
    ) as mock_check:
        # Create a set of changed URLs with only competitor URLs
        changed_urls = {("Test Product", "https://competitor.com/product")}

        # Call the method
        await db_manager.process_price_changes(
            notification_mgr, changed_urls, "target-site.com"
        )

        # Verify check_price_against_target was called
        mock_check.assert_called_once()


@pytest.mark.asyncio
async def test_connection_pool(test_db_url: str) -> None:
    """Test the ConnectionPool class"""
    db_path = test_db_url.replace("sqlite:///", "")

    # Get connection from pool
    db1 = await ConnectionPool.get_connection(test_db_url)
    db2 = await ConnectionPool.get_connection(test_db_url)

    # Both should be the same instance
    assert db1 is db2

    # Close connections
    await ConnectionPool.close_all()

    # Clean up
    if os.path.exists(db_path):
        os.remove(db_path)
