"""
Cache Decorators
================
Utility decorators for adding caching to functions and methods.
"""
from __future__ import annotations

import time
import logging
from typing import Callable, TypeVar, Any, Optional
from functools import wraps

from .interface import CacheInterface
from .config import CacheConfig

logger = logging.getLogger(__name__)

T = TypeVar('T')

def cached_method(
    cache: CacheInterface,
    config: CacheConfig,
    cache_type: str,
    ttl_key: Optional[str] = None
) -> Callable:
    """
    Decorator to cache method results.

    Args:
        cache: Cache interface instance
        config: Cache configuration
        cache_type: Type of data being cached (fundamentals, prices, snapshot)
        ttl_key: Configuration key for TTL, or None to use default

    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(self, ticker: str, *args, **kwargs) -> Any:
            # Generate cache key
            cache_key = config.get_full_key(cache_type, ticker)

            # Try to get from cache first
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.debug("Cache hit for %s: %s", cache_type, ticker)
                return cached_result

            logger.debug("Cache miss for %s: %s", cache_type, ticker)

            # Cache miss - execute the original function
            start_time = time.time()
            result = func(self, ticker, *args, **kwargs)
            execution_time = time.time() - start_time

            logger.debug("Fetched %s for %s in %.3fs", cache_type, ticker, execution_time)

            # Determine TTL
            if ttl_key:
                ttl = getattr(config, ttl_key, config.ttl_default)
            else:
                ttl = config.ttl_default

            # Store in cache
            cache.set(cache_key, result, ttl)
            logger.debug("Cached %s for %s with TTL %ss", cache_type, ticker, ttl)

            return result
        return wrapper
    return decorator

def timed_cache_method(cache: CacheInterface) -> Callable:
    """
    Decorator to measure and log cache performance for a method.

    Args:
        cache: Cache interface instance

    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start_time

                # Get cache stats
                stats = cache.get_stats()
                logger.info(
                    "Cache stats for %s: %.3fs, hits=%s, misses=%s, sets=%s",
                    func.__name__, elapsed, stats.hits, stats.misses, stats.sets
                )

                return result
            except Exception as exc:
                logger.error("Error in %s: %s", func.__name__, exc)
                raise
        return wrapper
    return decorator
