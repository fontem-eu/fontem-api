"""
Cache Interface - Abstract Base Class
======================================
Defines the contract that all cache implementations must fulfill.
This enables easy switching between different caching providers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Dict
from dataclasses import dataclass

@dataclass
class CacheStats:
    """Statistics about cache performance."""
    hits: int = 0
    misses: int = 0
    sets: int = 0
    deletes: int = 0
    evictions: int = 0

class CacheInterface(ABC):
    """
    Abstract base class for all cache implementations.

    All concrete cache providers (Redis, Memcached, etc.) must implement
    this interface to be used interchangeably in the application.
    """

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve a value from the cache.

        Args:
            key: The cache key to retrieve

        Returns:
            The cached value, or None if not found
        """
        pass

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Store a value in the cache.

        Args:
            key: The cache key
            value: The value to cache
            ttl: Time-to-live in seconds (None for no expiration)

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """
        Delete a value from the cache.

        Args:
            key: The cache key to delete

        Returns:
            True if deleted, False if key didn't exist
        """
        pass

    @abstractmethod
    def clear(self) -> bool:
        """
        Clear the entire cache.

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def get_stats(self) -> CacheStats:
        """
        Get statistics about cache performance.

        Returns:
            CacheStats object with hit/miss counts
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """
        Close any connections and clean up resources.
        """
        pass