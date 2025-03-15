# models.py
import json
from pathlib import Path
from typing import Literal, NotRequired, Optional, TypedDict

import tldextract
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class EnvVariables(BaseModel):
    consumer_key: str = Field(..., min_length=1)
    consumer_secret: str = Field(..., min_length=1)


class Selectors(BaseModel):
    price: Optional[str] = None
    regular_price: Optional[str] = None
    sale_price: Optional[str] = None

    def get(self, key: str, default: str = "price") -> str:
        return getattr(self, key, default)


class Site_Rules(BaseModel):
    text_contains: dict[str, bool] = Field(default_factory=dict)
    element_selector: dict[str, bool] = Field(default_factory=dict)


class SiteBase(BaseModel):
    root_domain: str
    category: Literal["api", "scrape"]
    disabled: bool = False

    @field_validator("root_domain")
    @classmethod
    def normalize_domain(cls, v: str) -> str:
        extracted = tldextract.extract(v)
        return f"{extracted.domain}.{extracted.suffix}".lower()


class ApiSite(SiteBase):
    category: Literal["api"] = "api"
    env_variables: EnvVariables


class ScrapeSite(SiteBase):
    category: Literal["scrape"] = "scrape"
    site_rules: Optional[Site_Rules] = None
    selectors: Selectors


class Product(BaseModel):
    product_name: str
    urls: list[str]


class InputFile(BaseModel):
    sites: list[ApiSite | ScrapeSite]
    products: list[Product]

    @classmethod
    def from_json(cls, json_path: Path) -> "InputFile":
        with open(json_path, encoding="utf-8") as f:
            raw_data = json.load(f)

        # First validate all sites
        validated_sites: list[ApiSite | ScrapeSite] = []
        for site_data in raw_data["sites"]:
            try:
                if site_data["category"] == "api":
                    site = ApiSite.model_validate(site_data)
                else:
                    site = ScrapeSite.model_validate(site_data)
                validated_sites.append(site)
            except ValidationError as e:
                logger.error(f"Invalid site config: {e}")
                continue

        # Get disabled domains
        disabled_domains = {
            site.root_domain for site in validated_sites if site.disabled
        }

        # Process products with filtering
        filtered_products = []
        for product_data in raw_data["products"]:
            filtered_urls = [
                url
                for url in product_data["urls"]
                if tldextract.extract(url).registered_domain not in disabled_domains
            ]

            if filtered_urls:
                filtered_products.append(
                    Product(
                        product_name=product_data["product_name"], urls=filtered_urls
                    )
                )

        return cls(
            sites=[site for site in validated_sites if not site.disabled],
            products=filtered_products,
        )


class Response(TypedDict):
    product_name: str
    url: str
    source: Literal["api", "scrape"]
    error: NotRequired[str]
    data: NotRequired[dict]
