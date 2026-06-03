"""Unit tests for the CurrencyClient HTTP wrapper.

The client is a thin facade over httpx that the ETL pods use instead
of mounting the rates PVC. Each method translates a payload into a
POST against the currency-service and degrades to the same
"unknown" semantics the old in-process version returned on bad input.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx

from src.services.currency.client import CurrencyClient


def _stub_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=payload,
        request=httpx.Request("POST", "http://test/"),
    )


# ── parse_value ────────────────────────────────────────────────


def test_parse_value_returns_decimal_and_sentinel_flag():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        return_value=_stub_response({"value": "1234.5", "was_sentinel": True}),
    ):
        value, was_sentinel = client.parse_value("1234,5 EUR")
    assert value == Decimal("1234.5")
    assert was_sentinel is True


def test_parse_value_handles_null_value_payload():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        return_value=_stub_response({"value": None, "was_sentinel": False}),
    ):
        value, was_sentinel = client.parse_value("garbage")
    assert value is None
    assert was_sentinel is False


def test_parse_value_degrades_gracefully_on_http_error():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        side_effect=httpx.ConnectError("nope"),
    ):
        value, was_sentinel = client.parse_value("anything")
    assert value is None
    assert was_sentinel is False


def test_parse_value_degrades_gracefully_on_value_error():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        side_effect=ValueError("bad json"),
    ):
        value, was_sentinel = client.parse_value("anything")
    assert (value, was_sentinel) == (None, False)


# ── resolve_currency ───────────────────────────────────────────


def test_resolve_currency_returns_pair():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        return_value=_stub_response({"currency": "EUR", "inferred": True}),
    ):
        ccy, inferred = client.resolve_currency(
            "EUR", country="DE", on=date(2026, 1, 1),
        )
    assert ccy == "EUR"
    assert inferred is True


def test_resolve_currency_returns_none_pair_on_network_error():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        side_effect=httpx.ReadTimeout("slow"),
    ):
        ccy, inferred = client.resolve_currency("EUR")
    assert ccy is None
    assert inferred is False


# ── convert_detailed + to_eur ──────────────────────────────────


def test_convert_detailed_returns_conversion_result():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        return_value=_stub_response({
            "eur": "1100.00",
            "rate_used": "1.10",
            "rate_date": "2026-01-01",
            "source": "ecb",
        }),
    ):
        result = client.convert_detailed(
            "1000", "USD", on=date(2026, 1, 1),
        )
    assert result.eur == Decimal("1100.00")
    assert result.rate_used == Decimal("1.10")
    assert result.rate_date == date(2026, 1, 1)
    assert result.source == "ecb"


def test_convert_detailed_degrades_to_unknown_on_http_error():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        side_effect=httpx.HTTPError("dead"),
    ):
        result = client.convert_detailed("1000", "USD", on=date(2026, 1, 1))
    assert result.eur is None
    assert result.source == "unknown"


def test_to_eur_returns_just_the_amount():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        return_value=_stub_response({
            "eur": "950.00", "rate_used": "0.95",
            "rate_date": "2026-01-01", "source": "ecb",
        }),
    ):
        eur = client.to_eur("1000", "USD", on=date(2026, 1, 1))
    assert eur == Decimal("950.00")


def test_to_eur_passes_none_through_for_missing_inputs():
    client = CurrencyClient(base_url="http://test")
    with patch.object(
        client._client, "post",  # pylint: disable=protected-access
        return_value=_stub_response({
            "eur": None, "rate_used": None,
            "rate_date": None, "source": "unknown",
        }),
    ):
        assert client.to_eur(None, "USD", on=date(2026, 1, 1)) is None


# ── Client lifecycle ──────────────────────────────────────────


def test_context_manager_closes_underlying_client():
    with CurrencyClient(base_url="http://test") as client:
        underlying = client._client  # pylint: disable=protected-access
    # After exiting, the httpx client should be closed.
    assert underlying.is_closed


def test_default_timeout_picks_up_env_var(monkeypatch):
    monkeypatch.setenv("CURRENCY_SERVICE_TIMEOUT_S", "5.0")
    # Re-import via the module so the new env var is read.
    from importlib import reload  # pylint: disable=import-outside-toplevel
    import src.services.currency.client as mod  # pylint: disable=import-outside-toplevel

    reload(mod)
    client = mod.CurrencyClient(base_url="http://test")
    assert client._timeout == 5.0  # pylint: disable=protected-access


def test_base_url_strips_trailing_slash():
    client = CurrencyClient(base_url="http://test/")
    assert client._base == "http://test"  # pylint: disable=protected-access


def test_constructor_passes_through_explicit_timeout():
    client = CurrencyClient(base_url="http://test", timeout_s=2.5)
    assert client._timeout == 2.5  # pylint: disable=protected-access


# Trivially exercise close() outside the context manager too.
def test_close_can_be_called_directly():
    client = CurrencyClient(base_url="http://test")
    client.close()
    assert client._client.is_closed  # pylint: disable=protected-access


# Smoke test on the importable surface (the package's __init__).
def test_conversion_result_importable_from_package_root():
    from src.services.currency import ConversionResult  # pylint: disable=import-outside-toplevel
    assert ConversionResult is not None
