"""
Integration tests for EdgarFetcher — hits the real SEC EDGAR network.

Run with:
    pytest -m slow tests/test_edgar_fetcher_integration.py -v

Coverage
--------
- ASML (ticker: ASML)  — Dutch company, files 20-F (foreign private issuer)
- RY   (ticker: RY)    — Royal Bank of Canada, files 40-F (MJDS filer)
- SHOP (ticker: SHOP)  — Shopify; has both 10-K and 40-F on EDGAR — our
                         fetcher picks 10-K first, so form_type is "10-K"

Each test verifies:
  1. The correct annual form type is detected.
  2. Core financial concepts (revenue, net_income, equity) are non-empty.
  3. The data covers at least the expected number of fiscal years.
  4. Revenue values are positive.
"""
from __future__ import annotations
# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest

from src.data.edgar_fetcher import EdgarFetcher


@pytest.fixture(scope="module")
def fetcher():
    return EdgarFetcher()


# ---------------------------------------------------------------------------
# ASML — Netherlands, Foreign Private Issuer → 20-F
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def asml_data(fetcher):
    return fetcher.fetch_fundamentals("ASML", years=5)


@pytest.mark.slow
def test_asml_uses_20f(asml_data):
    assert asml_data["form_type"] == "20-F"


@pytest.mark.slow
def test_asml_ticker(asml_data):
    assert asml_data["ticker"] == "ASML"


@pytest.mark.slow
def test_asml_revenue_non_empty(asml_data):
    assert not asml_data["revenue"].empty, "ASML revenue should not be empty"


@pytest.mark.slow
def test_asml_net_income_non_empty(asml_data):
    assert not asml_data["net_income"].empty, "ASML net_income should not be empty"


@pytest.mark.slow
def test_asml_equity_non_empty(asml_data):
    assert not asml_data["equity"].empty, "ASML equity should not be empty"


@pytest.mark.slow
def test_asml_covers_multiple_years(asml_data):
    assert len(asml_data["revenue"]) >= 3, "Expected at least 3 years of ASML revenue"


@pytest.mark.slow
def test_asml_revenue_is_positive(asml_data):
    assert (asml_data["revenue"] > 0).all(), "All ASML revenue values should be positive"


# ---------------------------------------------------------------------------
# Royal Bank of Canada — Canadian MJDS filer → 40-F
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ry_data(fetcher):
    return fetcher.fetch_fundamentals("RY", years=5)


@pytest.mark.slow
def test_ry_uses_40f(ry_data):
    assert ry_data["form_type"] == "40-F"


@pytest.mark.slow
def test_ry_ticker(ry_data):
    assert ry_data["ticker"] == "RY"


@pytest.mark.slow
def test_ry_revenue_non_empty(ry_data):
    assert not ry_data["revenue"].empty, "RY revenue should not be empty"


@pytest.mark.slow
def test_ry_net_income_non_empty(ry_data):
    assert not ry_data["net_income"].empty, "RY net_income should not be empty"


@pytest.mark.slow
def test_ry_equity_non_empty(ry_data):
    assert not ry_data["equity"].empty, "RY equity should not be empty"


@pytest.mark.slow
def test_ry_covers_multiple_years(ry_data):
    assert len(ry_data["revenue"]) >= 3, "Expected at least 3 years of RY revenue"


@pytest.mark.slow
def test_ry_revenue_is_positive(ry_data):
    assert (ry_data["revenue"] > 0).all(), "All RY revenue values should be positive"


# ---------------------------------------------------------------------------
# Shopify — Canadian company with both 10-K and 40-F on EDGAR.
# Our fetcher tries 10-K first so form_type will be "10-K".
# This test confirms Shopify data is fetchable and fundamentals are present.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shopify_data(fetcher):
    return fetcher.fetch_fundamentals("SHOP", years=5)


@pytest.mark.slow
def test_shopify_uses_annual_form(shopify_data):
    # Shopify files both 10-K and 40-F; our fetcher picks 10-K first.
    assert shopify_data["form_type"] in ("10-K", "40-F")


@pytest.mark.slow
def test_shopify_ticker(shopify_data):
    assert shopify_data["ticker"] == "SHOP"


@pytest.mark.slow
def test_shopify_revenue_non_empty(shopify_data):
    assert not shopify_data["revenue"].empty, "Shopify revenue should not be empty"


@pytest.mark.slow
def test_shopify_net_income_non_empty(shopify_data):
    assert not shopify_data["net_income"].empty, "Shopify net_income should not be empty"


@pytest.mark.slow
def test_shopify_equity_non_empty(shopify_data):
    assert not shopify_data["equity"].empty, "Shopify equity should not be empty"


@pytest.mark.slow
def test_shopify_covers_multiple_years(shopify_data):
    assert len(shopify_data["revenue"]) >= 2, "Expected at least 2 years of Shopify revenue"


@pytest.mark.slow
def test_shopify_revenue_is_positive(shopify_data):
    assert (shopify_data["revenue"] > 0).all(), "All Shopify revenue values should be positive"
