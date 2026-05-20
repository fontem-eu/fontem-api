"""Tests for load_exchange_rates event-log emission."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.etl.load_exchange_rates import emit_currency_events


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emit_skips_when_log_is_none():
    """JSON-only mode (no EVENTS_DATABASE_URL) must not crash."""
    daily = {"2025-01-01": "1.0", "2025-01-02": "1.1"}
    assert emit_currency_events(None, "USD", "ecb", daily) == 0


def test_emit_skips_when_daily_empty():
    log, _emit = _mock_log()
    assert emit_currency_events(log, "USD", "ecb", {}) == 0
    log.batch.assert_not_called()


def test_emit_drops_unparseable_rates():
    log, _emit = _mock_log()
    daily = {"2025-01-01": "1.05", "2025-01-02": "not-a-number"}
    n = emit_currency_events(log, "USD", "ecb", daily)
    assert n == 1


def test_emit_payload_shape():
    log, emit = _mock_log()
    daily = {"2025-01-01": "1.0473"}
    n = emit_currency_events(log, "USD", "ecb", daily)
    assert n == 1
    call = emit.upsert.call_args
    assert call.args[0] == "UpsertExchangeRate"
    payload = call.kwargs["payload"]
    assert payload["base"] == "EUR"
    assert payload["target"] == "USD"
    assert payload["date"] == "2025-01-01"
    assert payload["rate"] == 1.0473
    assert payload["source"] == "ecb"
