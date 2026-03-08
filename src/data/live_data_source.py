"""
Live Data Source
=================
Concrete implementation of GMRDataSource that pulls data from the SEC EDGAR
API (via EdgarFetcher) and Yahoo Finance (via PriceFetcher).

Inject this into GMRLong / GMRShort for production use.
Inject a MockDataSource in unit tests to avoid any network traffic.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from .edgar_fetcher import EdgarFetcher
from .price_fetcher import PriceFetcher
from ..analysis.gmr_data_source import GMRDataSource
from ..cache import CacheInterface, CacheConfig, create_cache, cached_method

logger = logging.getLogger(__name__)

class LiveDataSource(GMRDataSource):
    """
    Production adapter that satisfies the GMRDataSource port using the two
    fetcher classes already implemented in this project.

    Example::

        ds = LiveDataSource()                   # uses default identities
        result = GMRLong(ds).compute("KO")
    """

    def __init__(
        self,
        edgar_fetcher: Optional[EdgarFetcher] = None,
        price_fetcher: Optional[PriceFetcher] = None,
        edgar_identity: str = "bemar-edgar@research.com",
        cache: Optional[CacheInterface] = None,
        cache_config: Optional[CacheConfig] = None,
    ) -> None:
        self._edgar = edgar_fetcher or EdgarFetcher(identity=edgar_identity)
        self._price = price_fetcher or PriceFetcher()

        # Initialize caching
        self._cache = cache or create_cache(cache_config)
        self._cache_config = cache_config or CacheConfig.from_env()

        logger.info("LiveDataSource initialized with %s cache",
                   type(self._cache).__name__)

    # ------------------------------------------------------------------
    def get_annual_fundamentals(self, ticker: str, years: int = 10) -> dict:
        cache_key = self._cache_config.get_full_key("fundamentals", ticker)

        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for fundamentals: %s", ticker)
            return cached

        # Cache miss - fetch and cache
        logger.debug("Cache miss for fundamentals: %s", ticker)
        result = self._edgar.fetch_fundamentals(ticker, years=years)
        self._cache.set(cache_key, result, self._cache_config.ttl_fundamentals)
        return result

    def get_annual_avg_prices(self, ticker: str, years: int = 10) -> pd.Series:
        cache_key = self._cache_config.get_full_key("prices", ticker)

        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for prices: %s", ticker)
            return cached

        # Cache miss - fetch and cache
        logger.debug("Cache miss for prices: %s", ticker)
        period = f"{min(years, 10)}y"
        result = self._price.get_annual_avg_prices(ticker, period=period)
        self._cache.set(cache_key, result, self._cache_config.ttl_prices)
        return result

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        cache_key = self._cache_config.get_full_key("dividends", ticker)

        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for dividends: %s", ticker)
            return cached

        # Cache miss - fetch and cache
        logger.debug("Cache miss for dividends: %s", ticker)
        result = self._price.get_annual_dividends(ticker)
        self._cache.set(cache_key, result, self._cache_config.ttl_prices)
        return result

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        cache_key = self._cache_config.get_full_key(f"history_{period}", ticker)

        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for price history: %s", ticker)
            return cached

        # Cache miss - fetch and cache
        logger.debug("Cache miss for price history: %s", ticker)
        result = self._price.get_history(ticker, period=period)
        self._cache.set(cache_key, result, self._cache_config.ttl_prices)
        return result

    def get_market_snapshot(self, ticker: str) -> dict:
        cache_key = self._cache_config.get_full_key("snapshot", ticker)

        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for market snapshot: %s", ticker)
            return cached

        # Cache miss - fetch and cache
        logger.debug("Cache miss for market snapshot: %s", ticker)
        result = {
            "current_price":      self._price.get_current_price(ticker),
            "avg_volume":         self._price.get_avg_volume(ticker),
            "shares_outstanding": self._price.get_shares_outstanding(ticker),
            "last_dividend":      self._price.get_last_dividend(ticker),
            "splits":             self._price.get_splits(ticker),
            "latest_quarter":     self._price.get_latest_quarter(ticker),
        }
        self._cache.set(cache_key, result, self._cache_config.ttl_market_snapshot)
        return result

    # ------------------------------------------------------------------
    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "provider": type(self._cache).__name__,
            "stats": self._cache.get_stats().__dict__
        }

    def clear_cache(self) -> bool:
        """Clear the cache."""
        return self._cache.clear()

    def close(self) -> None:
        """Close cache connections."""
        self._cache.close()