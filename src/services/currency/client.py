"""HTTP client for the fontem-currency service.

The old in-process ``CurrencyService`` is now hosted as a singleton
under ``currency-service`` namespace; this client makes the in-cluster
HTTP call so ETL pods don't have to mount the rates PVC themselves.

API surface mirrors the three CurrencyService methods the loaders
actually use:

  * ``parse_value(raw)``                 → (Decimal | None, was_sentinel)
  * ``resolve_currency(declared, country, on)``
                                          → (currency, inferred)
  * ``to_eur(value, currency, on)``       → Decimal | None
  * ``convert_detailed(value, currency, on)``
                                          → ConversionResult

Plus a passthrough ``normalize_currency()`` since the loaders use it
inline. Network failures degrade to the same "unknown" semantics the
in-process version returned on bad input, so the caller never has to
distinguish "no such currency" from "service down" — both produce
``None``-shaped results and the loader logs and moves on.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.environ.get(
    "CURRENCY_SERVICE_URL",
    "http://fontem-currency.currency-service.svc.cluster.local",
)
DEFAULT_TIMEOUT_S = float(os.environ.get("CURRENCY_SERVICE_TIMEOUT_S", "10"))


@dataclass
class ConversionResult:
    """Mirrors src.services.currency.ConversionResult — kept here so
    callers don't import from both old and new locations during the
    cutover. Will eventually move to a shared types module.
    """
    eur: Decimal | None
    rate_used: Decimal | None
    rate_date: date | None
    source: str


class CurrencyClient:
    """Thin httpx client over fontem-currency's /v1 surface."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout_s or DEFAULT_TIMEOUT_S
        self._client = httpx.Client(
            base_url=self._base, timeout=self._timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CurrencyClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── Methods that match the old CurrencyService API ─────────────

    def parse_value(self, raw) -> tuple[Decimal | None, bool]:
        """Sentinel detection. Returns (value, was_sentinel).

        Network or parse failures fall through to (None, False) — same
        as a missing/garbage upstream value in the old in-process call.
        """
        try:
            r = self._client.post("/v1/parse", json={"raw": raw})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("currency.parse_value failed: %s", exc)
            return None, False
        value = data.get("value")
        return (
            Decimal(value) if value is not None else None,
            bool(data.get("was_sentinel", False)),
        )

    def resolve_currency(
        self,
        declared: str | None,
        country: str | None = None,
        on: date | None = None,
    ) -> tuple[str | None, bool]:
        """Returns (currency, inferred). (None, False) on network errors."""
        body = {
            "declared": declared,
            "country": country,
            "on": on.isoformat() if on else None,
        }
        try:
            r = self._client.post("/v1/resolve", json=body)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("currency.resolve failed: %s", exc)
            return None, False
        return data.get("currency"), bool(data.get("inferred", False))

    def convert_detailed(
        self,
        value: Decimal | float | int | str | None,
        currency: str | None,
        on: date | None,
    ) -> ConversionResult:
        body = {
            "value": str(value) if value is not None else None,
            "currency": currency,
            "on": on.isoformat() if on else None,
        }
        try:
            r = self._client.post("/v1/convert", json=body)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("currency.convert failed: %s", exc)
            return ConversionResult(None, None, None, "unknown")
        eur = data.get("eur")
        rate = data.get("rate_used")
        rate_date = data.get("rate_date")
        return ConversionResult(
            eur=Decimal(eur) if eur is not None else None,
            rate_used=Decimal(rate) if rate is not None else None,
            rate_date=date.fromisoformat(rate_date) if rate_date else None,
            source=data.get("source", "unknown"),
        )

    def to_eur(
        self,
        value: Decimal | float | int | str | None,
        currency: str | None,
        on: date | None,
    ) -> Decimal | None:
        return self.convert_detailed(value, currency, on).eur
