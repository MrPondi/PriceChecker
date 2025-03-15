# rate_limiter.py
import asyncio
import json
import os
import time
from pathlib import Path

from aiolimiter import AsyncLimiter

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class DomainRateLimiter:
    """Domain-specific rate limiting with persistent configuration"""

    def __init__(self, config_path: str = "data/rate_limits.json") -> None:
        # Default limits
        self.default_rate = 5
        self.default_period = 1.0  # seconds

        # Path to the configuration file
        self.config_path = config_path
        self.config_dir = os.path.dirname(config_path)

        # Store limiters for each domain
        self.limiters: dict[str, AsyncLimiter] = {}
        self.domain_configs: dict[str, tuple[float, float]] = {}

        # Load existing configurations
        self._load_configs()

        # Track last request time for adaptive backoff
        self.last_request_time: dict[str, float] = {}

        # Track domain success rates
        self.success_counts: dict[str, int] = {}
        self.failure_counts: dict[str, int] = {}

        # Flag to indicate if configs have been modified
        self.configs_modified = False

    def _load_configs(self) -> None:
        """Load rate limit configurations from file"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path) as f:
                    loaded_configs = json.load(f)

                    # Convert string keys to tuples of floats
                    for domain, config in loaded_configs.items():
                        if isinstance(config, list) and len(config) == 2:
                            self.domain_configs[domain] = (
                                float(config[0]),
                                float(config[1]),
                            )

                logger.info(
                    f"Loaded rate limits for {len(self.domain_configs)} domains"
                )
            else:
                logger.info("No rate limit configuration file found, using defaults")
        except Exception as e:
            logger.error(f"Error loading rate limit configurations: {str(e)}")

    async def save_configs(self) -> None:
        """Save current rate limit configurations to file"""
        if not self.configs_modified:
            logger.info("Ratelimit config not modified")
            return

        try:
            # Create directory if it doesn't exist
            Path(self.config_dir).mkdir(exist_ok=True)

            # Save configuration
            with open(self.config_path, "w") as f:
                json.dump(self.domain_configs, f, indent=2)

            logger.info(f"Saved rate limits for {len(self.domain_configs)} domains")
            self.configs_modified = False
        except Exception as e:
            logger.error(f"Error saving rate limit configurations: {str(e)}")

    def get_limiter(self, domain: str) -> AsyncLimiter:
        """Get or create a rate limiter for a specific domain"""
        if domain not in self.limiters:
            # Get config for this domain or use default
            rate, period = self.domain_configs.get(
                domain, (self.default_rate, self.default_period)
            )
            self.limiters[domain] = AsyncLimiter(rate, period)

            # Initialize success/failure counts
            if domain not in self.success_counts:
                self.success_counts[domain] = 0
                self.failure_counts[domain] = 0

            logger.debug(f"Created rate limiter for {domain}: {rate} req/{period}s")

        return self.limiters[domain]

    async def acquire(self, domain: str) -> None:
        """Acquire permission to make a request to the domain"""
        limiter = self.get_limiter(domain)

        # Check if we need additional adaptive delay
        now = time.time()
        if domain in self.last_request_time:
            time_since_last = now - self.last_request_time[domain]
            if time_since_last < 0.1:  # If less than 100ms since last request
                await asyncio.sleep(0.1 - time_since_last)  # Add small delay

        # Acquire from the limiter
        await limiter.acquire()
        self.last_request_time[domain] = time.time()

    def update_rate(self, domain: str, success: bool) -> None:
        """Dynamically adjust rate limits based on request success"""
        if domain not in self.limiters:
            return

        # Update success/failure counts
        if success:
            self.success_counts[domain] += 1
        else:
            self.failure_counts[domain] += 1

        # Only adjust after we have sufficient data
        total_requests = self.success_counts[domain] + self.failure_counts[domain]
        if total_requests < 10:
            return

        # Calculate success rate
        success_rate = self.success_counts[domain] / total_requests

        # Get current configuration
        current_limiter = self.limiters[domain]
        current_rate = current_limiter.max_rate
        current_period = current_limiter.time_period

        # Adjust rate based on success rate
        if not success or success_rate < 0.7:  # If failing or success rate is poor
            # Reduce rate by 25%
            new_rate = max(1, current_rate * 0.75)
            if new_rate != current_rate:
                new_rate = round(new_rate, 1)
                self.limiters[domain] = AsyncLimiter(new_rate, current_period)
                self.domain_configs[domain] = (new_rate, current_period)
                self.configs_modified = True
                logger.warning(
                    f"Reducing rate for {domain} to {new_rate} req/{current_period}s"
                )
        elif success_rate > 0.95 and current_rate < 10:
            # Gradually increase rate if very successful
            new_rate = min(10, current_rate * 1.1)
            if new_rate != current_rate:
                new_rate = round(new_rate, 1)
                self.limiters[domain] = AsyncLimiter(new_rate, current_period)
                self.domain_configs[domain] = (new_rate, current_period)
                self.configs_modified = True
                logger.info(
                    f"Increasing rate for {domain} to {new_rate} req/{current_period}s"
                )

        # Reset counters periodically
        if total_requests >= 50:
            self.success_counts[domain] = int(self.success_counts[domain] * 0.5)
            self.failure_counts[domain] = int(self.failure_counts[domain] * 0.5)
