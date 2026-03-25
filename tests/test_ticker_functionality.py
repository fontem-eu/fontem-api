"""
Ticker Functionality Tests
==========================
Tests for the ticker list and search functionality in LiveDataSource.
Uses a mocked LocalEdgarFetcher — no filesystem or network access.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

from unittest.mock import patch

import pytest

from src.data.live_data_source import LiveDataSource

# ---------------------------------------------------------------------------
# Sample ticker data
# ---------------------------------------------------------------------------

_SAMPLE_TICKERS = [
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
        'search_keywords': 'apple computer technology consumer electronics nasdaq',
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
        'search_keywords': 'microsoft software technology nasdaq',
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
        'search_keywords': 'alphabet google internet technology nasdaq',
    },
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def live_data_source():
    with patch('src.data.live_data_source.LocalEdgarFetcher') as mock_edgar_cls, \
         patch('src.data.live_data_source.LocalPriceFetcher'):
        mock_edgar_cls.return_value.get_edgar_ticker_list.return_value = _SAMPLE_TICKERS
        yield LiveDataSource(local_data_dir='/fake', local_price_data_dir='/fake')

# ---------------------------------------------------------------------------
# LiveDataSource.get_available_tickers
# ---------------------------------------------------------------------------

def test_get_available_tickers(live_data_source):
    tickers = live_data_source.get_available_tickers()
    assert len(tickers) == 3
    assert all('symbol' in t for t in tickers)
    assert all('search_name' in t for t in tickers)

def test_get_available_tickers_returns_same_list_on_repeat_call(live_data_source):
    tickers1 = live_data_source.get_available_tickers()
    tickers2 = live_data_source.get_available_tickers()
    assert tickers1 == tickers2

# ---------------------------------------------------------------------------
# LiveDataSource.search_tickers
# ---------------------------------------------------------------------------

def test_search_tickers_exact_match(live_data_source):
    results = live_data_source.search_tickers("AAPL")
    assert len(results) == 1
    assert results[0]['symbol'] == 'AAPL'

def test_search_tickers_name_match(live_data_source):
    results = live_data_source.search_tickers("Microsoft")
    assert len(results) == 1
    assert results[0]['symbol'] == 'MSFT'

def test_search_tickers_partial_match(live_data_source):
    results = live_data_source.search_tickers("alphabet")
    assert len(results) == 1
    assert results[0]['symbol'] == 'GOOGL'

def test_search_tickers_keyword_match(live_data_source):
    results = live_data_source.search_tickers("software")
    assert len(results) == 1
    assert results[0]['symbol'] == 'MSFT'

def test_search_tickers_case_insensitive(live_data_source):
    for query in ("apple", "APPLE", "ApPlE"):
        results = live_data_source.search_tickers(query)
        assert len(results) == 1
        assert results[0]['symbol'] == 'AAPL'

def test_search_tickers_limit(live_data_source):
    results = live_data_source.search_tickers("technology", limit=2)
    assert len(results) <= 2

def test_search_tickers_no_results(live_data_source):
    assert live_data_source.search_tickers("XYZNonexistentCompany") == []

def test_search_tickers_empty_query(live_data_source):
    results = live_data_source.search_tickers("", limit=2)
    assert len(results) == 2

# ---------------------------------------------------------------------------
# Ticker data structure
# ---------------------------------------------------------------------------

def test_ticker_data_structure(live_data_source):
    for ticker in live_data_source.get_available_tickers():
        for field in ('symbol', 'name', 'cik', 'sector', 'industry',
                      'exchange', 'search_name', 'search_keywords',
                      'country', 'currency'):
            assert field in ticker
