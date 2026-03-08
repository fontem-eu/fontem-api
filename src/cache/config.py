"""
Cache Configuration System
===========================
Centralized configuration for cache providers and settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class CacheConfig:
    """
    Configuration for cache providers and settings.

    All values can be overridden via environment variables.
    """
    # Provider selection
    provider: str = "fakeredis"  # Default to fake for development/testing

    # Redis connection settings
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # Cache TTL (time-to-live) settings in seconds
    ttl_default: int = 3600  # 1 hour
    ttl_fundamentals: int = 86400  # 24 hours (fundamentals change infrequently)
    ttl_prices: int = 300  # 5 minutes (prices change more frequently)
    ttl_market_snapshot: int = 60  # 1 minute (market data is volatile)

    # Cache key prefixes
    key_prefix: str = "gmretl_"
    key_fundamentals: str = "fund_"
    key_prices: str = "price_"
    key_snapshot: str = "snap_"

    @classmethod
    def from_env(cls) -> 'CacheConfig':
        """
        Create configuration from environment variables.

        Environment variables override defaults.
        """
        return cls(
            provider=os.environ.get("CACHE_PROVIDER", "fakeredis"),
            redis_host=os.environ.get("CACHE_REDIS_HOST", "localhost"),
            redis_port=int(os.environ.get("CACHE_REDIS_PORT", "6379")),
            redis_db=int(os.environ.get("CACHE_REDIS_DB", "0")),
            ttl_default=int(os.environ.get("CACHE_TTL_DEFAULT", "3600")),
            ttl_fundamentals=int(os.environ.get("CACHE_TTL_FUNDAMENTALS", "86400")),
            ttl_prices=int(os.environ.get("CACHE_TTL_PRICES", "300")),
            ttl_market_snapshot=int(os.environ.get("CACHE_TTL_SNAPSHOT", "60")),
            key_prefix=os.environ.get("CACHE_KEY_PREFIX", "gmretl_"),
            key_fundamentals=os.environ.get("CACHE_KEY_FUNDAMENTALS", "fund_"),
            key_prices=os.environ.get("CACHE_KEY_PRICES", "price_"),
            key_snapshot=os.environ.get("CACHE_KEY_SNAPSHOT", "snap_"),
        )

    def get_full_key(self, key_type: str, ticker: str) -> str:
        """
        Generate a full cache key with prefix.

        Args:
            key_type: Type of key (fundamentals, prices, snapshot)
            ticker: Stock ticker symbol

        Returns:
            Full cache key string
        """
        if key_type == "fundamentals":
            prefix = self.key_fundamentals
        elif key_type == "prices":
            prefix = self.key_prices
        elif key_type == "snapshot":
            prefix = self.key_snapshot
        else:
            prefix = ""

        return f"{self.key_prefix}{prefix}{ticker.upper()}"

    def is_real_redis(self) -> bool:
        """Check if real Redis should be used (vs fake)."""
        return os.environ.get("USE_REAL_REDIS", "false").lower() in ("true", "1", "yes")