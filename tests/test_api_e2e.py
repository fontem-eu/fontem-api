"""
End-to-end API tests — real EDGAR + Yahoo Finance data
=======================================================
These tests go all the way from the HTTP endpoint through the GMR logic layer
to live SEC EDGAR filings and Yahoo Finance price data.

Mark: @pytest.mark.slow  → excluded from the fast unit-test run.
Run them explicitly with:
    pytest tests/test_api_e2e.py -v

Design principles
-----------------
• We do NOT assert hardcoded values (market data changes daily).
• We DO assert structural correctness: right keys, right types, sane ranges.
• AAPL / MSFT cover the 10-K (US domestic) path.
• ASML covers the 20-F (foreign private issuer) path — Dutch company.
• RY   covers the 40-F (Canadian MJDS) path — Royal Bank of Canada.
• SHOP covers a company with both 10-K and 40-F on EDGAR.
• A deliberately invalid ticker verifies the 404 path end-to-end.

WHY these e2e tests matter
--------------------------
Unit tests mock the data source, so they never exercise the real XBRL parsing
pipeline.  Integration tests on EdgarFetcher in isolation verify data is
fetched but do not run it through Fundamentals.compute() or the HTTP layer.
Only these e2e tests exercise the full stack: HTTP → LiveDataSource →
EdgarFetcher → XBRLS.from_filings() → Fundamentals → JSON response.
That is the layer where issues such as 20-F/40-F OOM crashes or schema
mismatches surface.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
from starlette.testclient import TestClient

from src.api.app import app

# No dependency override — uses real LiveDataSource


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# E2E 1 — AAPL GMR Long (full response)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_e2e_aapl_gmr_long_returns_200(client):
    resp = client.get("/AAPL/gmr_long")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_aapl_gmr_long_has_top_level_keys(client):
    body = client.get("/AAPL/gmr_long").json()
    assert "ticker" in body
    assert "gmr_ratio" in body
    assert "market_snapshot" in body
    assert "per_year" in body


@pytest.mark.slow
def test_e2e_aapl_gmr_long_ticker_is_aapl(client):
    body = client.get("/AAPL/gmr_long").json()
    assert body["ticker"] == "AAPL"


@pytest.mark.slow
def test_e2e_aapl_gmr_long_per_year_is_non_empty(client):
    body = client.get("/AAPL/gmr_long").json()
    assert isinstance(body["per_year"], list)
    assert len(body["per_year"]) > 0


@pytest.mark.slow
def test_e2e_aapl_gmr_long_ratio_fields_present(client):
    ratio = client.get("/AAPL/gmr_long").json()["gmr_ratio"]
    for field in ("passes", "flags", "avg_pe", "avg_pb", "avg_roe",
                  "avg_npm", "avg_debt_equity", "avg_dividend_yield",
                  "avg_quick_ratio", "avg_fcf"):
        assert field in ratio, f"Missing field: {field}"


@pytest.mark.slow
def test_e2e_aapl_gmr_long_passes_is_bool(client):
    ratio = client.get("/AAPL/gmr_long").json()["gmr_ratio"]
    assert isinstance(ratio["passes"], bool)


@pytest.mark.slow
def test_e2e_aapl_gmr_long_flags_are_bools(client):
    flags = client.get("/AAPL/gmr_long").json()["gmr_ratio"]["flags"]
    for k, v in flags.items():
        assert isinstance(v, bool), f"Flag '{k}' should be bool, got {type(v)}"


@pytest.mark.slow
def test_e2e_aapl_gmr_long_revenue_is_positive(client):
    per_year = client.get("/AAPL/gmr_long").json()["per_year"]
    for row in per_year:
        if row.get("revenue") is not None:
            assert row["revenue"] > 0, f"Negative revenue in {row}"


@pytest.mark.slow
def test_e2e_aapl_gmr_long_current_price_is_positive(client):
    snap = client.get("/AAPL/gmr_long").json()["market_snapshot"]
    assert snap["current_price"] is not None
    assert snap["current_price"] > 0


# ---------------------------------------------------------------------------
# E2E 2 — AAPL GMR Short (full response)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_e2e_aapl_gmr_short_returns_200(client):
    resp = client.get("/AAPL/gmr_short")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_aapl_gmr_short_has_top_level_keys(client):
    body = client.get("/AAPL/gmr_short").json()
    assert "ticker" in body
    assert "gmr_ratio" in body
    assert "market_snapshot" in body
    assert "monthly_breakdown" in body


@pytest.mark.slow
def test_e2e_aapl_gmr_short_ratio_fields_present(client):
    ratio = client.get("/AAPL/gmr_short").json()["gmr_ratio"]
    for field in ("passes", "flags", "win_probability", "avg_v_up",
                  "avg_v_down", "mat_43d", "diff_mat_pct"):
        assert field in ratio, f"Missing field: {field}"


@pytest.mark.slow
def test_e2e_aapl_gmr_short_monthly_breakdown_not_empty(client):
    breakdown = client.get("/AAPL/gmr_short").json()["monthly_breakdown"]
    assert isinstance(breakdown, list)
    assert len(breakdown) > 0


@pytest.mark.slow
def test_e2e_aapl_gmr_short_monthly_breakdown_month_format(client):
    breakdown = client.get("/AAPL/gmr_short").json()["monthly_breakdown"]
    for entry in breakdown:
        assert isinstance(entry["month"], str)
        assert len(entry["month"]) == 7  # "YYYY-MM"


@pytest.mark.slow
def test_e2e_aapl_gmr_short_win_probability_in_range(client):
    prob = client.get("/AAPL/gmr_short").json()["gmr_ratio"]["win_probability"]
    assert prob is not None
    assert 0.0 <= prob <= 1.0, f"win_probability out of range: {prob}"


# ---------------------------------------------------------------------------
# E2E 3 — MSFT GMR Long summarized
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_e2e_msft_gmr_long_summarize_returns_200(client):
    resp = client.get("/MSFT/gmr_long?summarize=true")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_msft_gmr_long_summarize_compact_shape(client):
    body = client.get("/MSFT/gmr_long?summarize=true").json()
    assert set(body.keys()) == {"ticker", "gmr_ratio"}
    assert "per_year" not in body
    assert "market_snapshot" not in body


@pytest.mark.slow
def test_e2e_msft_gmr_long_summarize_ticker(client):
    body = client.get("/MSFT/gmr_long?summarize=true").json()
    assert body["ticker"] == "MSFT"


# ---------------------------------------------------------------------------
# E2E 4 — Invalid ticker → 404
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_e2e_invalid_ticker_gmr_long_returns_404(client):
    resp = client.get("/ZZZZNOTASTOCK9999/gmr_long")
    assert resp.status_code == 404


@pytest.mark.slow
def test_e2e_invalid_ticker_gmr_short_returns_404(client):
    resp = client.get("/ZZZZNOTASTOCK9999/gmr_short")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# E2E 5 — AAPL /fundamentals (full response)
# ---------------------------------------------------------------------------

FUNDAMENTALS_SUMMARY_KEYS = [
    "avg_pe", "avg_pb", "avg_ps",
    "avg_roe", "avg_roa", "avg_npm", "avg_gross_margin", "avg_operating_margin",
    "avg_current_ratio", "avg_quick_ratio", "avg_debt_to_equity", "avg_debt_to_assets",
    "avg_fcf_yield", "avg_dividend_yield",
    "avg_revenue_growth", "avg_earnings_growth",
]


@pytest.mark.slow
def test_e2e_aapl_fundamentals_returns_200(client):
    resp = client.get("/AAPL/fundamentals")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_aapl_fundamentals_ticker(client):
    body = client.get("/AAPL/fundamentals").json()
    assert body["ticker"] == "AAPL"


@pytest.mark.slow
def test_e2e_aapl_fundamentals_top_level_keys(client):
    body = client.get("/AAPL/fundamentals").json()
    assert "ticker" in body
    assert "ratios_summary" in body
    assert "market_snapshot" in body
    assert "per_year" in body


@pytest.mark.slow
def test_e2e_aapl_fundamentals_summary_keys_present(client):
    summary = client.get("/AAPL/fundamentals").json()["ratios_summary"]
    for key in FUNDAMENTALS_SUMMARY_KEYS:
        assert key in summary, f"Missing key in ratios_summary: {key}"


@pytest.mark.slow
def test_e2e_aapl_fundamentals_per_year_non_empty(client):
    per_year = client.get("/AAPL/fundamentals").json()["per_year"]
    assert isinstance(per_year, list)
    assert len(per_year) > 0


@pytest.mark.slow
def test_e2e_aapl_fundamentals_per_year_descending(client):
    per_year = client.get("/AAPL/fundamentals").json()["per_year"]
    years = [e["year"] for e in per_year]
    assert years == sorted(years, reverse=True)


@pytest.mark.slow
def test_e2e_aapl_fundamentals_revenue_positive(client):
    per_year = client.get("/AAPL/fundamentals").json()["per_year"]
    for row in per_year:
        if row.get("revenue") is not None:
            assert row["revenue"] > 0


@pytest.mark.slow
def test_e2e_aapl_fundamentals_market_cap_positive(client):
    snap = client.get("/AAPL/fundamentals").json()["market_snapshot"]
    assert snap.get("market_cap") is not None
    assert snap["market_cap"] > 0


@pytest.mark.slow
def test_e2e_aapl_fundamentals_years_param(client):
    body = client.get("/AAPL/fundamentals?years=3").json()
    assert body["status_code"] if "status_code" in body else True  # 200 checked below
    resp = client.get("/AAPL/fundamentals?years=3")
    assert resp.status_code == 200
    assert len(resp.json()["per_year"]) <= 3


# ---------------------------------------------------------------------------
# E2E 6 — MSFT /fundamentals summarized
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_e2e_msft_fundamentals_summarize_returns_200(client):
    resp = client.get("/MSFT/fundamentals?summarize=true")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_msft_fundamentals_summarize_shape(client):
    body = client.get("/MSFT/fundamentals?summarize=true").json()
    assert "ticker" in body
    assert "ratios_summary" in body
    assert "per_year" not in body
    assert "market_snapshot" not in body


@pytest.mark.slow
def test_e2e_msft_fundamentals_summarize_ticker(client):
    body = client.get("/MSFT/fundamentals?summarize=true").json()
    assert body["ticker"] == "MSFT"


# ---------------------------------------------------------------------------
# E2E 7 — Invalid ticker → 404
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_e2e_invalid_ticker_fundamentals_returns_404(client):
    resp = client.get("/ZZZZNOTASTOCK9999/fundamentals")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# E2E 8 — ASML /fundamentals  (20-F — Dutch foreign private issuer)
#
# This test exists to protect the 20-F filing path end-to-end.
# EdgarFetcher-level tests verify data is fetched, but only these e2e tests
# exercise the full stack: XBRLS.from_filings() → Fundamentals.compute() →
# HTTP response.  A regression here (e.g. OOM, schema mismatch) would cause
# a 502/404 in production without being caught by the unit or fetcher tests.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def asml_fundamentals(client):
    return client.get("/ASML/fundamentals").json()


@pytest.mark.slow
def test_e2e_asml_fundamentals_returns_200(client):
    resp = client.get("/ASML/fundamentals")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_asml_fundamentals_ticker(asml_fundamentals):
    assert asml_fundamentals["ticker"] == "ASML"


@pytest.mark.slow
def test_e2e_asml_fundamentals_per_year_non_empty(asml_fundamentals):
    assert isinstance(asml_fundamentals["per_year"], list)
    assert len(asml_fundamentals["per_year"]) >= 3


@pytest.mark.slow
def test_e2e_asml_fundamentals_revenue_positive(asml_fundamentals):
    rows_with_revenue = [r for r in asml_fundamentals["per_year"] if r.get("revenue")]
    assert len(rows_with_revenue) >= 1
    assert all(r["revenue"] > 0 for r in rows_with_revenue)


@pytest.mark.slow
def test_e2e_asml_fundamentals_has_ratios_summary(asml_fundamentals):
    assert "ratios_summary" in asml_fundamentals
    assert asml_fundamentals["ratios_summary"] is not None


# ---------------------------------------------------------------------------
# E2E 9 — RY /fundamentals  (40-F — Royal Bank of Canada, MJDS filer)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ry_fundamentals(client):
    return client.get("/RY/fundamentals").json()


@pytest.mark.slow
def test_e2e_ry_fundamentals_returns_200(client):
    resp = client.get("/RY/fundamentals")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_ry_fundamentals_ticker(ry_fundamentals):
    assert ry_fundamentals["ticker"] == "RY"


@pytest.mark.slow
def test_e2e_ry_fundamentals_per_year_non_empty(ry_fundamentals):
    assert isinstance(ry_fundamentals["per_year"], list)
    assert len(ry_fundamentals["per_year"]) >= 3


@pytest.mark.slow
def test_e2e_ry_fundamentals_revenue_positive(ry_fundamentals):
    rows_with_revenue = [r for r in ry_fundamentals["per_year"] if r.get("revenue")]
    assert len(rows_with_revenue) >= 1
    assert all(r["revenue"] > 0 for r in rows_with_revenue)


@pytest.mark.slow
def test_e2e_ry_fundamentals_has_ratios_summary(ry_fundamentals):
    assert "ratios_summary" in ry_fundamentals
    assert ry_fundamentals["ratios_summary"] is not None


# ---------------------------------------------------------------------------
# E2E 10 — SHOP /fundamentals  (Shopify — has both 10-K and 40-F on EDGAR)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shop_fundamentals(client):
    return client.get("/SHOP/fundamentals").json()


@pytest.mark.slow
def test_e2e_shop_fundamentals_returns_200(client):
    resp = client.get("/SHOP/fundamentals")
    assert resp.status_code == 200, resp.text


@pytest.mark.slow
def test_e2e_shop_fundamentals_ticker(shop_fundamentals):
    assert shop_fundamentals["ticker"] == "SHOP"


@pytest.mark.slow
def test_e2e_shop_fundamentals_per_year_non_empty(shop_fundamentals):
    assert isinstance(shop_fundamentals["per_year"], list)
    assert len(shop_fundamentals["per_year"]) >= 2


@pytest.mark.slow
def test_e2e_shop_fundamentals_revenue_positive(shop_fundamentals):
    rows_with_revenue = [r for r in shop_fundamentals["per_year"] if r.get("revenue")]
    assert len(rows_with_revenue) >= 1
    assert all(r["revenue"] > 0 for r in rows_with_revenue)


@pytest.mark.slow
def test_e2e_shop_fundamentals_has_ratios_summary(shop_fundamentals):
    assert "ratios_summary" in shop_fundamentals
    assert shop_fundamentals["ratios_summary"] is not None
