"""
Tests for GET /{ticker}/valuation
"""
# pylint: disable=missing-function-docstring,redefined-outer-name
from __future__ import annotations

import pytest

from tests.dishka_fixtures import make_test_client, cleanup_dishka
from tests.mock_data_source import EmptyDataSource, ErrorDataSource, MockDataSource


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    yield make_test_client(MockDataSource)
    cleanup_dishka()


@pytest.fixture()
def empty_client():
    yield make_test_client(EmptyDataSource)
    cleanup_dishka()


@pytest.fixture()
def error_client():
    yield make_test_client(ErrorDataSource)
    cleanup_dishka()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

def test_valuation_status_ok(client):
    resp = client.get("/TEST/valuation")
    assert resp.status_code == 200


def test_valuation_ticker_uppercased(client):
    resp = client.get("/test/valuation")
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "TEST"


def test_valuation_has_required_sections(client):
    body = client.get("/TEST/valuation").json()
    assert "ticker" in body
    assert "valuation_snapshot" in body
    assert "summary" in body
    assert "per_year" in body


def test_valuation_snapshot_enterprise_value(client):
    # market_cap = 22.0 * 10_000 = 220_000
    # net_debt (2023) = 40_000 - 15_000 = 25_000
    # EV = 245_000
    snap = client.get("/TEST/valuation").json()["valuation_snapshot"]
    assert snap["enterprise_value"] == pytest.approx(245_000.0, rel=1e-3)
    assert snap["market_cap"] == pytest.approx(220_000.0, rel=1e-3)


def test_valuation_snapshot_ev_ebitda(client):
    # EBITDA 2023 = operating_income + da = 25_000 + 4_000 = 29_000
    # EV/EBITDA = 245_000 / 29_000 ≈ 8.448
    snap = client.get("/TEST/valuation").json()["valuation_snapshot"]
    assert snap["ev_ebitda"] == pytest.approx(245_000 / 29_000, rel=1e-2)


def test_valuation_snapshot_ev_revenue(client):
    # EV/Revenue = 245_000 / 100_000 = 2.45
    snap = client.get("/TEST/valuation").json()["valuation_snapshot"]
    assert snap["ev_revenue"] == pytest.approx(2.45, rel=1e-3)


def test_valuation_snapshot_ev_fcf(client):
    # EV/FCF = 245_000 / 17_000 ≈ 14.41
    snap = client.get("/TEST/valuation").json()["valuation_snapshot"]
    assert snap["ev_fcf"] == pytest.approx(245_000 / 17_000, rel=1e-2)


def test_valuation_snapshot_ev_ebit(client):
    # EV/EBIT = 245_000 / 25_000 = 9.8
    snap = client.get("/TEST/valuation").json()["valuation_snapshot"]
    assert snap["ev_ebit"] == pytest.approx(9.8, rel=1e-3)


def test_valuation_per_year_count(client):
    rows = client.get("/TEST/valuation?years=3").json()["per_year"]
    assert len(rows) == 3


def test_valuation_per_year_years_descending(client):
    rows = client.get("/TEST/valuation?years=3").json()["per_year"]
    years = [r["year"] for r in rows]
    assert years == sorted(years, reverse=True)


def test_valuation_per_year_ebitda(client):
    # 2023: EBITDA = 25_000 + 4_000 = 29_000
    latest = client.get("/TEST/valuation?years=3").json()["per_year"][0]
    assert latest["year"] == 2023
    assert latest["ebitda"] == pytest.approx(29_000.0, rel=1e-3)


def test_valuation_per_year_ebitda_margin(client):
    # 2023: EBITDA margin = 29_000 / 100_000 * 100 = 29%
    latest = client.get("/TEST/valuation?years=3").json()["per_year"][0]
    assert latest["ebitda_margin"] == pytest.approx(29.0, rel=1e-3)


def test_valuation_per_year_net_debt(client):
    # 2023: net_debt = 40_000 - 15_000 = 25_000
    latest = client.get("/TEST/valuation?years=3").json()["per_year"][0]
    assert latest["net_debt"] == pytest.approx(25_000.0, rel=1e-3)


def test_valuation_per_year_interest_coverage(client):
    # 2023: interest_coverage = 25_000 / 2_000 = 12.5
    latest = client.get("/TEST/valuation?years=3").json()["per_year"][0]
    assert latest["interest_coverage"] == pytest.approx(12.5, rel=1e-3)


def test_valuation_per_year_roic(client):
    # 2023:
    #   effective_tax_rate = 5000 / (18000 + 5000) ≈ 0.2174
    #   nopat = 25000 * (1 - 0.2174) ≈ 19565
    #   invested_capital = 80000 + 40000 - 15000 = 105000
    #   roic = 19565 / 105000 * 100 ≈ 18.63%
    latest = client.get("/TEST/valuation?years=3").json()["per_year"][0]
    eff_tax = 5_000 / (18_000 + 5_000)
    nopat = 25_000 * (1 - eff_tax)
    invested_capital = 80_000 + 40_000 - 15_000
    expected_roic = nopat / invested_capital * 100
    assert latest["roic"] == pytest.approx(expected_roic, rel=1e-2)


def test_valuation_per_year_net_debt_to_ebitda(client):
    # 2023: net_debt/ebitda = 25_000 / 29_000 ≈ 0.862
    latest = client.get("/TEST/valuation?years=3").json()["per_year"][0]
    assert latest["net_debt_to_ebitda"] == pytest.approx(25_000 / 29_000, rel=1e-2)


def test_valuation_summary_has_all_keys(client):
    summary = client.get("/TEST/valuation").json()["summary"]
    for key in ("avg_ebitda_margin", "avg_roic", "avg_interest_coverage",
                "avg_net_debt_to_ebitda"):
        assert key in summary, f"Missing key: {key}"


def test_valuation_summarize_omits_per_year(client):
    body = client.get("/TEST/valuation?summarize=true").json()
    assert "summary" in body
    assert "per_year" not in body
    assert "valuation_snapshot" not in body


def test_valuation_years_param_limits_rows(client):
    rows = client.get("/TEST/valuation?years=1").json()["per_year"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Error / edge-case tests
# ---------------------------------------------------------------------------

def test_valuation_empty_source_returns_404(empty_client):
    resp = empty_client.get("/NONE/valuation")
    assert resp.status_code == 404


def test_valuation_error_source_returns_404(error_client):
    resp = error_client.get("/BADTICKER/valuation")
    assert resp.status_code == 404


def test_valuation_years_out_of_range(client):
    assert client.get("/TEST/valuation?years=0").status_code == 422
    assert client.get("/TEST/valuation?years=21").status_code == 422
