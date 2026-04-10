"""
Currency Service
================
A single source of truth for all currency operations in GMR:
- Historical daily exchange rates (loaded from per-currency JSON files)
- Country -> currency history (Estonia: EEK until 2010, EUR after)
- Locked/peg rates for currencies that became EUR
- ISO alias resolution (USN -> USD, etc.)
- Sentinel detection (-1, 0.01 -> "value undisclosed")
- Decimal-precision arithmetic

Usage:
    svc = CurrencyService.load(rates_dir="/srv/nfs/currency-data")
    eur = svc.to_eur(Decimal("1234567.89"), "PLN", date(2023, 7, 31))
    ccy = svc.currency_for("EST", date(2010, 5, 1))    # -> "EEK"
    ccy = svc.resolve_currency("USN", "POL", date(2025, 11, 24))  # -> "USD"
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Banker's rounding, 28 digits of precision (more than enough for monetary values)
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_EVEN

# Sentinel values TED uses for "value not disclosed"
SENTINEL_VALUES = {Decimal("-1"), Decimal("-1.0"), Decimal("0.01")}

# Maximum days to walk back for a missing rate (weekend/holiday tolerance)
RATE_LOOKBACK_DAYS = 7


@dataclass
class ConversionResult:
    """Result of a value conversion."""
    eur: Decimal | None
    rate_used: Decimal | None
    rate_date: date | None
    source: str  # 'ecb', 'locked', 'inferred', 'unknown'


class CurrencyService:
    """Single source of truth for currency operations."""

    def __init__(
        self,
        rates: dict[str, dict[str, Decimal]],
        locked: dict[str, dict],
        country_history: dict[str, list[dict]],
        aliases: dict[str, str],
    ) -> None:
        self._rates = rates
        self._locked = locked
        self._country_history = country_history
        self._aliases = aliases

    @classmethod
    def load(cls, rates_dir: str | Path) -> "CurrencyService":
        """Load all currency data from disk."""
        rates_dir = Path(rates_dir)
        data_dir = Path(__file__).parent / "data"

        # Daily rates (per-currency JSON files in rates_dir/rates/)
        rates: dict[str, dict[str, Decimal]] = {}
        rates_subdir = rates_dir / "rates"
        if rates_subdir.exists():
            for f in rates_subdir.glob("*.json"):
                if f.parent.name == "legacy":
                    continue
                ccy = f.stem.upper()
                with open(f) as fh:
                    raw = json.load(fh)
                rates[ccy] = {k: Decimal(str(v)) for k, v in raw.items()}
                logger.debug("Loaded %d %s rates", len(rates[ccy]), ccy)

        # Static reference data shipped with the package
        with open(data_dir / "locked_rates.json") as fh:
            locked_raw = json.load(fh)
        locked = {
            k: {**v, "locked_rate": Decimal(v["locked_rate"])}
            for k, v in locked_raw.items()
            if not k.startswith("_")
        }

        with open(data_dir / "country_currencies.json") as fh:
            country_raw = json.load(fh)
        country_history = {k: v for k, v in country_raw.items() if not k.startswith("_")}

        with open(data_dir / "aliases.json") as fh:
            aliases = json.load(fh)
        aliases = {k: v for k, v in aliases.items() if not k.startswith("_")}

        logger.info(
            "CurrencyService loaded: %d daily rate currencies, %d locked, "
            "%d countries, %d aliases",
            len(rates), len(locked), len(country_history), len(aliases),
        )
        return cls(rates, locked, country_history, aliases)

    # ── Sentinel detection ────────────────────────────────────────

    @staticmethod
    def parse_value(raw) -> tuple[Decimal | None, bool]:
        """Parse a raw value string/number into a Decimal.

        Returns (value, was_sentinel). When was_sentinel=True, the value
        is None and the caller should mark the contract as undisclosed.
        """
        if raw is None:
            return None, False
        try:
            v = Decimal(str(raw))
        except (ValueError, ArithmeticError):
            return None, False
        if v in SENTINEL_VALUES:
            return None, True
        return v, False

    # ── Alias resolution ──────────────────────────────────────────

    def normalize_currency(self, ccy: str | None) -> str | None:
        """Normalize a currency code: uppercase, resolve aliases."""
        if not ccy:
            return None
        ccy = ccy.strip().upper()
        return self._aliases.get(ccy, ccy)

    # ── Country → currency lookup ─────────────────────────────────

    def currency_for(self, country: str, on: date) -> str | None:
        """Return the official currency of a country on a given date.

        country: ISO 3166-1 alpha-3 code (DEU, FRA, EST, etc.)
        on: the date to check
        Returns the ISO 4217 currency code or None if unknown.
        """
        if not country:
            return None
        country = country.upper()
        history = self._country_history.get(country)
        if not history:
            return None
        for entry in history:
            start = date.fromisoformat(entry["start"])
            end_str = entry.get("end")
            end = date.fromisoformat(end_str) if end_str else date(9999, 12, 31)
            if start <= on <= end:
                return entry["currency"]
        return None

    def resolve_currency(
        self,
        declared: str | None,
        country: str | None = None,
        on: date | None = None,
    ) -> tuple[str | None, bool]:
        """Resolve the effective currency for a contract.

        Returns (currency, inferred). When inferred=True, the currency
        was filled from the country default (declared was None).
        """
        normalized = self.normalize_currency(declared)
        if normalized:
            return normalized, False
        if country and on:
            inferred = self.currency_for(country, on)
            if inferred:
                return inferred, True
        return None, False

    # ── Rate lookup ───────────────────────────────────────────────

    def _rate_on(self, currency: str, on: date) -> tuple[Decimal | None, date | None, str]:
        """Return (rate, actual_date_used, source) for a currency on a date.

        Walks back up to RATE_LOOKBACK_DAYS days for weekend/holiday
        tolerance. Sources: 'ecb' (daily file), 'locked' (post-EUR fixed
        rate), or 'unknown'.
        """
        currency = currency.upper()

        # Check locked rates first — these override daily rates after the lock date
        locked = self._locked.get(currency)
        if locked:
            locked_since = date.fromisoformat(locked["locked_since"])
            if on >= locked_since:
                return locked["locked_rate"], on, "locked"

        # Daily rates
        daily = self._rates.get(currency)
        if daily:
            for i in range(RATE_LOOKBACK_DAYS + 1):
                key = (on - timedelta(days=i)).isoformat()
                rate = daily.get(key)
                if rate is not None and rate > 0:
                    return rate, date.fromisoformat(key), "ecb"

        # Pre-locked rate fallback (DEM in 1995): use the locked rate even before
        # the lock date if we have no daily rate. This is approximate but better
        # than nothing.
        if locked:
            return locked["locked_rate"], on, "locked-approx"

        return None, None, "unknown"

    # ── Conversion ────────────────────────────────────────────────

    def to_eur(
        self,
        value: Decimal | float | int | str | None,
        currency: str | None,
        on: date | None,
    ) -> Decimal | None:
        """Convert a value to EUR. Returns None if not convertible."""
        result = self.convert_detailed(value, currency, on)
        return result.eur

    def convert_detailed(
        self,
        value: Decimal | float | int | str | None,
        currency: str | None,
        on: date | None,
    ) -> ConversionResult:
        """Convert with full provenance info."""
        if value is None:
            return ConversionResult(None, None, None, "unknown")
        if not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except (ValueError, ArithmeticError):
                return ConversionResult(None, None, None, "unknown")

        ccy = self.normalize_currency(currency)
        if ccy is None:
            return ConversionResult(None, None, None, "unknown")

        if ccy == "EUR":
            return ConversionResult(
                value.quantize(Decimal("0.01")), Decimal("1"), on, "identity",
            )

        if on is None:
            return ConversionResult(None, None, None, "unknown")

        rate, rate_date, source = self._rate_on(ccy, on)
        if rate is None:
            return ConversionResult(None, None, None, "unknown")

        eur = (value / rate).quantize(Decimal("0.01"))
        return ConversionResult(eur, rate, rate_date, source)

    # ── Stats / introspection ─────────────────────────────────────

    def known_currencies(self) -> list[str]:
        """Return all currencies the service can handle."""
        return sorted(set(self._rates.keys()) | set(self._locked.keys()) | {"EUR"})

    def currency_coverage(self, currency: str) -> tuple[date | None, date | None]:
        """Return (earliest_date, latest_date) for a currency's daily rates."""
        daily = self._rates.get(currency.upper())
        if not daily:
            return None, None
        keys = sorted(daily.keys())
        return date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])
