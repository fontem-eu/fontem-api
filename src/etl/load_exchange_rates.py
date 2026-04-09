"""
ECB Exchange Rate Loader
========================
Downloads daily exchange rates from the ECB Statistical Data Warehouse
for all currencies seen in TED procurement data. Rates are EUR-based:
"how many units of CCY per 1 EUR".

To convert from CCY to EUR: value_original / rate

Usage:
    python -m src.etl.load_exchange_rates --output data/ecb_rates.json
    python -m src.etl.load_exchange_rates --start 2024-01-01 --end 2026-04-01
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

# All currencies seen in TED data (plus common extras)
CURRENCIES = [
    "SEK", "PLN", "CZK", "HUF", "RON", "BGN", "NOK", "DKK",
    "CHF", "GBP", "ISK", "USD", "HRK", "TRY", "RSD", "MKD",
    "MDL", "GEL", "ALL", "JPY", "AUD", "CAD",
]

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
    "?startPeriod={start}&endPeriod={end}&format=csvdata"
)


def fetch_rates(
    currencies: list[str] | None = None,
    start: str = "2018-01-01",
    end: str | None = None,
) -> dict[str, dict[str, float]]:
    """Fetch daily ECB exchange rates for given currencies.

    Returns: {"SEK": {"2025-09-01": 11.34, ...}, "PLN": {...}, ...}
    Rates are units-of-CCY-per-EUR (divide original value by rate to get EUR).
    """
    if currencies is None:
        currencies = CURRENCIES
    if end is None:
        end = date.today().isoformat()

    rates: dict[str, dict[str, float]] = {}

    for ccy in currencies:
        url = ECB_URL.format(ccy=ccy, start=start, end=end)
        logger.info("Fetching %s rates (%s to %s)...", ccy, start, end)

        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch %s: %s", ccy, exc)
            continue

        daily: dict[str, float] = {}
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            period = row.get("TIME_PERIOD", "")
            obs = row.get("OBS_VALUE", "")
            if period and obs:
                try:
                    daily[period] = float(obs)
                except ValueError:
                    pass

        if daily:
            rates[ccy] = daily
            logger.info("  %s: %d daily rates (%s to %s)",
                        ccy, len(daily), min(daily), max(daily))
        else:
            logger.warning("  %s: no rates returned", ccy)

    return rates


def to_eur(
    value: float | None,
    currency: str,
    date_str: str | None,
    rates: dict[str, dict[str, float]],
) -> float | None:
    """Convert a value from CCY to EUR using the ECB rate for the given date.

    Falls back to the nearest preceding business day (up to 5 days back).
    Returns None if conversion is not possible.
    """
    if value is None:
        return None
    if currency == "EUR":
        return round(value, 2)

    daily = rates.get(currency)
    if daily is None:
        return None

    if date_str is None:
        return None

    # Try exact date first, then walk back for weekends/holidays
    try:
        d = date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return None

    for i in range(6):
        key = (d - timedelta(days=i)).isoformat()
        rate = daily.get(key)
        if rate is not None and rate > 0:
            return round(value / rate, 2)

    return None


def save_rates(rates: dict, output_path: str) -> None:
    """Save rates to JSON file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(rates, f, separators=(",", ":"))
    size_mb = os.path.getsize(output_path) / 1e6
    logger.info("Saved %d currencies to %s (%.1f MB)", len(rates), output_path, size_mb)


def load_rates(path: str) -> dict[str, dict[str, float]]:
    """Load rates from JSON file."""
    with open(path) as f:
        return json.load(f)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch ECB exchange rates")
    parser.add_argument("--output", default="data/ecb_rates.json")
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--currencies", nargs="+", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rates = fetch_rates(
        currencies=args.currencies,
        start=args.start,
        end=args.end,
    )
    save_rates(rates, args.output)


if __name__ == "__main__":
    main()
