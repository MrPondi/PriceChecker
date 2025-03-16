import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import tldextract
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from dotenv import load_dotenv

from src.core.database import DatabaseManager
from src.features.fetchers import ApiFetcher, BaseFetcher, ScrapeFetcher
from src.features.notifications import NotificationManager
from src.models import ApiSite, InputFile, Response, ScrapeSite
from src.utils.logging_config import setup_logging

logger = setup_logging()

# Constants
CONCURRENCY_LIMIT = 10
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",  # noqa: E501
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
    "Cache-Control": "no-cache",
}
TIMEOUT = ClientTimeout(total=30, sock_connect=15)


async def main(
    config_path: Path, target_site: str, database_url: str, notification_url: str
) -> None:
    input_data = InputFile.from_json(config_path)
    site_mapping = create_site_mapping(input_data.sites)
    notification_mgr = NotificationManager(notification_url)
    database_mgr = DatabaseManager(database_url)

    await database_mgr.initialize()

    connector = TCPConnector(
        force_close=False,  # Allow keep-alive
        limit=100,  # Max simultaneous connections
        limit_per_host=20,  # Connections per domain
        enable_cleanup_closed=True,  # Recycle closed connections
        use_dns_cache=True,  # Built-in DNS caching
        ttl_dns_cache=300,  # 5-minute DNS cache
    )

    try:
        async with ClientSession(
            connector=connector, headers=HEADERS, timeout=TIMEOUT, trust_env=True
        ) as session:
            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
            tasks = [
                create_task(session, semaphore, url, product.product_name, site_mapping)
                for product in input_data.products
                for url in product.urls
            ]

            results = await asyncio.gather(*filter(None, tasks), return_exceptions=True)

            # Process results
            valid_results = []
            for result in results:
                if not result:
                    continue
                if isinstance(result, BaseException):
                    logger.error(f"Task failed: {str(result)}")
                    continue
                valid_results.append(result)

            save_results(valid_results, config_path.parent)
            changed_urls = await database_mgr.update_price_database(valid_results)

            if changed_urls:
                await database_mgr.process_price_changes(
                    notification_mgr, changed_urls, target_site
                )
            else:
                logger.info("No price changes detected")
    finally:
        # Save rate limits to file
        rate_limiter = BaseFetcher.get_rate_limiter()
        await rate_limiter.save_configs()
        logger.info("Rate limits saved to file")

        # Cancel any ongoing cache cleanup tasks
        if hasattr(database_mgr, "price_cache") and hasattr(
            database_mgr.price_cache, "_cleanup_task"
        ):
            if (
                database_mgr.price_cache._cleanup_task
                and not database_mgr.price_cache._cleanup_task.done()
            ):
                database_mgr.price_cache._cleanup_task.cancel()

        if hasattr(database_mgr, "competitor_urls_cache") and hasattr(
            database_mgr.competitor_urls_cache, "_cleanup_task"
        ):
            if (
                database_mgr.competitor_urls_cache._cleanup_task
                and not database_mgr.competitor_urls_cache._cleanup_task.done()
            ):
                database_mgr.competitor_urls_cache._cleanup_task.cancel()


def create_site_mapping(
    sites: list[ApiSite | ScrapeSite],
) -> dict[str, ApiSite | ScrapeSite]:
    return {site.root_domain: site for site in sites if not site.disabled}


async def create_task(
    session: ClientSession,
    semaphore: asyncio.Semaphore,
    url: str,
    product_name: str,
    site_mapping: dict[str, ApiSite | ScrapeSite],
) -> Optional[Response]:
    parsed_domain = tldextract.extract(url).registered_domain
    if not (site := site_mapping.get(parsed_domain)):
        logger.error(f"No configuration found for {url}")
        return None

    async with semaphore:
        fetcher = (ApiFetcher if isinstance(site, ApiSite) else ScrapeFetcher)(
            session,
            site,  # type: ignore
        )
        return await fetcher.fetch(url=url, product_name=product_name)


def save_results(results: list[dict], dir_path: Path) -> None:
    """Save results with incremental JSON writing"""
    with open(dir_path / "output.json", "w") as outfile:
        json.dump(results, outfile, indent=4, ensure_ascii=False)


def cli() -> None:
    """Command Line Interface entry point"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Load environment variables from .env file if present
    load_dotenv()

    parser = argparse.ArgumentParser(description="Price monitoring system")
    parser.add_argument("-c", "--config", default="data/input.json")
    parser.add_argument("--target-site", help="Target site URL")
    parser.add_argument("--database-url", help="Database connection URL")
    parser.add_argument("--notification-url", help="Notification service URL")

    args = parser.parse_args()

    # Get values from CLI arguments or environment variables
    target_site = args.target_site or os.getenv("TARGET_SITE")
    database_url = args.database_url or os.getenv("DATABASE_URL", "sqlite:///data/product_prices.db")
    notification_url = args.notification_url or os.getenv("NOTIFICATION_URL")

    # Validate required parameters
    missing = []
    if not target_site:
        missing.append("TARGET_SITE")
    if not notification_url:
        missing.append("NOTIFICATION_URL")

    if not target_site or not notification_url:
        parser.error(
            f"Missing required parameters: {', '.join(missing)}. "
            + "Provide via CLI arguments or environment variables."
        )

    asyncio.run(
        main(
            config_path=Path(args.config),
            target_site=target_site,
            database_url=database_url,
            notification_url=notification_url,
        )
    )

if __name__ == "__main__":
    cli()
