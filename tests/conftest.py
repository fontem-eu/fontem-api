"""
pytest configuration / session-scoped fixtures shared across all test modules.
"""
import pytest
import yfinance as yf


@pytest.fixture(scope="session", autouse=True)
def warm_yfinance_session():
    """
    Pre-warm the yfinance curl_cffi HTTP session before any test runs.

    On this server the very first yfinance API call reliably fails with
    ``TypeError: 'NoneType' object is not subscriptable`` while the session
    authenticates with Yahoo Finance.  Retrying a few times here means all
    individual tests start with a ready session.
    """
    for _ in range(5):
        df = yf.download("AAPL", period="5d", progress=False, multi_level_index=False)
        if not df.empty:
            break
