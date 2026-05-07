"""Tests for the CPV reference-data loader (post-event-log)."""
from unittest.mock import MagicMock

from src.etl.load_cpv import CPV_DIVISIONS, CPV_DETAILED, load_cpv_divisions


def _mock_log():
    log = MagicMock()
    emit = MagicMock()
    log.batch.return_value.__enter__ = MagicMock(return_value=emit)
    log.batch.return_value.__exit__ = MagicMock(return_value=False)
    return log, emit


def test_emits_one_event_per_code():
    log, emit = _mock_log()
    count = load_cpv_divisions(log)
    assert count == len(CPV_DIVISIONS) + len(CPV_DETAILED)
    assert emit.upsert.call_count == count
    types = {c.args[0] for c in emit.upsert.call_args_list}
    assert types == {"UpsertTaxonomyCode"}


def test_division_payload_is_8_digit_with_zero_level():
    """Top-level divisions are emitted as 8-digit codes (e.g. 45000000)
    so they share the same key namespace as detailed codes."""
    log, emit = _mock_log()
    load_cpv_divisions(log)
    payloads = [c.kwargs["payload"] for c in emit.upsert.call_args_list]
    division_45 = next(p for p in payloads if p["code"] == "45000000")
    assert division_45["system"] == "cpv"
    assert division_45["level"] == 0
    assert "parent_code" not in division_45
    assert "Construction" in division_45["label"]


def test_detailed_payload_carries_parent_code():
    log, emit = _mock_log()
    load_cpv_divisions(log)
    payloads = [c.kwargs["payload"] for c in emit.upsert.call_args_list]
    # 45233000 (highway works) parents 45000000 (construction division)
    detailed = next(p for p in payloads if p["code"] == "45233000")
    assert detailed["parent_code"] == "45000000"
    assert detailed["level"] == 1


def test_cpv_divisions_have_standard_codes():
    """CPV division codes are 2-digit strings."""
    for code in CPV_DIVISIONS:
        assert len(code) == 2
        assert code.isdigit()
