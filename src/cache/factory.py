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

    if provider == "redis":
        if config.is_real_redis():
            logger.info("Using real Redis cache at %s:%s", config.redis_host, config.redis_port)
            return RedisCache(
                host=config.redis_host,
                port=config.redis_port,
                db=config.redis_db
            )
        else:
            logger.info("Using fake Redis cache (real Redis disabled)")
            return FakeRedisCache()

    elif provider == "fakeredis":
        logger.info("Using fake Redis cache")
        return FakeRedisCache()

    elif provider == "memory":
        # For backward compatibility
        logger.info("Using fake Redis cache (memory mode)")
        return FakeRedisCache()

    else:
        raise ValueError(f"Unknown cache provider: {provider}. Available: redis, fakeredis")

def get_default_cache() -> CacheInterface:
    """
    Get the default cache instance using environment configuration.

    This is a convenience function that creates a cache with default config.
    """
    return create_cache()