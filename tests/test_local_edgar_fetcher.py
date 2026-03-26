"""
LocalEdgarFetcher — unit tests
==============================
Verifies the offline-first guard: unknown tickers (e.g. GALP, a Portuguese
company listed on Euronext Lisbon, not in SEC EDGAR) must raise ValueError
immediately without making any network calls.

Regression: before this guard, ``Company("GALP")`` fell through to a live
GET https://www.sec.gov/files/company_tickers_mf.json which returned 403,
causing a 500 response from every API endpoint.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.data.north_america.local_edgar_fetcher import LocalEdgarFetcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TICKERS_JSON = {
    "0": {"cik_str": 320193,  "ticker": "AAPL",  "title": "Apple Inc."},
    "1": {"cik_str": 789019,  "ticker": "MSFT",  "title": "Microsoft Corporation"},
    "2": {"cik_str": 1018724, "ticker": "AMZN",  "title": "Amazon.com Inc."},
}


def _make_local_dir() -> Path:
    """Create a temporary directory that looks like the EDGAR local store."""
    tmp = Path(tempfile.mkdtemp())
    ref = tmp / "reference"
    ref.mkdir()
    (ref / "company_tickers.json").write_text(
        json.dumps(_TICKERS_JSON), encoding="utf-8"
    )
    return tmp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fetcher():
    local_dir = _make_local_dir()
    with patch("src.data.north_america.local_edgar_fetcher.set_identity"), \
         patch("src.data.north_america.local_edgar_fetcher.use_local_storage"):
        yield LocalEdgarFetcher(str(local_dir))


# ---------------------------------------------------------------------------
# Ticker index tests
# ---------------------------------------------------------------------------

def test_known_symbols_loaded(fetcher: LocalEdgarFetcher):
    """The symbol index must contain every ticker from company_tickers.json."""
    # pylint: disable=protected-access
    assert "AAPL" in fetcher._known_symbols
    assert "MSFT" in fetcher._known_symbols
    assert "AMZN" in fetcher._known_symbols


def test_unknown_eu_ticker_not_in_index(fetcher: LocalEdgarFetcher):
    """EU tickers like GALP (Euronext Lisbon) must not appear in EDGAR index."""
    assert "GALP" not in fetcher._known_symbols  # pylint: disable=protected-access


def test_ticker_list_length(fetcher: LocalEdgarFetcher):
    assert len(fetcher.get_edgar_ticker_list()) == len(_TICKERS_JSON)


# ---------------------------------------------------------------------------
# fetch_fundamentals guard — no network calls for unknown tickers
# ---------------------------------------------------------------------------

def test_unknown_ticker_raises_value_error(fetcher: LocalEdgarFetcher):
    """fetch_fundamentals must raise ValueError for tickers not in EDGAR."""
    with pytest.raises(ValueError, match="GALP"):
        fetcher.fetch_fundamentals("GALP")


def test_unknown_ticker_does_not_call_company(fetcher: LocalEdgarFetcher):
    """Regression: Company() must never be called for unknown tickers.

    Before the guard was added, ``Company("GALP")`` triggered a live network
    request to SEC.gov which returned 403 and caused a 500 API response.
    """
    with patch("src.data.north_america.local_edgar_fetcher.Company") as mock_company:
        with pytest.raises(ValueError):
            fetcher.fetch_fundamentals("GALP")
        mock_company.assert_not_called()


def test_unknown_ticker_case_insensitive(fetcher: LocalEdgarFetcher):
    """The guard must work regardless of case (galp / Galp / GALP)."""
    for sym in ("galp", "Galp", "GALP"):
        with patch("src.data.north_america.local_edgar_fetcher.Company") as mock_company:
            with pytest.raises(ValueError):
                fetcher.fetch_fundamentals(sym)
            mock_company.assert_not_called()


def test_known_ticker_calls_company(fetcher: LocalEdgarFetcher):
    """Known tickers must proceed to Company() — guard must not block them."""
    mock_company_instance = MagicMock()
    mock_facts = MagicMock()
    mock_facts.get_all_facts.return_value = []
    mock_company_instance.get_facts.return_value = mock_facts

    with patch(
        "src.data.north_america.local_edgar_fetcher.Company",
        return_value=mock_company_instance,
    ) as mock_company:
        # AAPL is in the index — Company() should be called
        # (it will then raise because facts are empty, that's fine)
        with pytest.raises(ValueError):
            fetcher.fetch_fundamentals("AAPL")
        mock_company.assert_called_once_with("AAPL")


# ---------------------------------------------------------------------------
# get_edgar_ticker_list — must return cached data
# ---------------------------------------------------------------------------

def test_get_edgar_ticker_list_returns_list(fetcher: LocalEdgarFetcher):
    result = fetcher.get_edgar_ticker_list()
    assert isinstance(result, list)
    assert len(result) > 0


def test_get_edgar_ticker_list_cached(fetcher: LocalEdgarFetcher):
    """Calling get_edgar_ticker_list() twice must return the same object."""
    first = fetcher.get_edgar_ticker_list()
    second = fetcher.get_edgar_ticker_list()
    assert first is second


def test_get_edgar_ticker_list_structure(fetcher: LocalEdgarFetcher):
    tickers = fetcher.get_edgar_ticker_list()
    for t in tickers:
        assert "symbol" in t
        assert "cik" in t
        assert "name" in t
        assert "search_name" in t
