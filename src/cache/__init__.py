"""
Cache Module
============
Centralized caching system for the GMR ETL application.

Provides:
- CacheInterface: Abstract base class for cache implementations
- RedisCache: Production Redis cache implementation
- FakeRedisCache: In-memory cache for testing
- CacheConfig: Configuration management
- create_cache: Factory function for creating cache instances
- cached_method: Decorator for adding caching to methods
"""
from .interface import CacheInterface, CacheStats
from .config import CacheConfig
from .factory import create_cache, get_default_cache
from .decorators import cached_method, timed_cache_method

# Re-export for convenience
from .redis_cache import RedisCache
from .fake_redis_cache import FakeRedisCache

__all__ = [
    'CacheInterface', 'CacheStats',
    'CacheConfig',
    'create_cache', 'get_default_cache',
    'cached_method', 'timed_cache_method',
    'RedisCache', 'FakeRedisCache'
]