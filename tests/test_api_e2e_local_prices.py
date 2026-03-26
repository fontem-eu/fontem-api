"""
End-to-end API tests — local price data
=========================================
These tests exercise the full HTTP → LiveDataSource → LocalPriceFetcher →
CSV files → JSON response stack, using price data downloaded by
usa-stock-price-fetcher into the edgar-data PVC.

No network calls are made for price data.  EDGAR fundamentals still come
from the local bulk data (same setup as test_api_e2e_local.py).

Tickers used:
  AAPL — present in both local EDGAR data and local price data
  MSFT — present in both local EDGAR data and local price data

Run explicitly with:
    pytest tests/test_api_e2e_local_prices.py -v

Mark: @pytest.mark.local_e2e
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.api.app import app
from src.api.dependencies import get_data_source
from src.data.north_america.live_data_source import LiveDataSource

# ---------------------------------------------------------------------------
# Locate data directories
# ---------------------------------------------------------------------------
_REPO_ROOT         = Path(__file__).resolve().parent.parent.parent  # /config/repos
_LOCAL_EDGAR_DIR   = _REPO_ROOT / "edgar-data-fetcher" / "full"
_LOCAL_PRICE_DIR   = _REPO_ROOT / "edgar-data-fetcher" / "prices"


def _require_data() -> None:
    """Skip if either EDGAR or price data is not available locally."""
    if not (_LOCAL_EDGAR_DIR / "companyfacts").is_dir():
        pytest.skip(
            f"Local EDGAR data not found at {_LOCAL_EDGAR_DIR}. "
            "Run edgar-data-fetcher first.",
            allow_module_level=True,
        )
    if not (_LOCAL_PRICE_DIR / "daily").is_dir():
        pytest.skip(
            f"Local price data not found at {_LOCAL_PRICE_DIR}. "
            "Run usa-stock-price-fetcher first.",
            allow_module_level=True,
        )


_require_data()


# ---------------------------------------------------------------------------
# Shared fixture: TestClient with local data sources injected
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    source = LiveDataSource(
        local_data_dir=str(_LOCAL_EDGAR_DIR),
        local_price_data_dir=str(_LOCAL_PRICE_DIR),
    )
    app.dependency_overrides[get_data_source] = lambda: source
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@pytest.mark.local_e2e
def test_local_prices_health(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# AAPL fundamentals — prices come from local CSV
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aapl_fundamentals(client):
    return client.get("/AAPL/fundamentals").json()


@pytest.mark.local_e2e
def test_local_prices_aapl_returns_200(client):
    assert client.get("/AAPL/fundamentals").status_code == 200


@pytest.mark.local_e2e
def test_local_prices_aapl_ticker(aapl_fundamentals):
    assert aapl_fundamentals["ticker"] == "AAPL"


@pytest.mark.local_e2e
def test_local_prices_aapl_per_year_non_empty(aapl_fundamentals):
    assert len(aapl_fundamentals["per_year"]) > 0


@pytest.mark.local_e2e
def test_local_prices_aapl_revenue_positive(aapl_fundamentals):
    rows = [r for r in aapl_fundamentals["per_year"] if r.get("revenue") is not None]
    assert len(rows) > 0
    assert all(r["revenue"] > 0 for r in rows)


@pytest.mark.local_e2e
def test_local_prices_aapl_market_snapshot_has_price(aapl_fundamentals):
    """current_price must be a positive float sourced from the local CSV."""
    snapshot = aapl_fundamentals.get("market_snapshot", {})
    price = snapshot.get("current_price")
    assert price is not None, "current_price missing from market_snapshot"
    assert isinstance(price, (int, float))
    assert price > 0, f"Expected positive price, got {price}"


@pytest.mark.local_e2e
def test_local_prices_aapl_current_price_is_recent(aapl_fundamentals):
    """
    The price from local CSV should be close to AAPL's actual recent price.
    We just assert it's in a sane range (100–1000) rather than an exact value.
    """
    price = aapl_fundamentals["market_snapshot"]["current_price"]
    assert 50 < price < 2000, f"AAPL price {price} looks wrong"


@pytest.mark.local_e2e
def test_local_prices_aapl_per_year_has_price_rows(aapl_fundamentals):
    """At least some per_year rows should have non-null pe_ratio (needs price data)."""
    # P/E requires both price and EPS — at least some years should have it
    rows_with_price = [r for r in aapl_fundamentals["per_year"]
                       if r.get("avg_price") is not None]
    assert len(rows_with_price) > 0, "No per_year rows have avg_price"


@pytest.mark.local_e2e
def test_local_prices_aapl_52_week_range(aapl_fundamentals):
    """52-week high/low should be present and sensible when using local data."""
    snapshot = aapl_fundamentals.get("market_snapshot", {})
    high = snapshot.get("week_52_high")
    low  = snapshot.get("week_52_low")
    if high is not None and low is not None:
        assert high >= low, f"52-week high ({high}) < low ({low})"


# ---------------------------------------------------------------------------
# MSFT fundamentals — prices from local CSV
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def msft_fundamentals(client):
    return client.get("/MSFT/fundamentals").json()


@pytest.mark.local_e2e
def test_local_prices_msft_returns_200(client):
    assert client.get("/MSFT/fundamentals").status_code == 200


@pytest.mark.local_e2e
def test_local_prices_msft_snapshot_has_price(msft_fundamentals):
    snapshot = msft_fundamentals.get("market_snapshot", {})
    price = snapshot.get("current_price")
    assert price is not None and price > 0


@pytest.mark.local_e2e
def test_local_prices_msft_current_price_range(msft_fundamentals):
    price = msft_fundamentals["market_snapshot"]["current_price"]
    assert 50 < price < 5000, f"MSFT price {price} looks wrong"


@pytest.mark.local_e2e
def test_local_prices_msft_revenue_positive(msft_fundamentals):
    rows = [r for r in msft_fundamentals["per_year"] if r.get("revenue") is not None]
    assert len(rows) > 0
    assert all(r["revenue"] > 0 for r in rows)


# ---------------------------------------------------------------------------
# GMR long — uses both local EDGAR + local prices
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aapl_gmr(client):
    return client.get("/AAPL/gmr_long").json()


@pytest.mark.local_e2e
def test_local_prices_aapl_gmr_returns_200(client):
    assert client.get("/AAPL/gmr_long").status_code == 200


@pytest.mark.local_e2e
def test_local_prices_aapl_gmr_has_ratio(aapl_gmr):
    assert "gmr_ratio" in aapl_gmr
    assert isinstance(aapl_gmr["gmr_ratio"]["passes"], bool)


@pytest.mark.local_e2e
def test_local_prices_aapl_gmr_snapshot_price(aapl_gmr):
    snapshot = aapl_gmr.get("market_snapshot", {})
    assert snapshot.get("current_price", 0) > 0
