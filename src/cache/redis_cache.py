# pylint: disable=duplicate-code  # redis_cache and fake_redis_cache share identical method bodies by design
"""
Redis Cache Implementation
===========================
Concrete implementation of CacheInterface using Redis.
"""
from __future__ import annotations

import pickle
import logging
from typing import Any, Optional

import redis

from .interface import CacheInterface, CacheStats

logger = logging.getLogger(__name__)

class RedisCache(CacheInterface):
    """
    Redis-based cache implementation.

    Uses pickle for serialization to handle complex Python objects.
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        """
        Initialize Redis cache connection.

        Args:
            host: Redis server host
            port: Redis server port
            db: Redis database number
        """
        self.host = host
        self.port = port
        self.db = db
        self._client = None
        self._stats = CacheStats()
        self._connect()

    def _connect(self) -> None:
        """Establish connection to Redis server."""
        try:
            self._client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                decode_responses=False  # We handle serialization
            )
            # Test connection
            self._client.ping()
            logger.debug("Connected to Redis at %s:%s (db=%s)",
                        self.host, self.port, self.db)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Failed to connect to Redis: %s", exc)
            raise

    def _serialize(self, value: Any) -> bytes:
        """Serialize value for Redis storage."""
        try:
            return pickle.dumps(value)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Failed to serialize value: %s", exc)
            raise ValueError(f"Cannot serialize value: {exc}") from exc

    def _deserialize(self, data: bytes) -> Any:
        """Deserialize value from Redis storage."""
        try:
            return pickle.loads(data)
        except Exception as exc:  # pylint: disable=broad-exception-caught
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
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Cache get failed for key %s: %s", key, exc)
            self._stats.misses += 1
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Store a value in the cache."""
        try:
            serialized = self._serialize(value)
            if ttl:
                result = self._client.setex(key, ttl, serialized)
            else:
                result = self._client.set(key, serialized)
            self._stats.sets += 1
            return result
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Cache set failed for key %s: %s", key, exc)
            return False

    def delete(self, key: str) -> bool:
        """Delete a value from the cache."""
        try:
            deleted = self._client.delete(key) > 0
            if deleted:
                self._stats.deletes += 1
            return deleted
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Cache delete failed for key %s: %s", key, exc)
            return False

    def clear(self) -> bool:
        """Clear the entire cache."""
        try:
            self._client.flushdb()
            self._stats = CacheStats()  # Reset stats
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
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
        """Close Redis connection."""
        if self._client:
            try:
                self._client.close()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("Failed to close Redis connection: %s", exc)
            finally:
                self._client = None
