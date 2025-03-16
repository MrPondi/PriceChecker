import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional, Unpack
from unittest.mock import AsyncMock, patch

from aiohttp import ClientResponse, ClientSession, client, web
from databases import Database

from src.cli import main
from src.core.cache import AsyncLRUCache
from src.core.database import DatabaseManager
from src.features.fetchers import ApiFetcher, ScrapeFetcher
from src.features.notifications import NotificationManager
from src.models import InputFile

# Sample data for testing
SAMPLE_CONFIG = {
    "sites": [
        {
            "root_domain": "api-example.com",
            "category": "api",
            "env_variables": {
                "consumer_key": "test_key",
                "consumer_secret": "test_secret",
            },
        },
        {
            "root_domain": "scrape-example.com",
            "category": "scrape",
            "selectors": {
                "price": "#main-price",
                "regular_price": "#original-price",
                "sale_price": "#sale-price",
            },
        },
        {
            "root_domain": "target-example.com",
            "category": "scrape",
            "selectors": {"price": "#main-price"},
        },
    ],
    "products": [
        {
            "product_name": "Test Product",
            "urls": [
                "https://api-example.com/product/123",
                "https://scrape-example.com/product/123",
                "https://target-example.com/product/123",
            ],
        }
    ],
}

# Mock response data
API_RESPONSE = {"price": "100.50", "regular_price": "120.00", "sale_price": "100.50"}

SCRAPE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Product</title></head>
<body>
    <div id="main-price">95.99</div>
    <div id="original-price">110.00</div>
    <div id="sale-price">95.99</div>
</body>
</html>
"""

TARGET_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Product</title></head>
<body>
    <div id="main-price">105.00</div>
</body>
</html>
"""


class BaseIntegrationTest(unittest.TestCase):
    """Base class for integration tests"""

    def setUp(self) -> None:
        # Create a new event loop for each test
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.asyncSetUp())

    def tearDown(self) -> None:
        self.loop.run_until_complete(self.asyncTearDown())
        self.loop.close()
        asyncio.set_event_loop(None)

    def run(self, result: Optional["unittest.TestResult"] = None) -> None:
        """Override run method to handle async test methods"""
        method = getattr(self, self._testMethodName)
        if asyncio.iscoroutinefunction(method):
            self._run_async_test(method, result)
        else:
            super().run(result)

    def _run_async_test(
        self,
        method,  # noqa: ANN001
        result: Optional["unittest.TestResult"],
    ) -> Optional["unittest.TestResult"]:
        """Run an async test method"""
        if result is None:
            result = self.defaultTestResult()
        result.startTest(self)
        try:
            self.setUp()
            try:
                self.loop.run_until_complete(method())
            except Exception:
                result.addError(self, sys.exc_info())
            else:
                result.addSuccess(self)
        finally:
            try:
                self.tearDown()
            except Exception:
                result.addError(self, sys.exc_info())
        return result

    async def asyncSetUp(self) -> None:
        # Create temporary directory for test files
        self.temp_dir = Path(tempfile.mkdtemp())
        self.data_dir = self.temp_dir / "data"
        os.makedirs(self.data_dir, exist_ok=True)

        # Create test config file
        self.config_path = self.data_dir / "test_input.json"
        with open(self.config_path, "w") as f:
            json.dump(SAMPLE_CONFIG, f)

        # Set up mock database
        self.db_path = os.path.join(self.data_dir, "test_db.db")
        self.db_url = f"sqlite:///{self.db_path}"

        # Create mock HTTP server
        self.app = web.Application()
        self.setup_routes()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 8080)
        await self.site.start()

        # Setup notification mock
        self.notification_url = "http://localhost:8080/notify"
        self.notifications_received = []

        # Set environment variables
        os.environ["TARGET_SITE"] = "target-example.com"
        os.environ["DATABASE_URL"] = self.db_url
        os.environ["NOTIFICATION_URL"] = self.notification_url

    def setup_routes(self) -> None:
        """Set up mock HTTP server routes"""
        self.app.router.add_get("/api", self.handle_api_request)
        self.app.router.add_get("/scrape", self.handle_scrape_request)
        self.app.router.add_get("/target", self.handle_target_request)
        self.app.router.add_post("/notify", self.handle_notification)

    async def handle_api_request(self, request: web.Request) -> web.Response:
        """Handle API test requests"""
        return web.json_response(API_RESPONSE)

    async def handle_scrape_request(self, request: web.Request) -> web.Response:
        """Handle scrape test requests"""
        return web.Response(text=SCRAPE_HTML, content_type="text/html")

    async def handle_target_request(self, request: web.Request) -> web.Response:
        """Handle target site test requests"""
        return web.Response(text=TARGET_HTML, content_type="text/html")

    async def handle_notification(self, request: web.Request) -> web.Response:
        """Handle notifications"""
        data = await request.read()
        self.notifications_received.append(data.decode("utf-8"))
        return web.Response(text="OK")

    async def asyncTearDown(self) -> None:
        # Clean up test resources
        await self.runner.cleanup()
        shutil.rmtree(self.temp_dir)


class TestEndToEndFlow(BaseIntegrationTest):
    """Test the complete CLI workflow with mocked responses"""

    @patch("src.cli.ClientSession")
    @patch("src.features.fetchers.BaseFetcher._request_with_retry")
    def test_end_to_end_flow(self, mock_request, mock_session_class) -> None:  # noqa: ANN001
        """Test the complete workflow from CLI to database and notifications"""
        # Setup mocks
        mock_session = AsyncMock()
        mock_session_class.return_value.__aenter__.return_value = mock_session

        # Mock responses for different URLs
        api_response = AsyncMock(spec=ClientResponse)
        api_response.status = 200
        api_response.json.return_value = API_RESPONSE
        api_response.text.return_value = ""

        scrape_response = AsyncMock(spec=ClientResponse)
        scrape_response.status = 200
        scrape_response.text.return_value = SCRAPE_HTML
        scrape_response.json.side_effect = ValueError("Not JSON")

        target_response = AsyncMock(spec=ClientResponse)
        target_response.status = 200
        target_response.text.return_value = TARGET_HTML
        target_response.json.side_effect = ValueError("Not JSON")

        # Configure the mock to return different responses based on URL
        async def side_effect(
            url: str, **kwargs: Unpack[client._RequestOptions]
        ) -> AsyncMock:
            if "api-example.com" in url:
                return api_response
            if "scrape-example.com" in url:
                return scrape_response
            if "target-example.com" in url:
                return target_response
            raise ValueError(f"Unexpected URL: {url}")

        mock_request.side_effect = side_effect

        # Run main function with test config
        self.loop.run_until_complete(
            main(
                config_path=self.config_path,
                target_site="target-example.com",
                database_url=self.db_url,
                notification_url=self.notification_url,
            )
        )

        # Check output file was created
        output_path = os.path.join(self.data_dir, "output.json")
        self.assertTrue(os.path.exists(output_path))

        # Check database was created
        self.assertTrue(os.path.exists(self.db_path))

        # Verify database contains expected data
        db = Database(self.db_url)
        self.loop.run_until_complete(db.connect())
        result = self.loop.run_until_complete(
            db.fetch_all("SELECT * FROM price_history")
        )
        self.loop.run_until_complete(db.disconnect())

        # We should have at least one record for each URL (3 total)
        self.assertGreaterEqual(len(result), 3)


class TestDatabaseIntegration(BaseIntegrationTest):
    """Test database operations against a real SQLite database"""

    async def asyncSetUp(self) -> None:
        # First call the parent class setup
        await super().asyncSetUp()

        # Create and initialize the database
        self.db_manager = DatabaseManager(self.db_url)
        await self.db_manager.initialize()

        # Replace caches with non-persistent versions

        self.db_manager.price_cache = AsyncLRUCache(
            max_size=200, ttl=600, cache_name=None
        )
        self.db_manager.competitor_urls_cache = AsyncLRUCache(
            max_size=100, ttl=1800, cache_name=None
        )

    def test_insert_and_retrieve_price(self) -> None:
        """Test inserting price data and retrieving it"""
        # Test data
        test_data = {
            "product_name": "Test Product",
            "url": "https://example.com/product",
            "price": 99.99,
            "regular_price": 120.00,
            "sale_price": 99.99,
        }

        # Insert data
        self.loop.run_until_complete(self.db_manager.insert_price_data(test_data))

        # Retrieve data
        price = self.loop.run_until_complete(
            self.db_manager.get_latest_price(
                product_name="Test Product", url="https://example.com/product"
            )
        )

        # Verify data
        self.assertEqual(price, 99.99)

    def test_update_price_database(self) -> None:
        """Test updating price database and detecting changes"""
        # Initial test entry
        initial_entry = {
            "product_name": "Test Product",
            "url": "https://example.com/product",
            "source": "scrape",
            "data": {"price": 99.99, "regular_price": 120.00, "sale_price": 99.99},
        }

        # Insert initial data
        self.loop.run_until_complete(
            self.db_manager.insert_price_data(
                {
                    "product_name": initial_entry["product_name"],
                    "url": initial_entry["url"],
                    "price": initial_entry["data"]["price"],
                    "regular_price": initial_entry["data"]["regular_price"],
                    "sale_price": initial_entry["data"]["sale_price"],
                }
            )
        )

        # Updated entry with changed price
        updated_entry = {
            "product_name": "Test Product",
            "url": "https://example.com/product",
            "source": "scrape",
            "data": {
                "price": 89.99,  # Price changed
                "regular_price": 120.00,
                "sale_price": 89.99,
            },
        }

        # Test updating with changed price
        changed_urls = self.loop.run_until_complete(
            self.db_manager.update_price_database([updated_entry])
        )

        # Verify change was detected
        self.assertEqual(len(changed_urls), 1)
        self.assertEqual(
            changed_urls, {("Test Product", "https://example.com/product")}
        )

        # Verify new price was stored
        price = self.loop.run_until_complete(
            self.db_manager.get_latest_price(
                product_name="Test Product", url="https://example.com/product"
            )
        )
        self.assertEqual(price, 89.99)

    def test_get_competitor_urls(self) -> None:
        """Test getting competitor URLs"""
        # Insert test data for target site
        target_data = {
            "product_name": "Test Product",
            "url": "https://target-example.com/product",
            "price": 105.00,
        }
        self.loop.run_until_complete(self.db_manager.insert_price_data(target_data))

        # Insert test data for competitor sites
        competitor1_data = {
            "product_name": "Test Product",
            "url": "https://competitor1.com/product",
            "price": 95.99,
        }
        competitor2_data = {
            "product_name": "Test Product",
            "url": "https://competitor2.com/product",
            "price": 98.50,
        }
        self.loop.run_until_complete(
            self.db_manager.insert_price_data(competitor1_data)
        )
        self.loop.run_until_complete(
            self.db_manager.insert_price_data(competitor2_data)
        )

        # Get competitor URLs
        competitor_urls = self.loop.run_until_complete(
            self.db_manager.get_competitor_urls(
                product_name="Test Product", target_site="target-example.com"
            )
        )

        # Verify competitor URLs
        self.assertEqual(len(competitor_urls), 2)
        self.assertIn("https://competitor1.com/product", competitor_urls)
        self.assertIn("https://competitor2.com/product", competitor_urls)


class TestHTTPIntegration(BaseIntegrationTest):
    """Test fetchers against mock HTTP endpoints"""

    async def test_api_fetcher(self) -> None:
        """Test API fetcher against mock endpoint"""
        # Load the test site from config
        input_data = InputFile.from_json(self.config_path)
        api_site = next(
            site for site in input_data.sites if site.root_domain == "api-example.com"
        )

        # Create fetcher and session
        async with ClientSession() as session:
            fetcher = ApiFetcher(session, api_site)  # type: ignore

            # Override _request_with_retry to use our mock endpoints
            async def mock_request(
                url: str, **kwargs: Unpack[client._RequestOptions]
            ) -> ClientResponse:
                # Rewrite the URL to use our mock server
                mock_url = "http://localhost:8080/api"
                return await session.get(mock_url)

            with patch.object(
                ApiFetcher, "_request_with_retry", side_effect=mock_request
            ):
                # Execute fetch
                response = await fetcher.fetch(
                    url="https://api-example.com/product/123",
                    product_name="Test Product",
                )

                # Verify response
                assert "data" in response
                self.assertEqual(response["data"]["price"], 100.50)
                self.assertEqual(response["data"]["regular_price"], 120.00)
                self.assertEqual(response["data"]["sale_price"], 100.50)

    async def test_scrape_fetcher(self) -> None:
        """Test Scrape fetcher against mock endpoint"""
        # Load the test site from config
        input_data = InputFile.from_json(self.config_path)
        scrape_site = next(
            site
            for site in input_data.sites
            if site.root_domain == "scrape-example.com"
        )

        # Create fetcher and session
        async with ClientSession() as session:
            fetcher = ScrapeFetcher(session, scrape_site)  # type: ignore

            # Override _request_with_retry to use our mock endpoints
            async def mock_request(
                url: str, **kwargs: Unpack[client._RequestOptions]
            ) -> ClientResponse:
                # Rewrite the URL to use our mock server
                mock_url = "http://localhost:8080/scrape"
                return await session.get(mock_url)

            with patch.object(
                ScrapeFetcher, "_request_with_retry", side_effect=mock_request
            ):
                # Execute fetch
                response = await fetcher.fetch(
                    url="https://scrape-example.com/product/123",
                    product_name="Test Product",
                )

                # Verify response
                assert "data" in response
                self.assertEqual(response["data"]["price"], 95.99)
                self.assertEqual(response["data"]["regular_price"], 110.00)
                self.assertEqual(response["data"]["sale_price"], 95.99)


class TestNotificationIntegration(BaseIntegrationTest):
    """Test notification sending with mock notification endpoints"""

    async def asyncSetUp(self) -> None:
        # First call the parent class setup
        await super().asyncSetUp()

        # Set up the notification manager
        self.notification_mgr = NotificationManager(self.notification_url)

    async def test_send_alert(self) -> None:
        """Test sending notifications"""
        # Create session and use async with
        async with ClientSession() as session:
            # Send a test notification
            test_message = "Test Product: example.com has lower price (95.99) than target-example.com (105.00)"  # noqa: E501
            await self.notification_mgr.send_alert(session, test_message)

            # Verify notification was sent
            self.assertEqual(len(self.notifications_received), 1)
            self.assertEqual(self.notifications_received[0], test_message)

    async def test_price_change_notification(self) -> None:
        """Test complete price change and notification flow"""
        # Create and initialize the database
        db_manager = DatabaseManager(self.db_url)
        await db_manager.initialize()

        # Insert data for target site
        target_data = {
            "product_name": "Test Product",
            "url": "https://target-example.com/product",
            "price": 105.00,
        }
        await db_manager.insert_price_data(target_data)

        # Insert data for competitor site with lower price
        competitor_data = {
            "product_name": "Test Product",
            "url": "https://competitor.com/product",
            "price": 95.99,
        }
        await db_manager.insert_price_data(competitor_data)

        # Process price changes
        changed_urls = {("Test Product", "https://competitor.com/product")}
        await db_manager.process_price_changes(
            self.notification_mgr, changed_urls, "target-example.com"
        )

        # Verify notification was sent
        self.assertGreaterEqual(len(self.notifications_received), 1)
        # Check if any notification contains our expected message pattern
        has_expected_notification = any(
            "Test Product" in msg
            and "competitor.com" in msg
            and "95.99" in msg
            and "105.00" in msg
            for msg in self.notifications_received
        )
        self.assertTrue(has_expected_notification)
