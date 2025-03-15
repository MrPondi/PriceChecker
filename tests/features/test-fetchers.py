import logging
from collections.abc import Generator
from typing import Any, Optional
from unittest.mock import MagicMock, Mock, patch

import aiohttp
import pytest
from bs4 import BeautifulSoup

from src.features.fetchers import ApiFetcher, BaseFetcher, FetcherError, ScrapeFetcher
from src.models import ApiSite, EnvVariables, ScrapeSite, Selectors, Site_Rules


@pytest.fixture
def mock_session() -> MagicMock:
    """Create a mock aiohttp client session"""
    return MagicMock(spec=aiohttp.ClientSession)


@pytest.fixture
def api_site() -> ApiSite:
    """Create a sample API site configuration"""
    return ApiSite(
        root_domain="api-example.com",
        category="api",
        env_variables=EnvVariables(
            consumer_key="test_key", consumer_secret="test_secret"
        ),
    )


@pytest.fixture
def scrape_site() -> ScrapeSite:
    """Create a sample Scrape site configuration"""
    return ScrapeSite(
        root_domain="scrape-example.com",
        category="scrape",
        selectors=Selectors(
            price=".product-price",
            regular_price=".regular-price",
            sale_price=".sale-price",
        ),
        site_rules=Site_Rules(
            text_contains={"Out of stock": False}, element_selector={"sold-out": True}
        ),
    )


class MockResponse:
    """Mock aiohttp response for testing fetchers"""

    def __init__(
        self,
        status: int,
        json_data: Optional[dict] = None,
        text_data: Optional[str] = None,
        raise_error: Optional[BaseException] = None,
    ) -> None:
        self.status = status
        self._json_data = json_data or {}
        self._text_data = text_data or ""
        self._raise_error = raise_error

    async def json(self) -> dict:
        if self._raise_error:
            raise self._raise_error
        return self._json_data

    async def text(self) -> str:
        if self._raise_error:
            raise self._raise_error
        return self._text_data

    def __await__(self) -> Generator[Any, None, "MockResponse"]:
        async def _await_mock() -> "MockResponse":
            return self

        return _await_mock().__await__()

class TestBaseFetcher:
    @pytest.mark.asyncio
    async def test_request_with_retry_success(self, mock_session: MagicMock) -> None:
        """Test successful request with retry logic"""
        # Setup
        base_fetcher = BaseFetcher(mock_session)
        mock_response = MockResponse(status=200)
        mock_session.get.return_value = mock_response

        # Mock rate limiter to skip actual delays
        with (
            patch.object(base_fetcher.rate_limiter, "acquire"),
            patch.object(base_fetcher.rate_limiter, "update_rate"),
        ):
            # Execute
            response = await base_fetcher._request_with_retry("https://example.com")

            # Assert
            assert response == mock_response
            mock_session.get.assert_called_once_with("https://example.com")

    @pytest.mark.asyncio
    async def test_request_with_retry_rate_limited(
        self, mock_session: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test retry when rate limited"""
        # Setup
        caplog.set_level(logging.WARNING)
        base_fetcher = BaseFetcher(mock_session)
        responses = [MockResponse(status=429), MockResponse(status=200)]
        mock_session.get.side_effect = responses

        # Mock rate limiter to skip actual delays
        with (
            patch.object(base_fetcher.rate_limiter, "acquire"),
            patch.object(base_fetcher.rate_limiter, "update_rate"),
            patch("asyncio.sleep", return_value=None),
        ):  # Skip sleep
            # Execute
            await base_fetcher._request_with_retry("https://example.com")

            # Assert
            assert "Rate limited by example.com" in caplog.text
            assert mock_session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_request_with_retry_error(self, mock_session: MagicMock) -> None:
        """Test error handling during request"""
        # Setup
        base_fetcher = BaseFetcher(mock_session)
        mock_session.get.side_effect = aiohttp.ClientError("Connection error")

        # Mock rate limiter to skip actual delays
        with (
            patch.object(base_fetcher.rate_limiter, "acquire"),
            patch.object(base_fetcher.rate_limiter, "update_rate"),
            patch("asyncio.sleep", return_value=None),
        ):  # Skip sleep
            # Execute & Assert
            with pytest.raises(FetcherError) as exc_info:
                await base_fetcher._request_with_retry("https://example.com")

            assert "Connection error" in str(exc_info.value)
            assert mock_session.get.call_count > 1  # Should have retried


class TestApiFetcher:
    @pytest.mark.asyncio
    async def test_fetch_successful(
        self, mock_session: MagicMock, api_site: ApiSite
    ) -> None:
        """Test successful API fetch with valid price data"""
        # Setup
        api_fetcher = ApiFetcher(mock_session, api_site)
        mock_response = MockResponse(
            status=200,
            json_data={
                "price": "12.99",
                "regular_price": "15.99",
                "sale_price": "12.99",
            },
        )
        mock_session.get.return_value = mock_response

        # Mock request_with_retry to return our mock response
        with patch.object(
            api_fetcher, "_request_with_retry", return_value=mock_response
        ):
            # Execute
            result = await api_fetcher.fetch(
                url="https://api-example.com/product/123", product_name="Test Product"
            )

            # Assert
            assert result["product_name"] == "Test Product"
            assert result["url"] == "https://api-example.com/product/123"
            assert result["source"] == "api"
            assert "data" in result
            assert result["data"]["price"] == 12.99
            assert result["data"]["regular_price"] == 15.99
            assert result["data"]["sale_price"] == 12.99

    @pytest.mark.asyncio
    async def test_fetch_invalid_price(
        self, mock_session: MagicMock, api_site: ApiSite
    ) -> None:
        """Test API fetch with invalid price data"""
        # Setup
        api_fetcher = ApiFetcher(mock_session, api_site)
        mock_response = MockResponse(
            status=200,
            json_data={
                "price": "not a price",
                "regular_price": "invalid",
            },
        )
        mock_session.get.return_value = mock_response

        # Mock request_with_retry to return our mock response
        with patch.object(
            api_fetcher, "_request_with_retry", return_value=mock_response
        ):
            # Execute
            result = await api_fetcher.fetch(
                url="https://api-example.com/product/123", product_name="Test Product"
            )

            # Assert
            assert result["product_name"] == "Test Product"
            assert result["url"] == "https://api-example.com/product/123"
            assert result["source"] == "api"
            assert "error" in result
            assert "No valid price found" in result["error"]

    @pytest.mark.asyncio
    async def test_fetch_api_error(
        self, mock_session: MagicMock, api_site: ApiSite
    ) -> None:
        """Test API fetch with request error"""
        # Setup
        api_fetcher = ApiFetcher(mock_session, api_site)

        # Mock _request_with_retry to raise an exception
        with patch.object(
            api_fetcher, "_request_with_retry", side_effect=FetcherError("API Error")
        ):
            # Execute
            result = await api_fetcher.fetch(
                url="https://api-example.com/product/123", product_name="Test Product"
            )

            # Assert
            assert result["product_name"] == "Test Product"
            assert result["url"] == "https://api-example.com/product/123"
            assert result["source"] == "api"
            assert "error" in result
            assert "API Error" in result["error"]


class TestScrapeFetcher:
    @pytest.mark.asyncio
    async def test_fetch_successful(
        self, mock_session: MagicMock, scrape_site: ScrapeSite
    ) -> None:
        """Test successful HTML scraping with valid price data"""
        # Setup
        html_content = """
        <html>
            <body>
                <div class="product-price">$49.99</div>
                <div class="regular-price">$59.99</div>
                <div class="sale-price">$49.99</div>
            </body>
        </html>
        """
        scrape_fetcher = ScrapeFetcher(mock_session, scrape_site)
        mock_response = MockResponse(status=200, text_data=html_content)
        mock_session.get.return_value = mock_response

        # Mock request_with_retry to return our mock response
        with (
            patch.object(
                scrape_fetcher, "_request_with_retry", return_value=mock_response
            ),
            patch(
                "src.core.cache.async_cached", lambda ttl, max_size: lambda func: func
            ),
        ):  # Disable caching
            # Execute
            result = await scrape_fetcher.fetch(
                url="https://scrape-example.com/product/123",
                product_name="Test Product",
            )

            # Assert
            assert result["product_name"] == "Test Product"
            assert result["url"] == "https://scrape-example.com/product/123"
            assert result["source"] == "scrape"
            assert "data" in result
            assert result["data"]["price"] == 49.99
            assert result["data"]["regular_price"] == 59.99
            assert result["data"]["sale_price"] == 49.99

    @pytest.mark.asyncio
    async def test_fetch_no_price(
        self, mock_session: MagicMock, scrape_site: ScrapeSite
    ) -> None:
        """Test HTML scraping with no valid price data"""
        # Setup
        html_content = """
        <html>
            <body>
                <div class="product-price">Out of stock</div>
                <div class="regular-price">N/A</div>
            </body>
        </html>
        """
        scrape_fetcher = ScrapeFetcher(mock_session, scrape_site)
        mock_response = MockResponse(status=200, text_data=html_content)
        mock_session.get.return_value = mock_response

        # Mock request_with_retry to return our mock response
        with (
            patch.object(
                scrape_fetcher, "_request_with_retry", return_value=mock_response
            ),
            patch(
                "src.core.cache.async_cached", lambda ttl, max_size: lambda func: func
            ),
        ):  # Disable caching
            # Execute
            result = await scrape_fetcher.fetch(
                url="https://scrape-example.com/product/123",
                product_name="Test Product",
            )

            # Assert
            assert result["product_name"] == "Test Product"
            assert result["url"] == "https://scrape-example.com/product/123"
            assert result["source"] == "scrape"
            assert "error" in result
            assert "No valid prices found" in result["error"]

    @pytest.mark.asyncio
    async def test_extract_price(self, scrape_site: ScrapeSite) -> None:
        """Test price extraction from HTML elements"""
        # Setup
        scrape_fetcher = ScrapeFetcher(Mock(), scrape_site)

        # Case 1: Valid price
        html1 = '<div class="price">$49.99</div>'
        soup1 = BeautifulSoup(html1, "lxml")
        elements1 = soup1.select("div")

        # Case 2: Multiple prices
        html2 = '<div class="price">$49.99</div><div class="price">$39.99</div>'
        soup2 = BeautifulSoup(html2, "lxml")
        elements2 = soup2.select("div")

        # Case 3: No valid price
        html3 = '<div class="price">Call for price</div>'
        soup3 = BeautifulSoup(html3, "lxml")
        elements3 = soup3.select("div")

        # Execute & Assert
        assert (
            scrape_fetcher._extract_price(elements1, "https://example.com", "price")
            == 49.99
        )
        assert (
            scrape_fetcher._extract_price(elements2, "https://example.com", "price")
            == 39.99
        )  # Should get lowest
        assert (
            scrape_fetcher._extract_price(elements3, "https://example.com", "price")
            is None
        )

    def test_should_skip_element(self, scrape_site: ScrapeSite) -> None:
        """Test logic for skipping elements based on site rules"""
        # Setup
        scrape_fetcher = ScrapeFetcher(Mock(), scrape_site)

        # Case 1: Text contains rule match (should skip)
        html1 = "<div>Out of stock</div>"
        soup1 = BeautifulSoup(html1, "lxml")
        element1 = soup1.div

        # Case 2: Element selector rule match (should skip)
        html2 = '<div>Price: $49.99 <span class="sold-out">Sold Out</span></div>'
        soup2 = BeautifulSoup(html2, "lxml")
        element2 = soup2.div

        # Case 3: No rule match (should not skip)
        html3 = "<div>Price: $49.99</div>"
        soup3 = BeautifulSoup(html3, "lxml")
        element3 = soup3.div

        # Execute & Assert
        assert scrape_fetcher._should_skip_element(element1) is True  # type: ignore
        assert scrape_fetcher._should_skip_element(element2) is True  # type: ignore
        assert scrape_fetcher._should_skip_element(element3) is False  # type: ignore
