"""Rebuilding the company subjects an AssertSameAs wiped.

The repair re-emits UpsertCompany, whose render is a whole-subject
replace. That is what makes it a repair and also what makes it
dangerous: aimed at the wrong subject, or carrying the wrong field, it
destroys rather than restores. Most of what follows pins the aiming.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.etl import rebuild_stripped_companies as rsc


class _Virtuoso:
    """Honours keyset paging and enforces the two real server limits:
    ResultSetMaxRows truncates SILENTLY, and MaxSortedTopRows makes deep
    OFFSET paging a hard SR353 -- which is why this pages by key."""

    def __init__(self, subjects, cap=None):
        self._subjects = sorted(subjects)
        self._cap = cap if cap is not None else rsc._RESULT_SET_MAX_ROWS
        self.queries = []

    def query(self, q):
        self.queries.append(q)
        limit = int(q.split("LIMIT")[1].split()[0])
        rows = self._subjects
        if 'STR(?s) > "' in q:
            after = q.split('STR(?s) > "')[1].split('"')[0]
            rows = [r for r in rows if r > after]
        rows = rows[:limit]
        return [{"s": r} for r in rows[:self._cap]]


def _neo4j(records):
    client = MagicMock()
    session = client.session.return_value
    session.__enter__ = lambda s: s
    session.__exit__ = lambda s, *a: None
    session.run.return_value.data.return_value = records
    return client


def test_entity_kind_is_never_sent():
    """Two hazards ride on it. An UpsertCompany carrying entity_kind
    makes the sink issue an extra UNFILTERED delete of the sibling
    InvestmentFund subject -- unfiltered meaning it does not exempt
    owl:sameAs, so the repair would destroy the very assertion it is
    meant to preserve."""
    assert "entity_kind" not in rsc._FIELDS
    payloads, _, _ = rsc.load_from_neo4j(_neo4j([
        {"gmr_id": "a", "labels": ["Company"], "name": "Acme",
         "country": "FRA", "entity_kind": None, "lei": None, "vat": None,
         "cik": None, "active": True, "legal_form": None,
         "postal_code": None},
    ]), ["a"])
    assert payloads and "entity_kind" not in payloads[0]


def test_funds_are_skipped_not_repaired():
    """When entity_kind is FUND the renderer writes at the
    InvestmentFund subject, so the stripped Company subject would be
    left exactly as stripped -- a silent no-op reported as a repair.
    Prod holds 245,585 funds."""
    payloads, missing, funds = rsc.load_from_neo4j(_neo4j([
        {"gmr_id": "f1", "labels": ["InvestmentFund"], "name": "A Fund",
         "country": "LUX", "entity_kind": "FUND", "lei": None, "vat": None,
         "cik": None, "active": True, "legal_form": None,
         "postal_code": None},
        {"gmr_id": "f2", "labels": ["Company"], "name": "Also A Fund",
         "country": "LUX", "entity_kind": "FUND", "lei": None, "vat": None,
         "cik": None, "active": True, "legal_form": None,
         "postal_code": None},
    ]), ["f1", "f2"])
    assert not payloads
    assert sorted(funds) == ["f1", "f2"]
    assert not missing


def test_a_node_with_no_name_is_not_a_repair():
    """Rebuilding from a nameless node produces a subject with no label
    -- thinner than nothing useful, and it would read as repaired."""
    payloads, missing, _ = rsc.load_from_neo4j(_neo4j([
        {"gmr_id": "n1", "labels": ["Company"], "name": None,
         "country": "FRA", "entity_kind": None, "lei": None, "vat": None,
         "cik": None, "active": True, "legal_form": None,
         "postal_code": None},
    ]), ["n1"])
    assert not payloads
    assert missing == ["n1"]


def test_ids_absent_from_neo4j_are_reported():
    """The dedup losers. They have no source to rebuild from, and must
    be named rather than silently dropped from the count."""
    payloads, missing, _ = rsc.load_from_neo4j(_neo4j([]), ["gone-1", "gone-2"])
    assert not payloads
    assert sorted(missing) == ["gone-1", "gone-2"]


def test_selection_requires_a_low_predicate_count_not_just_sameas():
    """Every sameAs subject in graph/company is damaged today, but that
    is a fact about the incident, not an invariant. A healthy company
    that legitimately gains an owl:sameAs later must not be rewritten by
    a re-run of this script."""
    v = _Virtuoso([f"{rsc._COMPANY_PREFIX}a"])
    rsc.find_stripped(v, page=10)
    q = v.queries[0]
    assert f"COUNT(DISTINCT ?p) <= {rsc._MAX_PREDICATES}" in q
    assert "owl#sameAs" in q


def test_keyset_paging_walks_past_one_page():
    subjects = [f"{rsc._COMPANY_PREFIX}{i:05d}" for i in range(250)]
    v = _Virtuoso(subjects)
    got = rsc.find_stripped(v, page=100)
    assert len(got) == 250
    assert len(v.queries) >= 3


def test_a_page_at_the_cap_is_refused():
    """ResultSetMaxRows truncates with no signal, and a truncated scan
    would silently leave subjects unrepaired while reporting success."""

    class _Truncating:
        def query(self, _q):
            return [{"s": f"{rsc._COMPANY_PREFIX}{i}"} for i in range(5)]

    try:
        rsc.find_stripped(_Truncating(), page=4, cap=5)
    except RuntimeError as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError at the result-set cap")


def test_page_size_at_or_above_the_cap_is_rejected():
    try:
        rsc.find_stripped(_Virtuoso([]), page=50_000, cap=50_000)
    except ValueError as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("expected a ValueError for an unsafe page size")


def test_the_event_targets_the_company_subject():
    """The IRI decides which subject the sink replaces. Aimed anywhere
    else this rewrites an innocent record."""
    log = MagicMock()
    emit = log.batch.return_value.__enter__.return_value
    sent = rsc.emit_rebuilds(log, [{"gmr_id": "abc", "name": "Acme",
                                    "country": "FRA"}])
    assert sent == 1
    kwargs = emit.upsert.call_args.kwargs
    assert kwargs["iri"] == f"{rsc._COMPANY_PREFIX}abc"
    assert kwargs["domain"] == "company"
    assert kwargs["payload"]["gmr_id"] == "abc"


def test_dry_run_emits_nothing(monkeypatch):
    virtuoso_cls = MagicMock()
    virtuoso_cls.from_env.return_value = _Virtuoso([f"{rsc._COMPANY_PREFIX}a"])
    monkeypatch.setattr(rsc, "VirtuosoClient", virtuoso_cls)
    monkeypatch.setattr(rsc, "Neo4jClient", lambda *a, **k: _neo4j([
        {"gmr_id": "a", "labels": ["Company"], "name": "Acme",
         "country": "FRA", "entity_kind": None, "lei": None, "vat": None,
         "cik": None, "active": True, "legal_form": None,
         "postal_code": None},
    ]))
    event_log_cls = MagicMock()
    monkeypatch.setattr(rsc, "EventLog", event_log_cls)
    rsc.main([])
    event_log_cls.from_env.assert_not_called()
