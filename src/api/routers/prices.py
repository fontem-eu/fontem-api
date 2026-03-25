"""
Price History Endpoint
=======================
GET /{ticker}/prices

Returns daily OHLCV price bars for a given ticker from local CSV files.

Query parameters:
  period — one of: 1m, 6m, 1y, 3y, 5y, all  (default: 1y)

Response includes:
  • ticker    — normalised symbol
  • name      — company name from EDGAR (null if not found)
  • exchange  — exchange from EDGAR (null if not found)
  • period    — echoed query param
  • bars      — list of daily OHLCV dicts

A 404 is returned when no local price data exists for the ticker.
"""
# pylint: disable=duplicate-code
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from src.analysis.gmr_data_source import FinancialDataSource
from src.api.dependencies import get_data_source
from src.api.schemas.prices import PriceBar, PricesResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Prices"])

# Maps the user-facing period strings to the LocalPriceFetcher period strings.
_PERIOD_MAP: dict[str, str] = {
    "1m":  "1mo",
    "6m":  "6mo",
    "1y":  "1y",
    "3y":  "3y",
    "5y":  "5y",
    "all": "max",
}


def _lookup_company(
    data_source: FinancialDataSource, ticker: str
) -> tuple[str | None, str | None]:
    """Return (name, exchange) from the EDGAR ticker list, or (None, None)."""
    try:
        tickers = data_source.get_available_tickers()
        for entry in tickers:
            if entry.get("symbol", "").upper() == ticker:
                return entry.get("name"), entry.get("exchange")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return None, None


@router.get(
    "/{ticker}/prices",
    response_model=PricesResponse,
    response_model_exclude_none=True,
    summary="OHLCV Price History",
    description=(
        "Returns daily OHLCV price bars for a given ticker from locally "
        "cached CSV data.  Use `?period=` to select the look-back window: "
        "**1m** (1 month, default for the UI), **6m**, **1y**, **3y**, **5y**, "
        "or **all** (full history).  A 404 is returned when no local CSV "
        "exists for the requested ticker."
    ),
)
def get_prices(
    ticker: str,
    period: str = Query(
        default="1y",
        description="Look-back period: 1m, 6m, 1y, 3y, 5y, all",
    ),
    data_source: FinancialDataSource = Depends(get_data_source),
) -> PricesResponse:
    """Return OHLCV price bars for a given ticker."""
    ticker = ticker.upper()
    internal_period = _PERIOD_MAP.get(period, "1y")

    try:
        df = data_source.get_price_history(ticker, period=internal_period)
    except (ValueError, LookupError) as exc:
        logger.warning("404 prices %s: %s", ticker, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if df is None or df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No price data found for ticker '{ticker}'",
        )

    name, exchange = _lookup_company(data_source, ticker)

    bars = [
        PriceBar(
            date=str(dt.date()),
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=float(row["Volume"]),
        )
        for dt, row in df.iterrows()
    ]

    return PricesResponse(
        ticker=ticker,
        name=name,
        exchange=exchange,
        period=period,
        bars=bars,
    )
