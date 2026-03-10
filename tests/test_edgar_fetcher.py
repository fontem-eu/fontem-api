"""
Integration tests for EdgarFetcher (edgartools).
These hit the real SEC EDGAR API — run with network access.
"""
import pytest
import pandas as pd
from src.data.edgar_fetcher import EdgarFetcher

TICKER = "AAPL"
IDENTITY = "test@bemar-edgar.com"

@pytest.fixture(scope="module")
def fundamentals():
    fetcher = EdgarFetcher(identity=IDENTITY)
    return fetcher.fetch_fundamentals(TICKER, years=5)

# ── top-level structure ────────────────────────────────────────────────────

def test_returns_dict(fundamentals):
    assert isinstance(fundamentals, dict)

def test_ticker_key(fundamentals):
    assert fundamentals["ticker"] == TICKER

def test_all_expected_keys_present(fundamentals):
    expected = [
        "revenue", "net_income", "total_assets", "total_liabilities",
        "equity", "operating_cashflow", "capex", "free_cashflow",
        "current_assets", "current_liabilities",
        "inventory", "prepaid_expenses",
        "shares_outstanding", "eps",
    ]
    for key in expected:
        assert key in fundamentals, f"Missing key: {key}"

# ── revenue ────────────────────────────────────────────────────────────────

def test_revenue_is_series(fundamentals):
    assert isinstance(fundamentals["revenue"], pd.Series)

def test_revenue_not_empty(fundamentals):
    assert not fundamentals["revenue"].empty, "Revenue series is empty"

def test_revenue_index_is_integers(fundamentals):
    rev = fundamentals["revenue"]
    assert rev.index.dtype == int or all(isinstance(i, (int,)) for i in rev.index), \
        f"Expected int index, got {rev.index.dtype}"

def test_revenue_values_positive(fundamentals):
    rev = fundamentals["revenue"]
    assert (rev > 0).all(), f"Some revenue values are not positive: {rev}"

def test_revenue_sorted_descending(fundamentals):
    rev = fundamentals["revenue"]
    assert list(rev.index) == sorted(rev.index, reverse=True), \
        "Revenue index should be sorted descending (most recent first)"

# ── net income ─────────────────────────────────────────────────────────────

def test_net_income_is_series(fundamentals):
    assert isinstance(fundamentals["net_income"], pd.Series)

def test_net_income_not_empty(fundamentals):
    assert not fundamentals["net_income"].empty, "Net income series is empty"

# ── equity ─────────────────────────────────────────────────────────────────

def test_equity_not_empty(fundamentals):
    assert not fundamentals["equity"].empty, "Equity series is empty"

# ── raw dataframes available ───────────────────────────────────────────────

def test_raw_balance_sheet_available(fundamentals):
    bs = fundamentals["_balance_sheet"]
    assert isinstance(bs, pd.DataFrame)
    assert not bs.empty

def test_raw_income_available(fundamentals):
    inc = fundamentals["_income"]
    assert isinstance(inc, pd.DataFrame)
    assert not inc.empty

def test_raw_cashflow_available(fundamentals):
    cf = fundamentals["_cashflow"]
    assert isinstance(cf, pd.DataFrame)
    assert not cf.empty

# ── capex ──────────────────────────────────────────────────────────────────

def test_capex_is_series(fundamentals):
    assert isinstance(fundamentals["capex"], pd.Series)

def test_capex_not_empty(fundamentals):
    assert not fundamentals["capex"].empty, "CapEx series is empty (check _CAPEX labels)"

def test_capex_values_positive(fundamentals):
    """CapEx is stored as positive magnitude (we flipped the sign from CF statement)."""
    capex = fundamentals["capex"]
    if not capex.empty:
        assert (capex > 0).all(), f"CapEx should be positive after sign flip: {capex}"

# ── free cash flow ─────────────────────────────────────────────────────────

def test_free_cashflow_is_series(fundamentals):
    assert isinstance(fundamentals["free_cashflow"], pd.Series)

def test_free_cashflow_not_empty(fundamentals):
    assert not fundamentals["free_cashflow"].empty, "FCF series is empty"

def test_free_cashflow_is_positive_for_aapl(fundamentals):
    """AAPL generates substantial FCF every year."""
    fcf = fundamentals["free_cashflow"]
    if not fcf.empty:
        assert (fcf > 0).all(), f"AAPL FCF should always be positive: {fcf}"

# ── inventory ──────────────────────────────────────────────────────────────

def test_inventory_is_series(fundamentals):
    assert isinstance(fundamentals["inventory"], pd.Series)

def test_inventory_not_empty(fundamentals):
    """AAPL is a hardware company and reports inventory."""
    assert not fundamentals["inventory"].empty, "Inventory series is empty"

def test_inventory_values_positive(fundamentals):
    inv = fundamentals["inventory"]
    if not inv.empty:
        assert (inv > 0).all(), f"Inventory should be positive: {inv}"

# ── prepaid expenses ───────────────────────────────────────────────────────

def test_prepaid_expenses_is_series(fundamentals):
    assert isinstance(fundamentals["prepaid_expenses"], pd.Series)

# (prepaid may be empty for some companies — we only assert type here)

# ── total_assets ───────────────────────────────────────────────────────────

def test_total_assets_is_series(fundamentals):
    assert isinstance(fundamentals["total_assets"], pd.Series)

def test_total_assets_not_empty(fundamentals):
    assert not fundamentals["total_assets"].empty, "Total assets series is empty"

def test_total_assets_values_positive(fundamentals):
    ta = fundamentals["total_assets"]
    if not ta.empty:
        assert (ta > 0).all(), f"Total assets should be positive: {ta}"

def test_total_assets_sorted_descending(fundamentals):
    ta = fundamentals["total_assets"]
    if not ta.empty:
        assert list(ta.index) == sorted(ta.index, reverse=True)

def test_total_assets_greater_than_current_assets(fundamentals):
    """Total assets ≥ current assets by balance-sheet identity."""
    ta = fundamentals["total_assets"]
    ca = fundamentals["current_assets"]
    if not ta.empty and not ca.empty:
        common = ta.index.intersection(ca.index)
        assert (ta[common] >= ca[common]).all(), \
            "Total assets should always be >= current assets"

# ── operating_cashflow ─────────────────────────────────────────────────────

def test_operating_cashflow_is_series(fundamentals):
    assert isinstance(fundamentals["operating_cashflow"], pd.Series)

def test_operating_cashflow_not_empty(fundamentals):
    assert not fundamentals["operating_cashflow"].empty, "Operating cash flow series is empty"

def test_operating_cashflow_positive_for_aapl(fundamentals):
    """AAPL generates positive operating cash flow every year."""
    ocf = fundamentals["operating_cashflow"]
    if not ocf.empty:
        assert (ocf > 0).all(), f"AAPL operating CF should always be positive: {ocf}"

def test_operating_cashflow_greater_than_free_cashflow(fundamentals):
    """OCF ≥ FCF (since FCF = OCF − CapEx and CapEx > 0)."""
    ocf = fundamentals["operating_cashflow"]
    fcf = fundamentals["free_cashflow"]
    if not ocf.empty and not fcf.empty:
        common = ocf.index.intersection(fcf.index)
        assert (ocf[common] >= fcf[common]).all(), \
            "Operating CF should be >= free cash flow (FCF = OCF - CapEx)"
