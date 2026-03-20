"""
Cache Factory
=============
Factory function for creating cache instances based on configuration.
"""
from __future__ import annotations

import logging
from typing import Optional

from .interface import CacheInterface
from .config import CacheConfig
from .redis_cache import RedisCache
from .fake_redis_cache import FakeRedisCache

logger = logging.getLogger(__name__)

def create_cache(config: Optional[CacheConfig] = None) -> CacheInterface:
    """
    Factory function to create cache instances.

    Args:
        config: Cache configuration. If None, creates default config from environment.

    Returns:
        CacheInterface instance

    Raises:
        ValueError: If provider is unknown or cannot be initialized
    """
    if config is None:
        config = CacheConfig.from_env()

    provider = config.provider.lower()
    logger.debug("Creating cache provider: %s", provider)

    if provider in ("fakeredis", "memory"):
        logger.info("Using in-memory (fake) Redis cache")
        return FakeRedisCache()

    if provider == "redis":
        logger.info("Using real Redis cache at %s:%s", config.redis_host, config.redis_port)
        return RedisCache(
            host=config.redis_host,
            port=config.redis_port,
            db=config.redis_db,
        )

    raise ValueError(f"Unknown cache provider: '{provider}'. Available: redis, fakeredis")

def get_default_cache() -> CacheInterface:
    """
    Get the default cache instance using environment configuration.

    This is a convenience function that creates a cache with default config.
    """
    return create_cache()
