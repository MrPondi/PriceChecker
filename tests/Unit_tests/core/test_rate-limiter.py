import json
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.core.rate_limiter import DomainRateLimiter


@pytest.fixture
def temp_config_file() -> Iterator[str]:
    """Create a temporary config file for testing."""
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmp:
        json.dump({"example.com": [5, 1.0], "test.com": [2, 1.0]}, tmp)
        tmp_name = tmp.name

    yield tmp_name

    # Clean up
    Path(tmp_name).unlink(missing_ok=True)


@pytest.fixture
def rate_limiter(temp_config_file: str) -> DomainRateLimiter:
    """Create a rate limiter with a test config file."""
    return DomainRateLimiter(config_path=temp_config_file)


@pytest.mark.asyncio
async def test_rate_limiter_init(
    rate_limiter: DomainRateLimiter, temp_config_file: str
) -> None:
    """Test rate limiter initialization."""
    assert rate_limiter.config_path == temp_config_file
    assert "example.com" in rate_limiter.domain_configs
    assert "test.com" in rate_limiter.domain_configs
    assert rate_limiter.domain_configs["example.com"] == (5, 1.0)
    assert rate_limiter.domain_configs["test.com"] == (2, 1.0)


@pytest.mark.asyncio
async def test_get_limiter(rate_limiter: DomainRateLimiter) -> None:
    """Test getting a limiter for a domain."""
    # Get limiter for known domain
    limiter1 = rate_limiter.get_limiter("example.com")
    assert limiter1.max_rate == 5
    assert limiter1.time_period == 1.0

    # Get limiter for unknown domain (should use defaults)
    limiter2 = rate_limiter.get_limiter("unknown.com")
    assert limiter2.max_rate == 5  # default_rate
    assert limiter2.time_period == 1.0  # default_period


@pytest.mark.asyncio
async def test_acquire(rate_limiter: DomainRateLimiter) -> None:
    """Test acquiring from rate limiter."""
    start_time = time.time()

    # Acquire 3 times from test.com which has a limit of 2 req/sec
    await rate_limiter.acquire("test.com")
    await rate_limiter.acquire("test.com")
    await rate_limiter.acquire("test.com")

    elapsed = time.time() - start_time

    # Should take at least 0.5 second because of the rate limit
    # first two bypass, 2 req/s -> 0.5s / req
    assert elapsed >= 0.5


@pytest.mark.asyncio
async def test_update_rate_reduction(rate_limiter: DomainRateLimiter) -> None:
    """Test rate reduction on failures."""
    # Set initial success/failure counts
    rate_limiter.success_counts["test.com"] = 7
    rate_limiter.failure_counts["test.com"] = 3

    # Current rate should be 2 req/sec
    assert rate_limiter.domain_configs["test.com"][0] == 2

    # Update with a failure
    rate_limiter.get_limiter("test.com")
    rate_limiter.update_rate("test.com", False)

    # Rate should be reduced (2 * 0.75 = 1.5, rounded to 1.5)
    assert rate_limiter.domain_configs["test.com"][0] == 1.5
    assert rate_limiter.limiters["test.com"].max_rate == 1.5
    assert rate_limiter.configs_modified is True


@pytest.mark.asyncio
async def test_update_rate_increase(rate_limiter: DomainRateLimiter) -> None:
    """Test rate increase on high success rate."""
    # Set initial success/failure counts for high success rate
    rate_limiter.success_counts["test.com"] = 95
    rate_limiter.failure_counts["test.com"] = 5

    # Current rate should be 2 req/sec
    assert rate_limiter.domain_configs["test.com"][0] == 2

    # Update with a success
    rate_limiter.get_limiter("test.com")
    rate_limiter.update_rate("test.com", True)

    # Rate should be increased (2 * 1.1 = 2.2, rounded to 2.2)
    assert rate_limiter.domain_configs["test.com"][0] == 2.2
    assert rate_limiter.limiters["test.com"].max_rate == 2.2
    assert rate_limiter.configs_modified is True


@pytest.mark.asyncio
async def test_counter_reset(rate_limiter: DomainRateLimiter) -> None:
    """Test counter reset after many requests."""
    # Set counters close to reset threshold
    rate_limiter.success_counts["test.com"] = 45
    rate_limiter.failure_counts["test.com"] = 5

    # Update with a success to cross threshold
    rate_limiter.get_limiter("test.com")
    rate_limiter.update_rate("test.com", True)

    # Counters should be halved
    assert rate_limiter.success_counts["test.com"] == 23  # 46 / 2 rounded down
    assert rate_limiter.failure_counts["test.com"] == 2  # 5 / 2 rounded down


@pytest.mark.asyncio
async def test_save_configs(rate_limiter: DomainRateLimiter) -> None:
    """Test saving configurations to file."""
    # Modify a config to set the modified flag
    rate_limiter.domain_configs["test.com"] = (3.0, 1.0)
    rate_limiter.configs_modified = True

    # Save configs
    await rate_limiter.save_configs()

    # Check file was written with correct data
    with open(rate_limiter.config_path) as f:
        saved_data = json.load(f)
        assert saved_data["test.com"] == [3.0, 1.0]
        assert saved_data["example.com"] == [5, 1.0]

    # Modified flag should be reset
    assert rate_limiter.configs_modified is False


@pytest.mark.asyncio
async def test_adaptive_delay(rate_limiter: DomainRateLimiter) -> None:
    """Test adaptive delay between requests."""
    # Make quick successive requests
    await rate_limiter.acquire("example.com")

    start_time = time.time()
    await rate_limiter.acquire("example.com")
    elapsed = time.time() - start_time

    # Should have small delay (0.1s) between requests
    assert elapsed >= 0.1
    # Should have small delay (0.1s) between requests
    assert elapsed >= 0.1
