import asyncio
import tempfile
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from src.core.cache import AsyncLRUCache


@pytest.fixture
def temp_cache_dir() -> Iterator[str]:
    """Create a temporary directory for cache files."""
    with tempfile.TemporaryDirectory() as tmpdirname:

        def mock_expanduser(path:str) -> str:
            if path.startswith("~"):
                return path.replace("~", tmpdirname)
            return path

        with patch("os.path.expanduser", side_effect=mock_expanduser):
            yield tmpdirname


@pytest.mark.asyncio
async def test_cache_init() -> None:
    """Test cache initialization with default values."""
    cache = AsyncLRUCache()
    assert cache.max_size == 100
    assert cache.ttl == 300
    assert cache.cache_name is None
    assert len(cache.cache) == 0


@pytest.mark.asyncio
async def test_cache_set_get() -> None:
    """Test basic set and get operations."""
    cache = AsyncLRUCache()

    # Set a value
    await cache.set("test_key", "test_value")

    # Get the value
    value = await cache.get("test_key")
    assert value == "test_value"

    # Get a non-existent key
    value = await cache.get("non_existent")
    assert value is None


@pytest.mark.asyncio
async def test_cache_expiry() -> None:
    """Test that cache entries expire."""
    cache = AsyncLRUCache(ttl=1)  # 1 second TTL

    # Set a value
    await cache.set("test_key", "test_value")

    # Get the value immediately
    value = await cache.get("test_key")
    assert value == "test_value"

    # Wait for expiry
    await asyncio.sleep(1.1)

    # Value should be None after expiry
    value = await cache.get("test_key")
    assert value is None


@pytest.mark.asyncio
async def test_cache_eviction() -> None:
    """Test LRU eviction policy."""
    cache = AsyncLRUCache(max_size=2)

    # Set two values
    await cache.set("key1", "value1")
    await cache.set("key2", "value2")

    # Both should be present
    assert await cache.get("key1") == "value1"
    assert await cache.get("key2") == "value2"

    # Access key1 to make key2 the LRU
    await cache.get("key1")

    # Add a third value
    await cache.set("key3", "value3")

    # key2 should be evicted
    assert await cache.get("key1") == "value1"
    assert await cache.get("key2") is None
    assert await cache.get("key3") == "value3"


@pytest.mark.asyncio
async def test_cache_clear() -> None:
    """Test clearing the cache."""
    cache = AsyncLRUCache()

    # Set values
    await cache.set("key1", "value1")
    await cache.set("key2", "value2")

    # Clear the cache
    await cache.clear()

    # Verify all values are gone
    assert await cache.get("key1") is None
    assert await cache.get("key2") is None


@pytest.mark.asyncio
async def test_cache_invalidation() -> None:
    """Test invalidating a specific cache entry."""
    cache = AsyncLRUCache()

    # Set values
    await cache.set("key1", "value1")
    await cache.set("key2", "value2")

    # Invalidate one key
    await cache.set("key1", None)

    # Verify only that key is gone
    assert await cache.get("key1") is None
    assert await cache.get("key2") == "value2"


@pytest.mark.asyncio
async def test_cache_cleanup_task() -> None:
    """Test that the cleanup task is created and runs."""
    cache = AsyncLRUCache(ttl=1)

    # Set a value to trigger cleanup task creation
    await cache.set("key1", "value1")

    # Check that cleanup task is created
    assert cache._cleanup_task is not None
    assert not cache._cleanup_task.done()

    # Wait for expiry and cleanup
    await asyncio.sleep(1.5)

    # Try to get the expired value
    assert await cache.get("key1") is None


@pytest.mark.asyncio
async def test_cache_persistence(temp_cache_dir: str) -> None:
    """Test cache persistence to disk."""
    # Create a persistent cache
    cache1 = AsyncLRUCache(cache_name="test_persistence")

    # Set some values
    await cache1.set("key1", "value1")
    await cache1.set("key2", "value2")

    # Create a new cache instance with the same name
    cache2 = AsyncLRUCache(cache_name="test_persistence")

    # Values should be loaded from disk
    assert await cache2.get("key1") == "value1"
    assert await cache2.get("key2") == "value2"


@pytest.mark.asyncio
async def test_stats() -> None:
    """Test cache statistics."""
    cache = AsyncLRUCache(max_size=10, ttl=30)

    # Add some items
    await cache.set("key1", "value1")
    await cache.set("key2", "value2")

    # Get stats
    stats = cache.get_stats()

    assert stats["size"] == 2
    assert stats["max_size"] == 10
    assert stats["ttl"] == 30
    assert stats["utilization"] == 0.2
    assert not stats["persistent"]


@pytest.mark.asyncio
async def test_expired_entry_cleanup_on_load(temp_cache_dir: str) -> None:
    """Test that expired entries are cleaned up when loading from disk."""
    # Create a persistent cache with short TTL
    cache1 = AsyncLRUCache(cache_name="test_expiry", ttl=1)

    # Set some values
    await cache1.set("key1", "value1")

    # Wait for expiry
    await asyncio.sleep(1.1)

    # Create a new cache instance with the same name
    cache2 = AsyncLRUCache(cache_name="test_expiry")

    # Expired value should not be loaded
    assert await cache2.get("key1") is None
    assert await cache2.get("key1") is None
