"""
Ticker Functionality Tests
==========================
Comprehensive tests for the ticker list and search functionality.
"""
from __future__ import annotations

import pytest
from unittest.mock import Mock, patch

from src.data.edgar_fetcher import EdgarFetcher
from src.data.live_data_source import LiveDataSource
from src.cache.fake_redis_cache import FakeRedisCache
from src.cache.config import CacheConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_edgar_fetcher():
    """Mock EdgarFetcher with sample ticker data."""
    mock = Mock(spec=EdgarFetcher)
    mock.get_edgar_ticker_list.return_value = [
        {
            'symbol': 'AAPL',
            'cik': '0000320193',
            'name': 'Apple Inc.',
            'sic': '3571',
            'sic_description': 'Electronic Computers',
            'exchange': 'NASDAQ',
            'sector': 'Technology',
            'industry': 'Consumer Electronics',
            'country': 'US',
            'currency': 'USD',
            'is_active': True,
            'last_updated': '2023-01-15',
            'search_name': 'apple inc. aapl',
            'search_keywords': 'apple computer technology consumer electronics nasdaq'
        },
        {
            'symbol': 'MSFT',
            'cik': '0000789019',
            'name': 'Microsoft Corporation',
            'sic': '7372',
            'sic_description': 'Prepackaged Software',
            'exchange': 'NASDAQ',
            'sector': 'Technology',
            'industry': 'Software',
            'country': 'US',
            'currency': 'USD',
            'is_active': True,
            'last_updated': '2023-01-15',
            'search_name': 'microsoft corporation msft',
            'search_keywords': 'microsoft software technology nasdaq'
        },
        {
            'symbol': 'GOOGL',
            'cik': '0001652044',
            'name': 'Alphabet Inc.',
            'sic': '7373',
            'sic_description': 'Computer Integrated Systems Design',
            'exchange': 'NASDAQ',
            'sector': 'Technology',
            'industry': 'Internet',
            'country': 'US',
            'currency': 'USD',
            'is_active': True,
            'last_updated': '2023-01-15',
            'search_name': 'alphabet inc. googl',
            'search_keywords': 'alphabet google internet technology nasdaq'
        }
    ]
    return mock

@pytest.fixture
def live_data_source(mock_edgar_fetcher):
    """LiveDataSource with mock fetcher and fake cache."""
    return LiveDataSource(
        edgar_fetcher=mock_edgar_fetcher,
        cache=FakeRedisCache(),
        cache_config=CacheConfig.from_env()
    )

# ---------------------------------------------------------------------------
# EdgarFetcher Tests
# ---------------------------------------------------------------------------

def test_edgar_fetcher_ticker_list(mock_edgar_fetcher):
    """Test that EdgarFetcher returns expected ticker data structure."""
    tickers = mock_edgar_fetcher.get_edgar_ticker_list()

    assert len(tickers) == 3
    assert all('symbol' in t for t in tickers)
    assert all('name' in t for t in tickers)
    assert all('cik' in t for t in tickers)
    assert all('sector' in t for t in tickers)

    # Check specific companies
    apple = next(t for t in tickers if t['symbol'] == 'AAPL')
    assert apple['name'] == 'Apple Inc.'
    assert apple['cik'] == '0000320193'
    assert apple['sector'] == 'Technology'

# ---------------------------------------------------------------------------
# LiveDataSource Tests
# ---------------------------------------------------------------------------

def test_get_available_tickers(live_data_source):
    """Test getting full ticker list through LiveDataSource."""
    tickers = live_data_source.get_available_tickers()

    assert len(tickers) == 3
    assert all('symbol' in t for t in tickers)
    assert all('search_name' in t for t in tickers)

    # Verify caching works
    tickers_cached = live_data_source.get_available_tickers()
    assert len(tickers_cached) == 3
    assert tickers == tickers_cached  # Should be identical

def test_search_tickers_exact_match(live_data_source):
    """Test searching for exact ticker match."""
    results = live_data_source.search_tickers("AAPL")

    assert len(results) == 1
    assert results[0]['symbol'] == 'AAPL'
    assert results[0]['name'] == 'Apple Inc.'

def test_search_tickers_name_match(live_data_source):
    """Test searching by company name."""
    results = live_data_source.search_tickers("Microsoft")

    assert len(results) == 1
    assert results[0]['symbol'] == 'MSFT'
    assert results[0]['name'] == 'Microsoft Corporation'

def test_search_tickers_partial_match(live_data_source):
    """Test searching with partial name."""
    results = live_data_source.search_tickers("alphabet")

    assert len(results) == 1
    assert results[0]['symbol'] == 'GOOGL'

def test_search_tickers_keyword_match(live_data_source):
    """Test searching by keywords."""
    results = live_data_source.search_tickers("software")

    assert len(results) == 1  # Only MSFT has "software" in keywords
    symbols = [t['symbol'] for t in results]
    assert 'MSFT' in symbols

def test_search_tickers_case_insensitive(live_data_source):
    """Test that search is case-insensitive."""
    results_lower = live_data_source.search_tickers("apple")
    results_upper = live_data_source.search_tickers("APPLE")
    results_mixed = live_data_source.search_tickers("ApPlE")

    assert len(results_lower) == 1
    assert len(results_upper) == 1
    assert len(results_mixed) == 1
    assert results_lower[0]['symbol'] == 'AAPL'

def test_search_tickers_limit(live_data_source):
    """Test that search respects limit parameter."""
    results = live_data_source.search_tickers("technology", limit=2)

    assert len(results) <= 2

def test_search_tickers_no_results(live_data_source):
    """Test search with no matches."""
    results = live_data_source.search_tickers("XYZNonexistentCompany")

    assert len(results) == 0

def test_search_tickers_empty_query(live_data_source):
    """Test search with empty query returns limited results."""
    results = live_data_source.search_tickers("", limit=2)

    assert len(results) == 2  # Should return first 2 tickers

# ---------------------------------------------------------------------------
# Sector/Exchange Filter Tests
# ---------------------------------------------------------------------------

def test_list_sectors(live_data_source):
    """Test getting unique sectors."""
    sectors = live_data_source.list_sectors()

    assert len(sectors) == 1  # All our mock data is in Technology
    assert 'Technology' in sectors

def test_list_exchanges(live_data_source):
    """Test getting unique exchanges."""
    exchanges = live_data_source.list_exchanges()

    assert len(exchanges) == 1  # All our mock data is on NASDAQ
    assert 'NASDAQ' in exchanges

# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

def test_ticker_data_structure(live_data_source):
    """Test that ticker data has all required fields for UI."""
    tickers = live_data_source.get_available_tickers()

    for ticker in tickers:
        # Required fields
        assert 'symbol' in ticker
        assert 'name' in ticker
        assert 'cik' in ticker

        # UI display fields
        assert 'sector' in ticker
        assert 'industry' in ticker
        assert 'exchange' in ticker

        # Search fields
        assert 'search_name' in ticker
        assert 'search_keywords' in ticker

        # Metadata
        assert 'country' in ticker
        assert 'currency' in ticker

def test_cache_behavior_tickers(live_data_source):
    """Test that ticker list uses caching properly."""
    # Clear cache first
    live_data_source.clear_cache()

    # First call - cache miss
    tickers1 = live_data_source.get_available_tickers()
    stats_after_miss = live_data_source.get_cache_stats()

    # Second call - cache hit
    tickers2 = live_data_source.get_available_tickers()
    stats_after_hit = live_data_source.get_cache_stats()

    # Verify results are identical
    assert tickers1 == tickers2

    # Verify cache stats show 1 miss and 1 hit
    assert stats_after_hit['stats']['hits'] == 1
    assert stats_after_hit['stats']['misses'] == 1
    assert stats_after_hit['stats']['sets'] == 1

# ---------------------------------------------------------------------------
# Performance Tests
# ---------------------------------------------------------------------------

def test_search_performance(live_data_source):
    """Test that search is fast even with many tickers."""
    import time

    # First search (cache miss for ticker list)
    start_time = time.time()
    results1 = live_data_source.search_tickers("technology")
    first_time = time.time() - start_time

    # Second search (cache hit for ticker list)
    start_time = time.time()
    results2 = live_data_source.search_tickers("technology")
    second_time = time.time() - start_time

    # Verify results are the same
    assert results1 == results2

    # Cached search should be faster
    assert second_time < first_time * 0.5  # At least 2x faster