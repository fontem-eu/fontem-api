"""
Fake Redis Cache Implementation
===============================
In-memory cache implementation that mimics Redis behavior for testing.
Uses fakeredis library to provide a Redis-compatible interface without
requiring a Redis server.
"""
from __future__ import annotations

import pickle
import logging
from typing import Any, Optional

try:
    import fakeredis
    FAKE_REDIS_AVAILABLE = True
except ImportError:
    FAKE_REDIS_AVAILABLE = False

from .interface import CacheInterface, CacheStats

logger = logging.getLogger(__name__)

class FakeRedisCache(CacheInterface):
    """
    In-memory cache implementation using fakeredis.

    Provides the same interface as RedisCache but runs entirely in-memory,
    making it perfect for testing without external dependencies.
    """

    def __init__(self):
        """
        Initialize in-memory fake Redis cache.
        """
        if not FAKE_REDIS_AVAILABLE:
            raise ImportError(
                "fakeredis is not installed. Please install it with: pip install fakeredis"
            )

        self._client = fakeredis.FakeStrictRedis(decode_responses=False)
        self._stats = CacheStats()
        logger.debug("Initialized FakeRedis in-memory cache")

    def _serialize(self, value: Any) -> bytes:
        """Serialize value for storage."""
        try:
            return pickle.dumps(value)
        except Exception as exc:
            logger.error("Failed to serialize value: %s", exc)
            raise ValueError(f"Cannot serialize value: {exc}") from exc

    def _deserialize(self, data: bytes) -> Any:
        """Deserialize value from storage."""
        try:
            return pickle.loads(data)
        except Exception as exc:
            logger.error("Failed to deserialize data: %s", exc)
            raise ValueError(f"Cannot deserialize data: {exc}") from exc

    def get(self, key: str) -> Optional[Any]:
        """Retrieve a value from the cache."""
        try:
            data = self._client.get(key)
            if data is None:
                self._stats.misses += 1
                return None

            self._stats.hits += 1
            return self._deserialize(data)
        except Exception as exc:
            logger.error("Cache get failed for key %s: %s", key, exc)
            self._stats.misses += 1
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Store a value in the cache."""
        try:
            serialized = self._serialize(value)
            if ttl:
                self._client.setex(key, ttl, serialized)
            else:
                self._client.set(key, serialized)
            self._stats.sets += 1
            return True
        except Exception as exc:
            logger.error("Cache set failed for key %s: %s", key, exc)
            return False

    def delete(self, key: str) -> bool:
        """Delete a value from the cache."""
        try:
            deleted = self._client.delete(key) > 0
            if deleted:
                self._stats.deletes += 1
            return deleted
        except Exception as exc:
            logger.error("Cache delete failed for key %s: %s", key, exc)
            return False

    def clear(self) -> bool:
        """Clear the entire cache."""
        try:
            self._client.flushdb()
            self._stats = CacheStats()  # Reset stats
            return True
        except Exception as exc:
            logger.error("Cache clear failed: %s", exc)
            return False

    def get_stats(self) -> CacheStats:
        """Get statistics about cache performance."""
        # Return a copy to prevent external modification of internal state
        return CacheStats(
            hits=self._stats.hits,
            misses=self._stats.misses,
            sets=self._stats.sets,
            deletes=self._stats.deletes,
            evictions=self._stats.evictions
        )

    def close(self) -> None:
        """Close any resources (no-op for fake Redis)."""

    def __getstate__(self):
        """Get state for pickling (exclude client)."""
        state = self.__dict__.copy()
        state['_client'] = None
        return state

    def __setstate__(self, state):
        """Set state after unpickling (recreate client)."""
        self.__dict__.update(state)
        self._client = fakeredis.FakeStrictRedis(decode_responses=False)
