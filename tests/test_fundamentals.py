"""
Tests for GET /{ticker}/fundamentals
"""
# pylint: disable=missing-function-docstring,redefined-outer-name
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.dependencies import get_data_source
from tests.mock_data_source import EmptyDataSource, ErrorDataSource, MockDataSource


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    """TestClient with the mock data source injected."""
    app.dependency_overrides[get_data_source] = MockDataSource
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def empty_client():
    app.dependency_overrides[get_data_source] = EmptyDataSource
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def error_client():
    app.dependency_overrides[get_data_source] = ErrorDataSource
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

def test_fundamentals_status_ok(client):
    resp = client.get("/TEST/fundamentals")
    assert resp.status_code == 200


def test_fundamentals_ticker_uppercased(client):
    resp = client.get("/test/fundamentals")
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "TEST"


def test_fundamentals_has_required_sections(client):
    body = client.get("/TEST/fundamentals").json()
    assert "ticker" in body
    assert "market_snapshot" in body
    assert "ratios_summary" in body
    assert "per_year" in body


def test_fundamentals_market_snapshot_fields(client):
    snap = client.get("/TEST/fundamentals").json()["market_snapshot"]
    assert snap["current_price"] == pytest.approx(22.0)
    assert snap["beta"] == pytest.approx(1.10)
    assert snap["week_52_high"] == pytest.approx(25.0)
    assert snap["week_52_low"] == pytest.approx(16.0)


def test_fundamentals_per_year_count(client):
    rows = client.get("/TEST/fundamentals?years=3").json()["per_year"]
    assert len(rows) == 3


def test_fundamentals_per_year_years_descending(client):
    rows = client.get("/TEST/fundamentals?years=3").json()["per_year"]
    years = [r["year"] for r in rows]
    assert years == sorted(years, reverse=True)


def test_fundamentals_per_year_income_statement(client):
    rows = client.get("/TEST/fundamentals?years=3").json()["per_year"]
    latest = rows[0]
    assert latest["year"] == 2023
    assert latest["revenue"] == pytest.approx(100_000.0)
    assert latest["net_income"] == pytest.approx(18_000.0)
    assert latest["gross_profit"] == pytest.approx(60_000.0)


def test_fundamentals_per_year_balance_sheet(client):
    latest = client.get("/TEST/fundamentals?years=3").json()["per_year"][0]
    assert latest["total_assets"] == pytest.approx(200_000.0)
    assert latest["equity"] == pytest.approx(80_000.0)
    assert latest["current_ratio"] == pytest.approx(50_000 / 30_000, rel=1e-3)


def test_fundamentals_per_year_cashflow(client):
    latest = client.get("/TEST/fundamentals?years=3").json()["per_year"][0]
    assert latest["operating_cashflow"] == pytest.approx(22_000.0)
    assert latest["free_cashflow"] == pytest.approx(17_000.0)


def test_fundamentals_ratios_summary_present(client):
    summary = client.get("/TEST/fundamentals").json()["ratios_summary"]
    for key in ("avg_pe", "avg_pb", "avg_ps", "avg_roe", "avg_roa",
                "avg_npm", "avg_gross_margin", "avg_operating_margin",
                "avg_current_ratio", "avg_debt_to_equity",
                "avg_fcf_yield", "avg_revenue_growth"):
        assert key in summary, f"Missing key: {key}"


def test_fundamentals_gross_margin_value(client):
    # gross_margin = gross_profit / revenue * 100 = 60000/100000*100 = 60%
    latest = client.get("/TEST/fundamentals?years=3").json()["per_year"][0]
    assert latest["gross_margin"] == pytest.approx(60.0, rel=1e-3)


def test_fundamentals_revenue_growth(client):
    rows = client.get("/TEST/fundamentals?years=3").json()["per_year"]
    # 2023 growth = (100000 - 90000) / 90000 * 100 ≈ 11.11%
    latest = rows[0]
    assert latest["revenue_growth"] == pytest.approx(100_000 / 90_000 * 100 - 100, rel=1e-3)


def test_fundamentals_summarize_omits_per_year(client):
    body = client.get("/TEST/fundamentals?summarize=true").json()
    assert "ratios_summary" in body
    assert "per_year" not in body
    assert "market_snapshot" not in body


def test_fundamentals_years_param_limits_rows(client):
    rows = client.get("/TEST/fundamentals?years=1").json()["per_year"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Error / edge-case tests
# ---------------------------------------------------------------------------

def test_fundamentals_empty_source_returns_404(empty_client):
    resp = empty_client.get("/NONE/fundamentals")
    assert resp.status_code == 404


def test_fundamentals_error_source_returns_404(error_client):
    resp = error_client.get("/BADTICKER/fundamentals")
    assert resp.status_code == 404


def test_fundamentals_years_out_of_range(client):
    # years must be 1-20
    assert client.get("/TEST/fundamentals?years=0").status_code == 422
    assert client.get("/TEST/fundamentals?years=21").status_code == 422
