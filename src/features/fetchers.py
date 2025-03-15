# fetchers.py
import asyncio
import logging
import re
from typing import Any, Optional, Unpack

import tldextract
from aiohttp import BasicAuth, ClientResponse, ClientSession
from aiohttp.client import _RequestOptions
from bs4 import BeautifulSoup, Tag

from src.core.cache import async_cached
from src.core.rate_limiter import DomainRateLimiter
from src.models import ApiSite, Response, ScrapeSite
from src.utils.logging_config import get_logger

logger = get_logger(__name__)
price_regex = re.compile(r"[^\d.,]")


class FetcherError(Exception):
    """Base exception for fetcher errors"""


class BaseFetcher:
    # Shared rate limiter instance
    _rate_limiter = DomainRateLimiter()

    @classmethod
    def get_rate_limiter(cls) -> DomainRateLimiter:
        return cls._rate_limiter

    def __init__(self, session: ClientSession) -> None:
        self.session = session
        self.retries = 3
        self.backoff_base = 2
        self.rate_limiter = self.get_rate_limiter()

    async def _request_with_retry(
        self, url: str, **kwargs: Unpack[_RequestOptions]
    ) -> ClientResponse:
        domain = tldextract.extract(url).registered_domain

        for attempt in range(self.retries):
            # Use rate limiter to control request rate
            await self.rate_limiter.acquire(domain)

            success = False

            try:
                response = await self.session.get(url, **kwargs)
                success = 200 <= response.status < 300
                if success:
                    return response

                if response.status == 429:  # Too Many Requests
                    logger.warning(f"Rate limited by {domain}")

                await self._handle_error_response(response, attempt)
            except Exception as e:
                await self._handle_request_error(e, attempt, url)
            finally:
                # Update rate limiter with request result
                self.rate_limiter.update_rate(domain, success)
        raise FetcherError(f"Failed after {self.retries} retries for {url}")

    async def _handle_error_response(
        self, response: ClientResponse, attempt: int
    ) -> None:
        if attempt == self.retries - 1:
            error_text = await response.text()
            raise FetcherError(f"HTTP {response.status}: {error_text[:200]}")
        await asyncio.sleep(self.backoff_base**attempt)

    async def _handle_request_error(
        self, error: Exception, attempt: int, url: str
    ) -> None:
        if type(error) is asyncio.TimeoutError:
            logger.warning(f"Attempt {attempt + 1} got timed out for {url}")
        else:
            logger.warning(f"Attempt {attempt + 1} failed for {url}: {str(error)}")
        if attempt == self.retries - 1:
            raise FetcherError(f"Request failed: {str(error)}") from error
        await asyncio.sleep(self.backoff_base**attempt)


class ApiFetcher(BaseFetcher):
    def __init__(self, session: ClientSession, site: ApiSite) -> None:
        super().__init__(session)
        self.site = site
        self.auth = BasicAuth(
            site.env_variables.consumer_key,
            site.env_variables.consumer_secret,
        )

    async def fetch(self, url: str, product_name: str) -> Response:
        try:
            response = await self._request_with_retry(url, auth=self.auth)
            data = await response.json()
            return self._format_response(data, product_name, url)
        except FetcherError as e:
            return self._error_response(product_name, url, str(e))

    def _format_response(
        self, data: dict[str, Any], product_name: str, url: str
    ) -> Response:
        """Format API response with price validation"""
        price_fields = ["price", "regular_price", "sale_price"]
        prices = {}

        for field in price_fields:
            value = data.get(field)
            if value and re.match(r"^\d+\.?\d*$", str(value)):
                try:
                    prices[field] = float(value)
                except ValueError:
                    continue

        if not prices.get("price"):
            return self._error_response(
                product_name, url, "No valid price found in API response"
            )

        return {
            "product_name": product_name,
            "url": url,
            "source": self.site.category,
            "data": prices,
        }

    def _error_response(self, product_name: str, url: str, error: str) -> Response:
        return {
            "product_name": product_name,
            "url": url,
            "source": self.site.category,
            "error": error,
        }


class ScrapeFetcher(BaseFetcher):
    def __init__(self, session: ClientSession, site: ScrapeSite) -> None:
        super().__init__(session)
        self.site = site
        self.selectors = site.selectors

    @async_cached(ttl=300, max_size=50)
    async def fetch(self, url: str, product_name: str) -> Response:
        try:
            response = await self._request_with_retry(url)
            html = await response.text()
            return self._parse_html(html, product_name, url)
        except FetcherError as e:
            return self._error_response(product_name, url, str(e))

    def _parse_html(self, html: str, product_name: str, url: str) -> Response:
        """Parse HTML and extract prices using configured selectors"""
        soup = BeautifulSoup(html, "lxml")
        price_data = {}

        def _extract_price_data(price_type: str) -> Optional[float]:
            """Extract price data for a given price type if selector exists"""
            if selector := self.selectors.get(price_type):
                elements = soup.select(selector)
                return self._extract_price(elements, url, price_type)
            return None

        # Process regular and sale prices
        for pt in ("regular_price", "sale_price"):
            if price_value := _extract_price_data(pt):
                price_data[pt] = price_value

        # Process generic price only if no complete pair exists
        if not all(price_data.get(k) for k in ("regular_price", "sale_price")):
            if price_value := _extract_price_data("price"):
                price_data.setdefault("price", price_value)
                price_data.setdefault("regular_price", price_value)
        else:
            price_data.setdefault("price", price_data["sale_price"])

        if not price_data.get("price"):
            logging.error(f"No valid prices found for {product_name} at {url}")
            return self._error_response(
                product_name, url, "No valid prices found in HTML"
            )

        return {
            "product_name": product_name,
            "url": url,
            "source": self.site.category,
            "data": price_data,
        }

    def _extract_price(
        self, elements: list, url: str, price_type: str
    ) -> Optional[float]:
        """Extract and validate price from HTML elements"""
        prices = []
        for el in elements:
            if self._should_skip_element(el):
                continue
            price_text = price_regex.sub(
                "", el.get_text(strip=True).replace(" ", "").replace(",", ".")
            )
            if not price_text:
                continue

            try:
                # Handle decimal/currency formatting
                parts = price_text.rsplit(".", 1)
                integer_part = parts[0].replace(".", "") if parts[0] else "0"
                decimal_part = parts[1] if len(parts) > 1 else ""
                price = float(
                    f"{integer_part}.{decimal_part}" if decimal_part else integer_part
                )
                prices.append(price)
            except ValueError:
                continue

        if not prices:
            logger.debug(f"No valid {price_type} found at {url}, {elements}")
            return None

        if len(set(prices)) > 1:
            logger.warning(f"Multiple prices found: {prices} at {url} ({price_type})")

        # Return lowest price if multiple found
        return min(prices)

    def _should_skip_element(self, element: Tag) -> bool:
        """Determine if an element should be skipped based on site-specific rules."""
        element_text = element.get_text(strip=True)

        # Check if site has site specific rules
        if not self.site.site_rules:
            return False

        # Apply text content rules
        if self.site.site_rules.text_contains is not None:
            for term, should_include in self.site.site_rules.text_contains.items():
                contains = term in element_text
                if contains != should_include:
                    return True

        # Apply element selector rules
        if self.site.site_rules.element_selector is not None:
            for selector, should_skip in self.site.site_rules.element_selector.items():
                if element.find(class_=selector) is not None and should_skip:
                    return True

        # Default: don't skip
        return False

    def _error_response(self, product_name: str, url: str, error: str) -> Response:
        return {
            "product_name": product_name,
            "url": url,
            "source": self.site.category,
            "error": error,
        }
