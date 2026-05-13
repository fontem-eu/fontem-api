"""
Exchange Rate Loader
====================
Downloads daily exchange rates from the ECB Statistical Data
Warehouse for all currencies seen in TED procurement data, plus
secondary sources for currencies ECB doesn't cover (MDL, MKD,
UAH, RSD, BAM, etc.).

Two outputs:
  1. Per-currency JSON files in ``{rates_dir}/rates/{CCY}.json``
     (consumed by the existing CurrencyService API).
  2. ``UpsertExchangeRate`` events emitted into the event log so
     the rates land in both projection stores.

Each rate is base=EUR, target=<CCY>, units of <CCY> per 1 EUR.

Usage:
    python -m src.etl.load_exchange_rates --rates-dir /srv/nfs/currency-data
    python -m src.etl.load_exchange_rates --rates-dir /tmp/currency --start 2000-01-01
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import uuid
from datetime import date
from pathlib import Path

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

logger = logging.getLogger(__name__)

# Currencies the ECB publishes daily reference rates for
ECB_CURRENCIES = [
    "USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK",
    "CHF", "ISK", "NOK", "TRY", "AUD", "BRL", "CAD", "CNY", "HKD", "IDR",
    "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB", "ZAR",
    # Historical (locked currencies, ECB still has the rates pre-lock)
    "HRK",
]

# Currencies needed by TED data but NOT in ECB. We use exchangerate.host
# (free, no auth, daily rates back to 2000).
NON_ECB_CURRENCIES = [
    "MDL",  # Moldovan leu
    "MKD",  # Macedonian denar
    "UAH",  # Ukrainian hryvnia
    "RSD",  # Serbian dinar
    "BAM",  # Bosnia and Herzegovina convertible mark
    "MAD",  # Moroccan dirham
    "TND",  # Tunisian dinar
    "AMD",  # Armenian dram
    "AWG",  # Aruban florin
    "GEL",  # Georgian lari
    "ALL",  # Albanian lek
    "DZD",  # Algerian dinar
    "EGP",  # Egyptian pound
    "ARS",  # Argentine peso
    "RUB",  # Russian ruble
]

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
    "?startPeriod={start}&endPeriod={end}&format=csvdata"
)

# Frankfurter is a free, simple, ECB-mirror API with broader currency support
# (uses ECB data + Yahoo Finance for the rest). No auth required.
# https://www.frankfurter.app
FRANKFURTER_URL = "https://api.frankfurter.app/{start}..{end}?from=EUR&to={ccy}"


def fetch_ecb(ccy: str, start: str, end: str) -> dict[str, str]:
    """Fetch ECB daily reference rates for a currency."""
    url = ECB_URL.format(ccy=ccy, start=start, end=end)
    try:
        resp = httpx.get(url, timeout=60, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("ECB fetch failed for %s: %s", ccy, exc)
        return {}

    daily: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        period = row.get("TIME_PERIOD", "")
        obs = row.get("OBS_VALUE", "")
        if period and obs:
            try:
                # Validate it's a number, but store as string for Decimal precision
                float(obs)
                daily[period] = obs
            except ValueError:
                pass
    return daily


def fetch_frankfurter(ccy: str, start: str, end: str) -> dict[str, str]:
    """Fetch rates via Frankfurter API (covers more currencies than ECB).

    Frankfurter has a date range limit; chunk by year to be safe.
    """
    daily: dict[str, str] = {}
    start_year = int(start[:4])
    end_year = int(end[:4])

    for year in range(start_year, end_year + 1):
        chunk_start = f"{year}-01-01" if year > start_year else start
        chunk_end = f"{year}-12-31" if year < end_year else end
        url = FRANKFURTER_URL.format(start=chunk_start, end=chunk_end, ccy=ccy)
        try:
            resp = httpx.get(url, timeout=60, follow_redirects=True)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for d, rates in data.get("rates", {}).items():
                if ccy in rates:
                    daily[d] = str(rates[ccy])
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.warning("Frankfurter fetch failed for %s %s: %s", ccy, year, exc)

    return daily


def save_currency_file(rates_dir: Path, ccy: str, daily: dict[str, str]) -> None:
    """Write a per-currency JSON file."""
    rates_subdir = rates_dir / "rates"
    rates_subdir.mkdir(parents=True, exist_ok=True)
    out = rates_subdir / f"{ccy}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(daily, f, separators=(",", ":"), sort_keys=True)
    logger.info("  %s: %d rates saved (%s to %s)",
                ccy, len(daily),
                min(daily) if daily else "—",
                max(daily) if daily else "—")


# Event-emit chunk size — the gmr-events EventLog wraps every batch
# in one Postgres transaction, and tens-of-thousands of inserts in
# a single tx is a fast way to time out the writer's lock budget.
EMIT_CHUNK = 2000


def emit_currency_events(
    log: EventLog | None, ccy: str, source: str, daily: dict[str, str],
) -> int:
    """Emit one UpsertExchangeRate per (date, rate). No-op when
    ``log`` is None — we always write the JSON file (CurrencyService
    is the existing reader); the event log is the new canonical
    write path the sinks project."""
    if log is None or not daily:
        return 0
    items = sorted(daily.items())
    emitted = 0
    for i in range(0, len(items), EMIT_CHUNK):
        chunk = items[i:i + EMIT_CHUNK]
        batch_id = uuid.uuid4()
        with log.batch(batch_id, producer="load_exchange_rates") as emit:
            for d, rate_str in chunk:
                try:
                    rate = float(rate_str)
                except (TypeError, ValueError):
                    continue
                emit.upsert(
                    "UpsertExchangeRate",
                    iri=(
                        f"http://data.fontem.eu/id/ExchangeRate/"
                        f"EUR-{ccy}-{d}"
                    ),
                    domain="exchange_rate",
                    payload=builders.upsert_exchange_rate(
                        base="EUR", target=ccy, date=d, rate=rate,
                        source=source,
                    ),
                )
                emitted += 1
    return emitted


def load_all(
    rates_dir: str | Path,
    start: str = "2000-01-01",
    end: str | None = None,
    currencies: list[str] | None = None,
    log: EventLog | None = None,
) -> dict:
    """Load all currencies, write per-currency JSON files and
    optionally emit UpsertExchangeRate events. Returns a counts
    summary."""
    rates_dir = Path(rates_dir)
    if end is None:
        end = date.today().isoformat()

    if currencies is None:
        ecb_list = ECB_CURRENCIES
        non_ecb_list = NON_ECB_CURRENCIES
    else:
        # Custom list — try ECB first, then frankfurter for any failures
        ecb_list = [c for c in currencies if c in ECB_CURRENCIES]
        non_ecb_list = [c for c in currencies if c not in ECB_CURRENCIES]

    logger.info("Loading %d ECB + %d non-ECB currencies (%s to %s)",
                len(ecb_list), len(non_ecb_list), start, end)

    summary = {"ccy_loaded": 0, "events_emitted": 0}

    # ECB primary source
    for ccy in ecb_list:
        logger.info("Fetching %s from ECB...", ccy)
        daily = fetch_ecb(ccy, start, end)
        source = "ecb"
        if not daily:
            logger.warning("  %s: ECB returned no data, trying Frankfurter", ccy)
            daily = fetch_frankfurter(ccy, start, end)
            source = "frankfurter"
        if daily:
            save_currency_file(rates_dir, ccy, daily)
            summary["ccy_loaded"] += 1
            summary["events_emitted"] += emit_currency_events(
                log, ccy, source, daily,
            )

    # Non-ECB fallback source
    for ccy in non_ecb_list:
        logger.info("Fetching %s from Frankfurter...", ccy)
        daily = fetch_frankfurter(ccy, start, end)
        if daily:
            save_currency_file(rates_dir, ccy, daily)
            summary["ccy_loaded"] += 1
            summary["events_emitted"] += emit_currency_events(
                log, ccy, "frankfurter", daily,
            )

    # Write metadata
    metadata = {
        "last_refreshed": date.today().isoformat(),
        "start_period": start,
        "end_period": end,
        "ecb_currencies": ecb_list,
        "non_ecb_currencies": non_ecb_list,
        "sources": ["ecb", "frankfurter"],
    }
    with open(rates_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    return summary


def main(argv=None):
    """CLI entry point — fetch all currency rates and write to disk."""
    parser = argparse.ArgumentParser(description="Fetch all exchange rates")
    parser.add_argument(
        "--rates-dir",
        default=os.environ.get("CURRENCY_DATA_DIR", "/srv/nfs/currency-data"),
    )
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--currencies", nargs="+", default=None,
                        help="Specific currencies to fetch (default: all)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log: EventLog | None = None
    try:
        log = EventLog.from_env()
    except Exception:  # pylint: disable=broad-exception-caught
        # No EVENTS_DATABASE_URL configured (CLI runs / tests) —
        # fall through to JSON-only mode without crashing.
        logger.warning(
            "EVENTS_DATABASE_URL unset; running in JSON-only mode "
            "(no event-log emit)",
        )
    try:
        summary = load_all(
            args.rates_dir, args.start, args.end, args.currencies,
            log=log,
        )
    finally:
        if log is not None:
            log.close()
    logger.info(
        "Done: %d currencies loaded, %d events emitted",
        summary["ccy_loaded"], summary["events_emitted"],
    )


if __name__ == "__main__":
    main()
