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
from typing import Optional, List, Dict

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

    def _get_cached_data(self, cache_key: str, fetch_func, ttl_key: str, *args, **kwargs) -> Any:
        """
        Helper method to handle caching logic with a fetch function.

        Args:
            cache_key: The cache key to use
            fetch_func: Function to call if cache miss occurs
            ttl_key: Configuration key for TTL
            *args, **kwargs: Arguments to pass to fetch_func

        Returns:
            The cached or fetched data
        """
        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for %s", cache_key)
            return cached

        # Cache miss - fetch and cache
        logger.debug("Cache miss for %s", cache_key)

        # Get TTL from config
        ttl = getattr(self._cache_config, ttl_key, self._cache_config.ttl_default)

        # Fetch data
        result = fetch_func(*args, **kwargs)

        # Store in cache
        self._cache.set(cache_key, result, ttl)
        return result

    # ------------------------------------------------------------------
    def get_annual_fundamentals(self, ticker: str, years: int = 10) -> dict:
        cache_key = self._cache_config.get_full_key("fundamentals", ticker)
        return self._get_cached_data(
            cache_key,
            lambda: self._edgar.fetch_fundamentals(ticker, years=years),
            "ttl_fundamentals"
        )

    def get_annual_avg_prices(self, ticker: str, years: int = 10) -> pd.Series:
        cache_key = self._cache_config.get_full_key("prices", ticker)
        period = f"{min(years, 10)}y"
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_annual_avg_prices(ticker, period=period),
            "ttl_prices"
        )

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        cache_key = self._cache_config.get_full_key("dividends", ticker)
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_annual_dividends(ticker),
            "ttl_prices"
        )

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        cache_key = self._cache_config.get_full_key(f"history_{period}", ticker)
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_history(ticker, period=period),
            "ttl_prices"
        )

    def get_market_snapshot(self, ticker: str) -> dict:
        cache_key = self._cache_config.get_full_key("snapshot", ticker)

        def fetch_snapshot():
            return {
                "current_price":      self._price.get_current_price(ticker),
                "avg_volume":         self._price.get_avg_volume(ticker),
                "shares_outstanding": self._price.get_shares_outstanding(ticker),
                "last_dividend":      self._price.get_last_dividend(ticker),
                "splits":             self._price.get_splits(ticker),
                "latest_quarter":     self._price.get_latest_quarter(ticker),
            }

        return self._get_cached_data(
            cache_key,
            fetch_snapshot,
            "ttl_market_snapshot"
        )

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

    def list_sectors(self) -> List[str]:
        """Get list of unique sectors for filtering."""
        all_tickers = self.get_available_tickers()
        sectors = {ticker['sector'] for ticker in all_tickers if ticker.get('sector')}
        return sorted(list(sectors))

    def list_exchanges(self) -> List[str]:
        """Get list of unique exchanges for filtering."""
        all_tickers = self.get_available_tickers()
        exchanges = {ticker['exchange'] for ticker in all_tickers if ticker.get('exchange')}
        return sorted(list(exchanges))

    # ------------------------------------------------------------------
    def get_available_tickers(self) -> List[Dict]:
        """
        Get list of tickers available in EDGAR with rich metadata.

        Returns comprehensive company information including:
        - symbol, name, cik, sic codes
        - exchange, sector, industry classifications
        - search-friendly fields for UI filtering

        Uses caching to avoid repeated calls to SEC API.
        """
        cache_key = self._cache_config.get_full_key("ticker_list", "all")
        return self._get_cached_data(
            cache_key,
            lambda: self._edgar.get_edgar_ticker_list(),
            "ttl_ticker_list"
        )

    def search_tickers(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Search tickers by name, symbol, or keywords.

        Args:
            query: Search term (case-insensitive)
            limit: Maximum number of results to return

        Returns:
            List of matching ticker dictionaries
        """
        all_tickers = self.get_available_tickers()

        if not query:
            return all_tickers[:limit]

        query_lower = query.lower()

        # Search in name, symbol, and keywords
        matches = []
        for ticker in all_tickers:
            # Check if query matches in name, symbol, or keywords
            if (query_lower in ticker.get('search_name', '') or
                query_lower in ticker.get('symbol', '').lower() or
                query_lower in ticker.get('search_keywords', '')):
                matches.append(ticker)
                if len(matches) >= limit:
                    break

        return matches
