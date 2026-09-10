"""Purging the Notice subjects a mis-routed contract value rollup made.

Until fontem-virtuoso-sink#130 a value rollup was routed to
.../id/Notice/<ted_notice_id> while every full event rendered the same
notice at .../id/Contract/<ted_notice_id>. The rollup landed alone on a
subject carrying nothing but fontem:isCurrent (plus fontem:currentValue
when the collapse produced a figure), and the read path -- which looks
for isCurrent on the contract subject -- never saw it.

The dangerous mistake this script could make is purging a subject that
holds real notice triples, or one that is the last representation of a
notice. Most of what follows pins that it doesn't.
"""
# pylint: disable=protected-access
from unittest.mock import MagicMock

from src.etl import purge_orphan_rollup_subjects as porp

N = porp._NOTICE_PREFIX
C = porp._CONTRACT_PREFIX
G = "http://data.fontem.eu/graph/contract"
IS_CURRENT = f"{porp._FONTEM}isCurrent"
CURRENT_VALUE = f"{porp._FONTEM}currentValue"


class _Virtuoso:
    """Answers both query shapes the script asks, over a triple store.

    Enforces the two real Virtuoso limits, for the same reason the
    stranded-subject fake does: ResultSetMaxRows truncates SILENTLY
    (HTTP 200, fewer rows) and MaxSortedTopRows makes an ORDER BY past
    10,000 rows a hard SR353 rather than a truncation.
    """

    def __init__(self, triples, cap=None, sorted_top=10_000):
        self._triples = list(triples)          # (s, p) pairs
        self._cap = cap if cap is not None else porp._RESULT_SET_MAX_ROWS
        self._sorted_top = sorted_top
        self.queries = []

    def _candidates(self):
        preds = {}
        for s, p in self._triples:
            preds.setdefault(s, set()).add(p)
        return sorted(
            s for s, ps in preds.items()
            if IS_CURRENT in ps
            and ps <= set(porp._ROLLUP_PREDICATES)
            and s.startswith(N)
        )

    def query(self, q):
        self.queries.append(q)
        if "VALUES ?t" in q:
            wanted = {v.strip("<>") for v in
                      q.split("VALUES ?t {")[1].split("}")[0].split()}
            live = {s for s, _ in self._triples}
            return [{"t": t} for t in sorted(wanted & live)]
        limit = int(q.split("LIMIT")[1].split()[0]) if "LIMIT" in q else None
        if "ORDER BY" in q and limit is not None and limit > self._sorted_top:
            raise RuntimeError(
                "SR353: Sorted TOP clause specifies more then "
                f"{self._sorted_top + 5} rows to sort"
            )
        rows = self._candidates()
        if 'STR(?s) > "' in q:
            after = q.split('STR(?s) > "')[1].split('"')[0]
            rows = [r for r in rows if r > after]
        if limit is not None:
            rows = rows[:limit]
        return [{"s": r} for r in rows[:self._cap]]


def _store(*subjects_with_preds):
    return [(s, p) for s, preds in subjects_with_preds for p in preds]


def test_a_rollup_only_notice_subject_is_a_candidate():
    """The shape the bug produced: a Notice subject holding nothing but
    the two rollup predicates."""
    v = _Virtuoso(_store(
        (f"{N}639139-2020", [IS_CURRENT]),
        (f"{C}639139-2020", [IS_CURRENT, f"{porp._FONTEM}noticeType"]),
    ))
    assert porp.find_candidates(v, G) == [f"{N}639139-2020"]


def test_a_notice_subject_holding_real_triples_is_never_a_candidate():
    """The one thing that must not happen. If a real event ever rendered
    a notice at the Notice subject, that subject holds the only copy of
    those triples -- purging it destroys data rather than a duplicate."""
    v = _Virtuoso(_store(
        (f"{N}real-1", [IS_CURRENT, CURRENT_VALUE,
                        f"{porp._FONTEM}tedNoticeId"]),
    ))
    assert porp.find_candidates(v, G) == []


def test_currentvalue_alongside_iscurrent_is_still_a_candidate():
    """A rollup carrying a figure renders both predicates; that is still
    a rollup, not a real notice."""
    v = _Virtuoso(_store((f"{N}n-2", [IS_CURRENT, CURRENT_VALUE])))
    assert porp.find_candidates(v, G) == [f"{N}n-2"]


def test_contract_subjects_are_never_candidates():
    """After #130 the rollup lands on the Contract subject. Those hold
    the notice's real triples and must never be swept up by a later run
    of this script."""
    v = _Virtuoso(_store((f"{C}n-3", [IS_CURRENT, CURRENT_VALUE])))
    assert porp.find_candidates(v, G) == []


def test_splits_candidates_by_whether_a_contract_twin_exists():
    """A candidate with no twin is the only record of that notice, so it
    is reported separately and skipped by default."""
    v = _Virtuoso(_store(
        (f"{N}has-twin", [IS_CURRENT]),
        (f"{C}has-twin", [f"{porp._FONTEM}noticeType"]),
        (f"{N}lonely", [IS_CURRENT]),
    ))
    cands = porp.find_candidates(v, G)
    with_twin, without = porp.partition_by_twin(v, G, cands)
    assert with_twin == [f"{N}has-twin"]
    assert without == [f"{N}lonely"]


def test_keyset_paging_walks_past_one_page():
    """OFFSET paging is impossible here (MaxSortedTopRows), so the scan
    must advance by key. A page-size regression would silently report
    only the first page's worth of subjects."""
    subjects = [f"{N}n-{i:05d}" for i in range(250)]
    v = _Virtuoso(_store(*[(s, [IS_CURRENT]) for s in subjects]))
    assert porp.find_candidates(v, G, page=100) == sorted(subjects)
    assert len(v.queries) >= 3


def test_a_page_at_the_cap_is_refused():
    """ResultSetMaxRows truncates with no signal -- HTTP 200, just fewer
    rows. A page that comes back at the cap is indistinguishable from a
    truncated one, so the scan must raise rather than silently
    under-report and leave orphans behind. Driven with a server that
    ignores LIMIT, which is what truncation looks like."""

    class _Truncating:
        def query(self, _q):
            return [{"s": f"{N}n-{i}"} for i in range(5)]

    try:
        porp.find_candidates(_Truncating(), G, page=4, cap=5)
    except RuntimeError as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError at the result-set cap")


def test_page_size_at_or_above_the_cap_is_rejected_up_front():
    v = _Virtuoso([])
    try:
        porp.find_candidates(v, G, page=50_000, cap=50_000)
    except ValueError as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("expected a ValueError for an unsafe page size")


def test_purge_names_the_subject_verbatim_and_points_at_the_twin():
    """PurgeSubject is the only event that can name these subjects, and
    it passes the IRI through byte-for-byte. The reason string must name
    the surviving twin so the audit trail explains what was kept."""
    log = MagicMock()
    emit = log.batch.return_value.__enter__.return_value
    sent = porp.emit_purges(log, G, [f"{N}639139-2020"])
    assert sent == 1
    (event_type, payload), _ = emit.control.call_args
    assert event_type == "PurgeSubject"
    assert payload["subject_iri"] == f"{N}639139-2020"
    assert payload["graph_iri"] == G
    assert f"{C}639139-2020" in payload["reason"]
    # The sink refuses a purge of an ordinary IRI without this, and
    # with it checks the store before deleting anything.
    assert set(payload["only_predicates"]) == set(porp._ROLLUP_PREDICATES)


def test_dry_run_emits_nothing(monkeypatch):
    """--apply is the only thing that may write. A dry run must not even
    construct an EventLog."""
    virtuoso_cls = MagicMock()
    virtuoso_cls.from_env.return_value = _Virtuoso(_store(
        (f"{N}n-9", [IS_CURRENT]), (f"{C}n-9", [f"{porp._FONTEM}cpv"]),
    ))
    monkeypatch.setattr(porp, "VirtuosoClient", virtuoso_cls)
    event_log_cls = MagicMock()
    monkeypatch.setattr(porp, "EventLog", event_log_cls)
    porp.main([])
    event_log_cls.from_env.assert_not_called()


def test_twin_lookup_is_chunked_small_enough_for_a_get_url():
    """Virtuoso answers 400 Bad Request when the GET query string gets
    long, and says nothing about size. The first shared run failed that
    way at 1,000 IRIs per chunk (~64KB of URL); the guard is that the
    chunk stays small, so assert both the default and that the code
    actually splits by it."""
    assert porp._TWIN_CHUNK <= 200
    subjects = [f"{N}n-{i:04d}" for i in range(250)]
    triples = _store(*[(s, [IS_CURRENT]) for s in subjects])
    triples += [(porp._twin(s), [f"{porp._FONTEM}cpv"]) for s in subjects]
    v = _Virtuoso(triples)
    with_twin, without = porp.partition_by_twin(
        v, G, sorted(subjects), chunk_size=100,
    )
    assert with_twin == sorted(subjects)
    assert not without
    values_queries = [q for q in v.queries if "VALUES ?t" in q]
    assert len(values_queries) == 3
    for q in values_queries:
        assert q.count("<http") <= 101   # 100 values + the graph IRI
