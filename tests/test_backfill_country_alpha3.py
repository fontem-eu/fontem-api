"""Tests for the alpha-2 → alpha-3 country backfill script."""
from unittest.mock import MagicMock

from src.etl.backfill_country_alpha3 import (
    LABELS,
    _SET_ALPHA3,
    _backfill_label,
    run,
)


def _mock_session(pages_per_label):
    """A session whose run() returns the next prepared page for FETCH
    queries and a no-op result for SET queries.

    ``pages_per_label`` is a dict {label: [page1_rows, page2_rows, ...]}
    where each row is ``{"eid": ..., "a2": ...}``. After all pages are
    consumed for a label, subsequent fetches return empty iterators.
    """
    pages = {label: list(pages_per_label.get(label, [])) for label in LABELS}
    write_calls: list[list[dict]] = []
    session = MagicMock()

    def _run(query, **kwargs):
        result = MagicMock()
        if query.lstrip().startswith("MATCH (e:"):
            for label in LABELS:
                if f"(e:{label})" in query:
                    page = pages[label].pop(0) if pages[label] else []
                    result.__iter__ = lambda self_, _p=page: iter(_p)
                    return result
        elif "UNWIND" in query:
            write_calls.append(list(kwargs["rows"]))
        return result

    session.run.side_effect = _run
    return session, write_calls


# ── normalisation ────────────────────────────────────────────────────


def test_set_alpha3_query_writes_via_unwind():
    """Update statement uses elementId + UNWIND for batch updates."""
    assert "UNWIND $rows AS row" in _SET_ALPHA3
    assert "elementId(e) = row.eid" in _SET_ALPHA3
    assert "SET e.country = row.a3" in _SET_ALPHA3


def test_labels_cover_company_authority_lobbyist():
    assert set(LABELS) == {"Company", "Authority", "Lobbyist"}


# ── _backfill_label ──────────────────────────────────────────────────


def test_backfill_label_converts_alpha2_to_alpha3():
    page = [{"eid": "n1", "a2": "NL"}, {"eid": "n2", "a2": "DE"}]
    session, writes = _mock_session({"Company": [page]})
    summary = _backfill_label(session, "Company")
    assert summary == {"converted": 2, "unknown": 0}
    assert len(writes) == 1
    rows = writes[0]
    assert {r["eid"]: r["a3"] for r in rows} == {"n1": "NLD", "n2": "DEU"}


def test_backfill_label_skips_unknown_alpha2():
    """Rows whose alpha-2 isn't recognised are counted but not written."""
    page = [
        {"eid": "n1", "a2": "NL"},
        {"eid": "n2", "a2": "ZZ"},  # not a valid ISO alpha-2
    ]
    session, writes = _mock_session({"Company": [page]})
    summary = _backfill_label(session, "Company")
    assert summary == {"converted": 1, "unknown": 1}
    assert writes[0] == [{"eid": "n1", "a3": "NLD"}]


def test_backfill_label_handles_eu_specific_codes():
    """The EL/UK overrides (Greece + UK) work — they're not in pycountry."""
    page = [
        {"eid": "el1", "a2": "EL"},
        {"eid": "uk1", "a2": "UK"},
    ]
    session, writes = _mock_session({"Company": [page]})
    summary = _backfill_label(session, "Company")
    assert summary["converted"] == 2
    assert {r["eid"]: r["a3"] for r in writes[0]} == {
        "el1": "GRC", "uk1": "GBR",
    }


def test_backfill_label_returns_zero_on_empty_page():
    session, writes = _mock_session({"Company": []})
    summary = _backfill_label(session, "Company")
    assert summary == {"converted": 0, "unknown": 0}
    assert not writes


# ── run() orchestration ───────────────────────────────────────────────


def test_run_iterates_all_labels():
    """run() must process Company, Authority, Lobbyist in order."""
    session, _ = _mock_session({
        "Company":   [[{"eid": "c1", "a2": "FR"}]],
        "Authority": [[{"eid": "a1", "a2": "DE"}]],
        "Lobbyist":  [[{"eid": "l1", "a2": "IT"}]],
    })
    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    summary = run(driver)
    for label in LABELS:
        assert summary[label]["converted"] == 1
        assert summary[label]["unknown"] == 0
    assert summary["elapsed_s"] >= 0
