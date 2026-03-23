"""
End-to-end API tests — local EDGAR bulk data
=============================================
These tests exercise the full HTTP → LiveDataSource → LocalEdgarFetcher →
EntityFacts → JSON response stack using data already downloaded to the local
edgar-data-fetcher/full directory.

No network calls are made.  Both EDGAR fundamentals and price data are read
from local files.  Tests assert structural correctness, not exact values.

Tickers used:
  AAPL (CIK 320193) — 10-K, US domestic — confirmed present in local data
  MSFT (CIK 789019) — 10-K, US domestic — confirmed present in local data

Run explicitly with:
    pytest tests/test_api_e2e_local.py -v

Mark: @pytest.mark.local_e2e — excluded from the standard unit-test run.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.api.app import app
from src.api.dependencies import get_data_source
from src.data.live_data_source import LiveDataSource

# ---------------------------------------------------------------------------
# Locate the local data directory relative to this file's repository root.
# edgar-data-fetcher lives alongside edgar-gmr-etl under /config/repos/.
# ---------------------------------------------------------------------------
_REPO_ROOT       = Path(__file__).resolve().parent.parent.parent  # /config/repos
_LOCAL_DATA_DIR  = _REPO_ROOT / "edgar-data-fetcher" / "full"
_LOCAL_PRICE_DIR = _REPO_ROOT / "edgar-data-fetcher" / "prices"


def _require_local_data() -> None:
    """Skip the entire module if local data is not present."""
    if not (_LOCAL_DATA_DIR / "companyfacts").is_dir():
        pytest.skip(
            f"Local EDGAR data not found at {_LOCAL_DATA_DIR}. "
            "Run edgar-data-fetcher first.",
            allow_module_level=True,
        )


_require_local_data()


# ---------------------------------------------------------------------------
# Shared fixture: TestClient with local data source injected
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """
    TestClient with LiveDataSource(local_data_dir=...) injected.
    This avoids relying on env vars or the lru_cache singleton.
    """
    local_source = LiveDataSource(
        local_data_dir=str(_LOCAL_DATA_DIR),
        local_price_data_dir=str(_LOCAL_PRICE_DIR),
    )
    app.dependency_overrides[get_data_source] = lambda: local_source
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# E2E — /health (sanity)
# ---------------------------------------------------------------------------

@pytest.mark.local_e2e
def test_local_health(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# E2E — AAPL /fundamentals (local data)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aapl_fundamentals(client):
    return client.get("/AAPL/fundamentals").json()


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_returns_200(client):
    assert client.get("/AAPL/fundamentals").status_code == 200


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_ticker(aapl_fundamentals):
    assert aapl_fundamentals["ticker"] == "AAPL"


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_top_level_keys(aapl_fundamentals):
    for key in ("ticker", "ratios_summary", "market_snapshot", "per_year"):
        assert key in aapl_fundamentals, f"Missing key: {key}"


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_per_year_non_empty(aapl_fundamentals):
    assert isinstance(aapl_fundamentals["per_year"], list)
    assert len(aapl_fundamentals["per_year"]) > 0


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_per_year_descending(aapl_fundamentals):
    years = [row["year"] for row in aapl_fundamentals["per_year"]]
    assert years == sorted(years, reverse=True)


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_revenue_positive(aapl_fundamentals):
    rows = [r for r in aapl_fundamentals["per_year"] if r.get("revenue") is not None]
    assert len(rows) > 0, "No revenue rows found"
    assert all(r["revenue"] > 0 for r in rows)


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_net_income_positive(aapl_fundamentals):
    rows = [r for r in aapl_fundamentals["per_year"] if r.get("net_income") is not None]
    assert len(rows) > 0, "No net_income rows found"
    # AAPL has been consistently profitable — all years should be positive
    assert all(r["net_income"] > 0 for r in rows)


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_ratios_summary_keys(aapl_fundamentals):
    summary = aapl_fundamentals["ratios_summary"]
    for key in ("avg_pe", "avg_pb", "avg_roe", "avg_npm", "avg_revenue_growth"):
        assert key in summary, f"Missing ratios_summary key: {key}"


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_years_param(client):
    resp = client.get("/AAPL/fundamentals?years=3")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) <= 3


@pytest.mark.local_e2e
def test_local_aapl_fundamentals_summarize(client):
    body = client.get("/AAPL/fundamentals?summarize=true").json()
    assert "ticker" in body
    assert "ratios_summary" in body
    assert "per_year" not in body
    assert "market_snapshot" not in body


# ---------------------------------------------------------------------------
# E2E — MSFT /fundamentals (local data)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def msft_fundamentals(client):
    return client.get("/MSFT/fundamentals").json()


@pytest.mark.local_e2e
def test_local_msft_fundamentals_returns_200(client):
    assert client.get("/MSFT/fundamentals").status_code == 200


@pytest.mark.local_e2e
def test_local_msft_fundamentals_ticker(msft_fundamentals):
    assert msft_fundamentals["ticker"] == "MSFT"


@pytest.mark.local_e2e
def test_local_msft_fundamentals_per_year_non_empty(msft_fundamentals):
    assert isinstance(msft_fundamentals["per_year"], list)
    assert len(msft_fundamentals["per_year"]) > 0


@pytest.mark.local_e2e
def test_local_msft_fundamentals_revenue_positive(msft_fundamentals):
    rows = [r for r in msft_fundamentals["per_year"] if r.get("revenue") is not None]
    assert len(rows) > 0
    assert all(r["revenue"] > 0 for r in rows)


@pytest.mark.local_e2e
def test_local_msft_fundamentals_ratios_summary_present(msft_fundamentals):
    assert "ratios_summary" in msft_fundamentals
    assert msft_fundamentals["ratios_summary"] is not None


# ---------------------------------------------------------------------------
# E2E — Invalid ticker → 404
# ---------------------------------------------------------------------------

@pytest.mark.local_e2e
def test_local_invalid_ticker_fundamentals_returns_404(client):
    resp = client.get("/ZZZZNOTASTOCK9999/fundamentals")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# E2E — /AAPL/gmr_long (fundamentals from local data, prices from yfinance)
# ---------------------------------------------------------------------------

@pytest.mark.local_e2e
def test_local_aapl_gmr_long_returns_200(client):
    assert client.get("/AAPL/gmr_long").status_code == 200


@pytest.mark.local_e2e
def test_local_aapl_gmr_long_top_level_keys(client):
    body = client.get("/AAPL/gmr_long").json()
    for key in ("ticker", "gmr_ratio", "market_snapshot", "per_year"):
        assert key in body, f"Missing key: {key}"


@pytest.mark.local_e2e
def test_local_aapl_gmr_long_passes_is_bool(client):
    ratio = client.get("/AAPL/gmr_long").json()["gmr_ratio"]
    assert isinstance(ratio["passes"], bool)
