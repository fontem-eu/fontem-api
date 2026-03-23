"""
Live Data Source
=================
Concrete implementation of GMRDataSource that pulls data from the SEC EDGAR
API (via EdgarFetcher) and Yahoo Finance (via PriceFetcher).

Inject this into GMRLong / GMRShort for production use.
Inject a MockDataSource in unit tests to avoid any network traffic.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Any, Optional, List, Dict


import pandas as pd

from .edgar_fetcher import EdgarFetcher
from .price_fetcher import PriceFetcher
from ..analysis.gmr_data_source import FinancialDataSource
from ..cache import CacheInterface, CacheConfig, create_cache

logger = logging.getLogger(__name__)

# Hard cap on how long a single yfinance network call may block the worker
# thread.  If Yahoo Finance is slow or rate-limiting, we return the cached
# fallback rather than leaving callers waiting 30 + seconds.
_PRICE_FETCH_TIMEOUT_S = 15

# When a price fetch times out or errors, cache the fallback for this many
# seconds.  This prevents every subsequent request from also waiting the
# full timeout period when the upstream is persistently unavailable.
_NEGATIVE_RESULT_TTL_S = 60


class LiveDataSource(FinancialDataSource):
    """
    Production adapter that satisfies the GMRDataSource port using the two
    fetcher classes already implemented in this project.

    When *local_data_dir* is provided the EDGAR fundamentals are read from the
    locally downloaded bulk data (companyfacts / submissions) instead of being
    fetched over the network.  Price data (yfinance) is always live.

    Example::

        ds = LiveDataSource()                              # live EDGAR + live prices
        ds = LiveDataSource(local_data_dir="/edgar-data/full")  # local EDGAR + live prices
        result = GMRLong(ds).compute("KO")
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        edgar_fetcher: Optional[EdgarFetcher] = None,
        price_fetcher: Optional[PriceFetcher] = None,
        edgar_identity: str = "bemar-edgar@research.com",
        cache: Optional[CacheInterface] = None,
        cache_config: Optional[CacheConfig] = None,
        local_data_dir: Optional[str] = None,
    ) -> None:
        if local_data_dir is not None:
            from .local_edgar_fetcher import LocalEdgarFetcher  # pylint: disable=import-outside-toplevel
            self._edgar = LocalEdgarFetcher(local_data_dir=local_data_dir)
            logger.info("LiveDataSource using local EDGAR data from %s", local_data_dir)
        else:
            self._edgar = edgar_fetcher or EdgarFetcher(identity=edgar_identity)
        self._price = price_fetcher or PriceFetcher()

        # Initialize caching
        self._cache = cache or create_cache(cache_config)
        self._cache_config = cache_config or CacheConfig.from_env()

        # Per-key locks to prevent thundering-herd on cold-cache misses.
        # Multiple concurrent requests for the same key each see a cache miss
        # and would all fetch independently without this guard.
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._fetch_locks_guard = threading.Lock()

        logger.info("LiveDataSource initialized with %s cache",
                   type(self._cache).__name__)

    def _get_fetch_lock(self, cache_key: str) -> threading.Lock:
        with self._fetch_locks_guard:
            if cache_key not in self._fetch_locks:
                self._fetch_locks[cache_key] = threading.Lock()
            return self._fetch_locks[cache_key]

    def _get_cached_data(
        self,
        cache_key: str,
        fetch_func,
        ttl_key: str,
        timeout_s: Optional[float] = None,
        fallback: Any = None,
    ) -> Any:
        """
        Caching helper with optional per-call timeout.

        Uses double-checked locking (per cache key) to prevent the thundering
        herd.  When *timeout_s* is set and the fetch exceeds it, *fallback* is
        returned immediately (and the result is NOT cached, so the next caller
        will retry the live fetch).
        """
        # Fast path — no lock needed for a cache hit
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache HIT  %s", cache_key)
            return cached

        # Slow path — acquire a per-key lock so only one thread fetches
        lock = self._get_fetch_lock(cache_key)
        with lock:
            # Re-check: another thread may have populated the cache while we waited
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache HIT (post-lock) %s", cache_key)
                return cached

            logger.info("Cache MISS %s — fetching…", cache_key)
            t0 = time.perf_counter()
            ttl = getattr(self._cache_config, ttl_key, self._cache_config.ttl_default)

            if timeout_s is not None:
                # NOTE: do NOT use `with ThreadPoolExecutor() as ex` here.
                # A `return` inside a `with` block still triggers __exit__,
                # which calls shutdown(wait=True) and blocks until the
                # background thread finishes — defeating the timeout entirely.
                _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                _future = _ex.submit(fetch_func)
                try:
                    result = _future.result(timeout=timeout_s)
                except concurrent.futures.TimeoutError:
                    _ex.shutdown(wait=False)  # let background thread finish on its own
                    elapsed = time.perf_counter() - t0
                    logger.warning(
                        "Cache TIMEOUT %s after %.1fs — caching fallback for %ds",
                        cache_key, elapsed, _NEGATIVE_RESULT_TTL_S,
                    )
                    # Cache the fallback briefly so the next request does not
                    # wait another timeout period for a persistently slow source.
                    if fallback is not None:
                        self._cache.set(cache_key, fallback, _NEGATIVE_RESULT_TTL_S)
                    return fallback
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    _ex.shutdown(wait=False)
                    elapsed = time.perf_counter() - t0
                    logger.warning(
                        "Cache FETCH ERROR %s after %.1fs: %s — caching fallback for %ds",
                        cache_key, elapsed, exc, _NEGATIVE_RESULT_TTL_S,
                    )
                    if fallback is not None:
                        self._cache.set(cache_key, fallback, _NEGATIVE_RESULT_TTL_S)
                    return fallback
                _ex.shutdown(wait=False)
            else:
                result = fetch_func()

            elapsed = time.perf_counter() - t0
            logger.info("Fetched %s in %.1fs", cache_key, elapsed)
            self._cache.set(cache_key, result, ttl)
            return result

    # ------------------------------------------------------------------
    def get_annual_fundamentals(self, ticker: str, years: int = 10) -> dict:
        cache_key = self._cache_config.get_full_key("fundamentals", ticker)

        def _fetch() -> dict:
            result = self._edgar.fetch_fundamentals(ticker, years=years)
            # Strip raw edgartools DataFrames (_balance_sheet, _income, _cashflow)
            # before caching: they're large, fragile to pickle across version upgrades,
            # and unused by any caller of this method.
            return {k: v for k, v in result.items() if not k.startswith("_")}

        return self._get_cached_data(cache_key, _fetch, "ttl_fundamentals")

    def get_annual_avg_prices(self, ticker: str, years: int = 10) -> pd.Series:
        cache_key = self._cache_config.get_full_key("prices", ticker)
        period = f"{min(years, 10)}y"
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_annual_avg_prices(ticker, period=period),
            "ttl_prices",
            timeout_s=_PRICE_FETCH_TIMEOUT_S,
            fallback=pd.Series(dtype=float),
        )

    def get_annual_dividends(self, ticker: str) -> pd.Series:
        cache_key = self._cache_config.get_full_key("dividends", ticker)
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_annual_dividends(ticker),
            "ttl_prices",
            timeout_s=_PRICE_FETCH_TIMEOUT_S,
            fallback=pd.Series(dtype=float),
        )

    def get_price_history(self, ticker: str, period: str = "1y") -> pd.DataFrame:
        cache_key = self._cache_config.get_full_key(f"history_{period}", ticker)
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_history(ticker, period=period),
            "ttl_prices",
            timeout_s=_PRICE_FETCH_TIMEOUT_S,
            fallback=pd.DataFrame(),
        )

    def get_market_snapshot(self, ticker: str) -> dict:
        cache_key = self._cache_config.get_full_key("snapshot", ticker)
        return self._get_cached_data(
            cache_key,
            lambda: self._price.get_snapshot(ticker),
            "ttl_market_snapshot",
            timeout_s=_PRICE_FETCH_TIMEOUT_S,
            fallback={},
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
            self._edgar.get_edgar_ticker_list,
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
