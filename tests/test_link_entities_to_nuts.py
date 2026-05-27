"""Tests for the entity→NUTS linker."""
from unittest.mock import MagicMock

from src.etl.link_entities_to_nuts import (
    ENTITY_LABELS,
    _MERGE_COUNTRY_EDGES,
    _MERGE_POSTCODE_EDGES,
    _resolve_postcode_rows,
    link_label_country,
    link_label_postcode,
    run,
)


# ── Mock infrastructure ───────────────────────────────────────────────


class _CountTracker:
    """Driver for ``session.run`` mocks that simulates LOCATED_IN edge
    counts growing in lockstep with link / postcode-merge queries.

    Each label sees two passes (postcode + country); each pass diffs
    ``count(r)`` before/after to compute edges created. ``edges_per_pass``
    is a flat list ``[label1_postcode_Δ, label2_postcode_Δ, ...,
    label1_country_Δ, label2_country_Δ, ...]`` in run-order.
    """

    def __init__(self, edges_per_pass, fetch_pages=None):
        self._edges = list(edges_per_pass)
        self._fetch_pages = list(fetch_pages or [])
        self._total = 0
        self._next_pass_delta = None

    def run(self, query, *_args, **_kwargs):
        result = MagicMock()
        if query.lstrip().startswith("MATCH (:"):
            # _COUNT_LABEL_TEMPLATE — before/after edge count.
            if self._next_pass_delta is None:
                # BEFORE — snapshot total, queue the pass's delta.
                self._next_pass_delta = self._edges.pop(0) if self._edges else 0
                result.single.return_value = {"n": self._total}
            else:
                # AFTER — total goes up by the queued delta.
                self._total += self._next_pass_delta
                self._next_pass_delta = None
                result.single.return_value = {"n": self._total}
        elif "RETURN elementId(e)" in query:
            # _FETCH_POSTCODE_CANDIDATES — return next prepared page,
            # then an empty iterator on subsequent calls.
            page = self._fetch_pages.pop(0) if self._fetch_pages else []
            result.__iter__ = lambda self_, _page=page: iter(_page)
        else:
            # Link queries (.consume() called) — no return value needed.
            pass
        return result


def _mock_driver(edges_per_pass, fetch_pages=None):
    driver = MagicMock()
    tracker = _CountTracker(edges_per_pass, fetch_pages)
    session = MagicMock()
    session.run.side_effect = tracker.run
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session, tracker


# ── Constants + Cypher shape ──────────────────────────────────────────


def test_entity_labels_covers_company_authority_lobbyist():
    """The linker must cover the three non-CohesionProject entity types."""
    assert set(ENTITY_LABELS) == {"Company", "Authority", "Lobbyist"}


def test_country_merge_joins_by_country_alpha3():
    """Country-pass UNWIND writes match NUTSRegion {level:0, country_alpha3}.
    Without this filter the join would either miss every row (wrong field
    name) or fan out to all 1797 regions per entity."""
    assert "UNWIND $rows AS row" in _MERGE_COUNTRY_EDGES
    assert "country_alpha3: row.a3" in _MERGE_COUNTRY_EDGES
    assert "level: 0" in _MERGE_COUNTRY_EDGES
    assert "MERGE (e)-[:LOCATED_IN]->(n)" in _MERGE_COUNTRY_EDGES


def test_country_merge_uses_unwind_not_call_in_transactions():
    """The previous shape was a single CALL { ... } IN TRANSACTIONS OF
    10000 ROWS. With ~38 NUTS-0 nodes and ~3M Company candidates the
    parallel sub-transactions deadlocked on the same target nodes.
    The new shape is Python-driven sequential UNWIND batches; this
    test pins that the old in-Cypher concurrency primitive is gone.
    """
    assert "IN TRANSACTIONS" not in _MERGE_COUNTRY_EDGES
    assert "CALL (" not in _MERGE_COUNTRY_EDGES


def test_postcode_merge_writes_via_unwind():
    """Postcode-pass writes use UNWIND on pre-resolved (eid, nuts3) pairs."""
    assert "UNWIND $rows AS row" in _MERGE_POSTCODE_EDGES
    assert "code: row.nuts3" in _MERGE_POSTCODE_EDGES
    assert "MERGE (e)-[:LOCATED_IN]->(n)" in _MERGE_POSTCODE_EDGES


# ── Country pass (Python-driven UNWIND batches) ───────────────────────


def test_link_label_country_returns_edge_delta():
    """link_label_country streams candidates + UNWIND-merges, then
    returns the after-before count diff."""
    fetch_page = [
        {"eid": "e1", "a3": "PRT"},
        {"eid": "e2", "a3": "DEU"},
    ]
    driver, session, _ = _mock_driver(
        edges_per_pass=[2], fetch_pages=[fetch_page],
    )
    delta = link_label_country(driver, "Company")
    assert delta == 2
    merge_calls = [c for c in session.run.call_args_list
                   if "UNWIND" in c.args[0]]
    assert len(merge_calls) == 1
    assert merge_calls[0].kwargs["rows"] == [
        {"eid": "e1", "a3": "PRT"},
        {"eid": "e2", "a3": "DEU"},
    ]


def test_link_label_country_uses_separate_read_and_write_sessions():
    """Same separation as link_label_postcode — concurrent read cursor +
    write txn on one session forces the driver to materialise the read,
    risking OOM. Two sessions keep the cursor genuinely streaming.
    Critical for the country pass too because it pulls ALL candidates
    (no postcode filter), so it can be 3M+ rows on Company."""
    driver, _session, _ = _mock_driver(
        edges_per_pass=[1],
        fetch_pages=[[{"eid": "e1", "a3": "PRT"}]],
    )
    link_label_country(driver, "Company")
    # count-before, read+write block, count-after → at least 3 sessions.
    assert driver.session.call_count >= 3


# ── Postcode-link pass ────────────────────────────────────────────────


def test_resolve_postcode_rows_joins_country_and_normalised_pc():
    """alpha-3 → alpha-2 + normalise(pc) lookup; only matched rows survive."""
    pcode = {
        ("NL", "3204XD"): "NL366",
        ("DE", "10115"): "DE300",
    }
    candidates = [
        {"eid": "n1", "a3": "NLD", "pc": "3204 XD"},
        {"eid": "n2", "a3": "DEU", "pc": "10115"},
        {"eid": "n3", "a3": "DEU", "pc": "unknown"},
        {"eid": "n4", "a3": "ZZZ", "pc": "3204 XD"},
    ]
    out = _resolve_postcode_rows(candidates, pcode)
    assert {row["eid"]: row["nuts3"] for row in out} == {
        "n1": "NL366",
        "n2": "DE300",
    }


def test_resolve_handles_missing_or_empty_postcode():
    """Empty / falsy postcode is skipped without raising."""
    out = _resolve_postcode_rows(
        [{"eid": "x", "a3": "NLD", "pc": ""}],
        {("NL", ""): "NL366"},  # would match but normalise("") == ""
    )
    # We don't gate on empty-postcode normalise output — both sides
    # normalise to "", so the join can succeed. The point of this test
    # is that the join doesn't crash on the empty input.
    assert isinstance(out, list)


def test_link_label_postcode_returns_edge_delta():
    """link_label_postcode merges resolved rows and returns count delta."""
    pcode = {("NL", "3204XD"): "NL366"}
    fetch_page = [{"eid": "n1", "a3": "NLD", "pc": "3204 XD"}]
    driver, session, _ = _mock_driver(
        edges_per_pass=[1], fetch_pages=[fetch_page],
    )
    delta = link_label_postcode(driver, "Company", pcode)
    assert delta == 1
    # The MERGE UNWIND must have been issued once with the resolved row.
    merge_calls = [c for c in session.run.call_args_list
                   if "UNWIND" in c.args[0]]
    assert len(merge_calls) == 1
    assert merge_calls[0].kwargs["rows"] == [{"eid": "n1", "nuts3": "NL366"}]


def test_link_label_postcode_skips_merge_when_no_resolved_rows():
    """If no candidate resolves, no UNWIND write is sent."""
    driver, session, _ = _mock_driver(
        edges_per_pass=[0],
        fetch_pages=[[{"eid": "x", "a3": "DEU", "pc": "unknown"}]],
    )
    delta = link_label_postcode(driver, "Company", {})
    assert delta == 0
    merge_calls = [c for c in session.run.call_args_list
                   if "UNWIND" in c.args[0]]
    assert merge_calls == []


def test_link_label_postcode_uses_separate_read_and_write_sessions():
    """Driver.session() must be called multiple times so the read cursor
    streams instead of materialising when writes interleave (avoids
    the 3M-row OOMKilled the single-session shape would cause).
    """
    pcode = {("NL", "3204XD"): "NL366"}
    page = [{"eid": "n1", "a3": "NLD", "pc": "3204 XD"}]
    driver, _session, _ = _mock_driver(
        edges_per_pass=[1], fetch_pages=[page],
    )
    link_label_postcode(driver, "Company", pcode)
    # count-before, read+write block, count-after → at least 3 sessions.
    assert driver.session.call_count >= 3


# ── run() two-pass orchestration ──────────────────────────────────────


def test_run_does_postcode_then_country_pass_per_label():
    """run() iterates the postcode pass for all labels, then country."""
    driver, _session, _ = _mock_driver(
        # 3 postcode passes (Company / Authority / Lobbyist) + 3 country.
        edges_per_pass=[7, 2, 1, 100, 50, 5],
    )
    summary = run(driver, pcode_lookup={})
    assert summary["postcode_counts"] == {
        "Company": 7, "Authority": 2, "Lobbyist": 1,
    }
    assert summary["country_counts"] == {
        "Company": 100, "Authority": 50, "Lobbyist": 5,
    }


def test_run_reports_elapsed_time():
    """Summary includes a non-negative elapsed_s field."""
    driver, _session, _ = _mock_driver(edges_per_pass=[0] * 6)
    summary = run(driver, pcode_lookup={})
    assert "elapsed_s" in summary
    assert summary["elapsed_s"] >= 0


def test_run_handles_zero_matches_gracefully():
    """If nothing gets linked, both summaries report zeros (not an error)."""
    driver, _session, _ = _mock_driver(edges_per_pass=[0] * 6)
    summary = run(driver, pcode_lookup={})
    assert summary["postcode_counts"] == {
        "Company": 0, "Authority": 0, "Lobbyist": 0,
    }
    assert summary["country_counts"] == {
        "Company": 0, "Authority": 0, "Lobbyist": 0,
    }


def test_run_loads_real_lookup_when_none_passed(monkeypatch):
    """Calling run() without pcode_lookup falls back to load_lookup()."""
    seen = {}

    def _fake_loader():
        seen["loaded"] = True
        return {}

    monkeypatch.setattr(
        "src.etl.link_entities_to_nuts.load_lookup", _fake_loader,
    )
    driver, _session, _ = _mock_driver(edges_per_pass=[0] * 6)
    run(driver)
    assert seen.get("loaded") is True
