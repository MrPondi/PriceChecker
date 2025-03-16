import asyncio
import functools
import os
import pickle
import time
from collections.abc import Awaitable
from typing import (
    Any,
    Callable,
    Concatenate,
    Optional,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
)

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")  # Return type of the decorated function
P = ParamSpec("P")  # Parameters of the decorated function


class AsyncLRUCache:
    """A simple async-compatible LRU cache implementation"""

    def __init__(
        self, max_size: int = 100, ttl: int = 300, cache_name: Optional[str] = None
    ) -> None:
        """
        Initialize the cache

        Args:
            max_size: Maximum number of items to store in cache
            ttl: Time to live for cache entries in seconds
            cache_name: Optional name for the cache to enable persistence
        """
        self.max_size = max_size
        self.ttl = ttl
        self.cache_name = cache_name
        self.cache: dict[str, tuple[Any, float]] = {}
        self.access_times: dict[str, float] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Load cache from disk if cache_name is provided
        if cache_name:
            self._load_cache()

    async def get(self, key: str) -> Optional[Any]:  # noqa: ANN401
        """Get an item from the cache"""
        if key not in self.cache:
            return None

        value, expiry = self.cache[key]
        current_time = time.time()

        # Check if expired
        if current_time > expiry:
            async with self._lock:
                # Double-check expiry after acquiring lock
                if key in self.cache and current_time > self.cache[key][1]:
                    del self.cache[key]
                    if key in self.access_times:
                        del self.access_times[key]
            return None

        # Update access time
        self.access_times[key] = current_time
        return value

    async def set(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Add an item to the cache"""
        current_time = time.time()
        expiry = current_time + self.ttl

        async with self._lock:
            # Special case: if value is None, it's a cache invalidation request
            if value is None and key in self.cache:
                del self.cache[key]
                if key in self.access_times:
                    del self.access_times[key]
                return

            # Evict least recently used items if we're at capacity
            if len(self.cache) >= self.max_size and key not in self.cache:
                self._evict_lru()

            self.cache[key] = (value, expiry)
            self.access_times[key] = current_time

            # Start cleanup task if not running
            self._ensure_cleanup_task()

            # Save cache to disk if cache_name is provided
            if self.cache_name:
                await self._save_cache()

    def _ensure_cleanup_task(self) -> None:
        """Ensure the cleanup task is running"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._cleanup_task.set_name(f"cache-cleanup-{id(self)}")

    def _evict_lru(self) -> None:
        """Evict the least recently used cache item"""
        if not self.access_times:
            return

        # Find oldest accessed key
        oldest_key = min(self.access_times.items(), key=lambda x: x[1])[0]
        del self.cache[oldest_key]
        del self.access_times[oldest_key]

    async def _cleanup_loop(self) -> None:
        """Background task to clean up expired entries"""
        try:
            while self.cache:
                await asyncio.sleep(
                    min(self.ttl / 2, 60)
                )  # Clean up every ttl/2 or 60s, whichever is less
                await self._cleanup_expired()
        except asyncio.CancelledError:
            logger.debug("Cache cleanup task cancelled")
        except Exception as e:
            logger.error(f"Error in cache cleanup: {str(e)}")

    async def _cleanup_expired(self) -> None:
        """Remove all expired entries from cache"""
        current_time = time.time()
        expired_keys = []

        # Identify expired keys
        for key, (_, expiry) in self.cache.items():
            if current_time > expiry:
                expired_keys.append(key)

        # Remove expired keys
        if expired_keys:
            async with self._lock:
                for key in expired_keys:
                    if key in self.cache and current_time > self.cache[key][1]:
                        del self.cache[key]
                        if key in self.access_times:
                            del self.access_times[key]
            logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")
            # Save cache after cleanup if persistence is enabled
            if self.cache_name:
                await self._save_cache()

    async def clear(self) -> None:
        """Clear all cache entries"""
        async with self._lock:
            self.cache.clear()
            self.access_times.clear()
            if self.cache_name:
                await self._save_cache()

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics"""
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "ttl": self.ttl,
            "utilization": len(self.cache) / self.max_size if self.max_size > 0 else 0,
            "persistent": self.cache_name is not None,
        }

    def _get_cache_path(self) -> str:
        """Get the path to the cache file"""
        cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "async_lru_cache")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{self.cache_name}.pkl")

    def _load_cache(self) -> None:
        """Load cache from disk"""
        cache_path = self._get_cache_path()
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "rb") as f:
                    data = pickle.load(f)
                    self.cache = data.get("cache", {})
                    self.access_times = data.get("access_times", {})
                    logger.debug(
                        f"Loaded cache from {cache_path} with {len(self.cache)} entries"
                    )

                    # Remove expired entries right away
                    current_time = time.time()
                    expired_keys = [
                        key
                        for key, (_, expiry) in self.cache.items()
                        if current_time > expiry
                    ]

                    for key in expired_keys:
                        del self.cache[key]
                        if key in self.access_times:
                            del self.access_times[key]

                    if expired_keys:
                        logger.debug(
                            f"Removed {len(expired_keys)} expired entries during load"
                        )
        except Exception as e:
            logger.error(f"Error loading cache from disk: {str(e)}")
            # Reset cache to empty if loading fails
            self.cache = {}
            self.access_times = {}

    async def _save_cache(self) -> None:
        """Save cache to disk"""
        if not self.cache_name:
            return

        cache_path = self._get_cache_path()
        try:
            # Create a separate task to avoid blocking
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._save_cache_to_disk, cache_path)
        except Exception as e:
            logger.error(f"Error saving cache to disk: {str(e)}")

    def _save_cache_to_disk(self, cache_path: str) -> None:
        """Helper method to save cache to disk (runs in executor)"""
        with open(cache_path, "wb") as f:
            pickle.dump({"cache": self.cache, "access_times": self.access_times}, f)
        logger.debug(f"Saved cache to {cache_path} with {len(self.cache)} entries")


# Define a protocol for the cached function
class CachedAsyncFunc(Protocol[P, T]):
    """Protocol for a cached async function."""

    __call__: Callable[P, Awaitable[T]]
    cache: AsyncLRUCache
    invalidate: Callable[[], Awaitable[None]]


def async_cached(
    ttl: int = 300, max_size: int = 100, cache_name: Optional[str] = None
) -> Callable[[Callable[Concatenate[Any, P], Awaitable[T]]], CachedAsyncFunc[P, T]]:
    """
    Decorator for caching async function results.

    Args:
        ttl: Time to live for cache entries in seconds
        max_size: Maximum number of items to store in cache
        cache_name: Optional name for the cache to enable persistence between runs

    Returns:
        A decorator function that caches results of the decorated async function
    """
    cache = AsyncLRUCache(max_size=max_size, ttl=ttl, cache_name=cache_name)

    def decorator(
        func: Callable[Concatenate[Any, P], Awaitable[T]],
    ) -> CachedAsyncFunc[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Create a cache key from the function name and arguments
            key_parts = [func.__name__]
            key_parts.extend(str(arg) for arg in args)
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)

            # Try to get from cache first
            cached_result = await cache.get(cache_key)
            if cached_result is not None:
                logger.debug(f"Cache hit for {func.__name__}")
                return cast(T, cached_result)

            # Execute function and cache result
            result = await func(*args, **kwargs)  # type: ignore
            await cache.set(cache_key, result)
            return result

        # Attach cache to the wrapper function for manual invalidation
        wrapper.cache = cache  # type: ignore
        wrapper.invalidate = cache.clear  # type: ignore

        return cast(CachedAsyncFunc[P, T], wrapper)

    return decorator
