import json
import tempfile
from pathlib import Path
from unittest import TestCase

import pytest
from pydantic import ValidationError

from src.models import (
    ApiSite,
    EnvVariables,
    InputFile,
    Product,
    ScrapeSite,
    Selectors,
    Site_Rules,
)


class TestEnvVariables(TestCase):
    def test_valid_env_variables(self) -> None:
        """Test valid environment variables validation"""
        env = EnvVariables(consumer_key="key123", consumer_secret="secret456")
        self.assertEqual(env.consumer_key, "key123")
        self.assertEqual(env.consumer_secret, "secret456")

    def test_invalid_env_variables(self) -> None:
        """Test environment variables validation failure"""
        with self.assertRaises(ValidationError):
            EnvVariables(consumer_key="", consumer_secret="secret456")
        with self.assertRaises(ValidationError):
            EnvVariables(consumer_key="key123", consumer_secret="")


class TestSelectors(TestCase):
    def test_selectors_with_values(self) -> None:
        selectors = Selectors(
            price=".price", regular_price=".regular-price", sale_price=".sale-price"
        )
        self.assertEqual(selectors.price, ".price")
        self.assertEqual(selectors.regular_price, ".regular-price")
        self.assertEqual(selectors.sale_price, ".sale-price")

    def test_selectors_get(self) -> None:
        """Test the get method of Selectors class"""
        # With all fields set
        selectors1 = Selectors(
            price=".price", regular_price=".regular-price", sale_price=".sale-price"
        )
        self.assertEqual(selectors1.get("price"), ".price")
        self.assertEqual(selectors1.get("regular_price"), ".regular-price")
        self.assertEqual(selectors1.get("sale_price"), ".sale-price")

        # With some fields not set
        selectors2 = Selectors(price=".price")
        self.assertEqual(
            selectors2.get("non_existent", "custom_default"), "custom_default"
        )
        self.assertEqual(selectors2.get("price"), ".price")
        self.assertEqual(selectors2.get("sale_price", "price"), "price")
        self.assertIsNone(selectors2.get("regular_price"))


class TestSiteRules(TestCase):
    def test_site_rules_defaults(self) -> None:
        rules = Site_Rules()
        self.assertEqual(rules.text_contains, {})
        self.assertEqual(rules.element_selector, {})

    def test_site_rules_with_values(self) -> None:
        rules = Site_Rules(
            text_contains={"out of stock": False, "available": True},
            element_selector={".discount": True, ".old-price": False},
        )
        self.assertEqual(
            rules.text_contains, {"out of stock": False, "available": True}
        )
        self.assertEqual(
            rules.element_selector, {".discount": True, ".old-price": False}
        )


class TestSiteBase(TestCase):
    def test_normalize_domain(self) -> None:
        # Test with clean domain
        api_site = ApiSite(
            root_domain="example.com",
            env_variables=EnvVariables(consumer_key="key", consumer_secret="secret"),
        )
        self.assertEqual(api_site.root_domain, "example.com")

        # Test with URL
        api_site = ApiSite(
            root_domain="https://www.example.com/path",
            env_variables=EnvVariables(consumer_key="key", consumer_secret="secret"),
        )
        self.assertEqual(api_site.root_domain, "example.com")

        # Test with subdomain
        api_site = ApiSite(
            root_domain="sub.example.co.uk",
            env_variables=EnvVariables(consumer_key="key", consumer_secret="secret"),
        )
        self.assertEqual(api_site.root_domain, "example.co.uk")


class TestApiSite(TestCase):
    def test_valid_api_site(self) -> None:
        site = ApiSite(
            root_domain="api.example.com",
            env_variables=EnvVariables(consumer_key="key", consumer_secret="secret"),
        )
        self.assertEqual(site.root_domain, "example.com")
        self.assertEqual(site.category, "api")
        self.assertEqual(site.disabled, False)
        self.assertEqual(site.env_variables.consumer_key, "key")
        self.assertEqual(site.env_variables.consumer_secret, "secret")


class TestScrapeSite(TestCase):
    def test_valid_scrape_site_minimal(self) -> None:
        site = ScrapeSite(
            root_domain="example.com",
            selectors=Selectors(price=".price"),
        )
        self.assertEqual(site.root_domain, "example.com")
        self.assertEqual(site.category, "scrape")
        self.assertEqual(site.disabled, False)
        self.assertEqual(site.selectors.price, ".price")
        self.assertIsNone(site.site_rules)

    def test_valid_scrape_site_complete(self) -> None:
        site = ScrapeSite(
            root_domain="example.com",
            selectors=Selectors(
                price=".price", regular_price=".regular-price", sale_price=".sale-price"
            ),
            site_rules=Site_Rules(
                text_contains={"sale": True}, element_selector={".out-of-stock": False}
            ),
            disabled=True,
        )
        self.assertEqual(site.root_domain, "example.com")
        self.assertEqual(site.category, "scrape")
        self.assertTrue(site.disabled)
        self.assertEqual(site.selectors.price, ".price")
        assert site.site_rules is not None
        self.assertEqual(site.site_rules.text_contains, {"sale": True})
        self.assertEqual(site.site_rules.element_selector, {".out-of-stock": False})


class TestProduct(TestCase):
    def test_valid_product(self) -> None:
        product = Product(
            product_name="Test Product",
            urls=["https://example.com/product", "https://other.com/product"],
        )
        self.assertEqual(product.product_name, "Test Product")
        self.assertEqual(
            product.urls, ["https://example.com/product", "https://other.com/product"]
        )


class TestInputFile:
    @pytest.fixture
    def valid_config(self) -> dict:
        return {
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
                        "price": ".price",
                        "regular_price": ".regular-price",
                        "sale_price": ".sale-price",
                    },
                },
                {
                    "root_domain": "disabled-example.com",
                    "category": "scrape",
                    "disabled": True,
                    "selectors": {"price": ".price"},
                },
            ],
            "products": [
                {
                    "product_name": "Product A",
                    "urls": [
                        "https://api-example.com/product-a",
                        "https://scrape-example.com/product-a",
                        "https://disabled-example.com/product-a",
                    ],
                },
                {
                    "product_name": "Product B",
                    "urls": ["https://scrape-example.com/product-b"],
                },
            ],
        }

    def test_from_json(self, valid_config: dict) -> None:
        # Create a temporary file with the test configuration
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as temp_file:
            json.dump(valid_config, temp_file)
            temp_file_path = temp_file.name

        try:
            # Test loading from the temporary file
            input_file = InputFile.from_json(Path(temp_file_path))

            # Verify sites
            self.verify_sites(input_file)

            # Verify products
            self.verify_products(input_file)

        finally:
            # Cleanup
            Path(temp_file_path).unlink(missing_ok=True)

    def verify_sites(self, input_file: InputFile) -> None:
        # Check that we have the expected number of sites (excluding disabled)
        assert len(input_file.sites) == 2

        # Verify first site (API)
        api_site = input_file.sites[0]
        assert isinstance(api_site, ApiSite)
        assert api_site.root_domain == "api-example.com"
        assert api_site.category == "api"
        assert api_site.env_variables.consumer_key == "test_key"
        assert api_site.env_variables.consumer_secret == "test_secret"

        # Verify second site (Scrape)
        scrape_site = input_file.sites[1]
        assert isinstance(scrape_site, ScrapeSite)
        assert scrape_site.root_domain == "scrape-example.com"
        assert scrape_site.category == "scrape"
        assert scrape_site.selectors.price == ".price"
        assert scrape_site.selectors.regular_price == ".regular-price"
        assert scrape_site.selectors.sale_price == ".sale-price"

    def verify_products(self, input_file: InputFile) -> None:
        # Check that we have the expected number of products
        assert len(input_file.products) == 2

        # Verify first product with filtered URLs (disabled site removed)
        product_a = input_file.products[0]
        assert product_a.product_name == "Product A"
        assert len(product_a.urls) == 2
        assert "https://api-example.com/product-a" in product_a.urls
        assert "https://scrape-example.com/product-a" in product_a.urls
        assert "https://disabled.-xample.com/product-a" not in product_a.urls

        # Verify second product
        product_b = input_file.products[1]
        assert product_b.product_name == "Product B"
        assert len(product_b.urls) == 1
        assert "https://scrape-example.com/product-b" in product_b.urls

    def test_from_json_with_invalid_site(
        self, valid_config: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Add an invalid site
        valid_config["sites"].append(
            {
                "root_domain": "invalid-example.com",
                "category": "api",  # Missing required env_variables
            }
        )

        # Create a temporary file with the modified configuration
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as temp_file:
            json.dump(valid_config, temp_file)
            temp_file_path = temp_file.name

        try:
            # Test loading from the temporary file
            input_file = InputFile.from_json(Path(temp_file_path))

            # Verify only valid sites are included
            assert len(input_file.sites) == 2
            domains = [site.root_domain for site in input_file.sites]
            assert "invalid-example.com" not in domains

            # Verify error was logged
            assert "Invalid site config" in caplog.text

        finally:
            # Cleanup
            Path(temp_file_path).unlink(missing_ok=True)
