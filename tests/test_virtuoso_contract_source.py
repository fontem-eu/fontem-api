"""Company contracts read from Virtuoso, aggregated across owl:sameAs.

Neo4j holds no equivalences — the :SAME_AS edge was removed from it
precisely because nothing followed it — so a company page built on Neo4j
shows one record's contracts and silently omits its duplicates'. On prod
a company whose bare subject has 3 contracts has 720 across its closure,
and its duplicate record returns the identical 720; before this, the two
pages disagreed.

Only get_company_contracts moves. Everything else delegates, including
the corporate-group walk (SUBSIDIARY_OF*1..5) which is a real graph
traversal and stays where it belongs.
"""

import pytest

from src.data.sparql.virtuoso_contract_source import VirtuosoContractSource
from src.data.sparql.virtuoso_client import SparqlTimeout


class _Fallback:
    """Records what the Neo4j-backed source was asked for."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def _record(*_a, **_k):
            self.calls.append(name)
            return {"from": "neo4j"}
        return _record


class _Virtuoso:
    def __init__(self, rows=None, counts=None, totals=None, auths=None):
        self.queries: list[str] = []
        self._rows = rows if rows is not None else []
        self._counts = counts if counts is not None else [{"cnt": "0"}]
        self._totals = totals if totals is not None else [{"total": "0"}]
        self._auths = auths if auths is not None else []

    def query(self, q):
        self.queries.append(q)
        if "COUNT(DISTINCT ?n)" in q:
            return self._counts
        if "SUM(?v)" in q:
            return self._totals
        if "graph/authority" in q:
            return self._auths
        return self._rows


def _src(virtuoso=None, fallback=None):
    return VirtuosoContractSource(
        fallback=fallback or _Fallback(), virtuoso=virtuoso,
    )


# ── the closure, which is the whole point ─────────────────────────


def test_contracts_are_read_through_the_sameas_closure():
    v = _Virtuoso()
    _src(v).get_company_contracts("abc")
    rows_q = v.queries[0]
    assert "owl#sameAs" in rows_q
    assert ")* ?me" in rows_q, "closure must be zero-or-more"
    assert "|^<" in rows_q, "closure must walk the inverse leg too"


def test_both_award_predicates_are_counted():
    """A company is on a notice either as the resolved awardee or as a
    winner in parties[]; either means it won the contract."""
    v = _Virtuoso()
    _src(v).get_company_contracts("abc")
    assert "awardedTo" in v.queries[0] and "winner" in v.queries[0]


# ── the aggregate, and the Virtuoso trap under it ─────────────────


def test_count_and_total_are_separate_queries():
    """Virtuoso's IF(BOUND(?x), 0, ...) silently evaluates to 0 for every
    row — verified on prod, the same aggregate returns 367,721,491.42
    with a plain COALESCE and 0 with an IF wrapped round it. Excluding
    the flagged rows with a FILTER gives the right value but also drops
    them from a COUNT in the same query, and the Cypher this replaces
    counts them while contributing 0. Hence two queries.
    """
    v = _Virtuoso(counts=[{"cnt": "720"}], totals=[{"total": "365042992.2"}])
    out = _src(v).get_company_contracts("abc")
    assert out["contract_count"] == 720
    assert out["total_contract_value_eur"] == pytest.approx(365042992.2)
    joined = " ".join(v.queries)
    assert "IF(BOUND(" not in joined, (
        "IF(BOUND(...)) evaluates to 0 for every row in Virtuoso"
    )


def test_modification_restatements_do_not_double_count():
    """A contract amended three times is one contract, not four. The
    canonical filter is is_current when the collapse pass has spoken,
    else 'not a can-modif restatement'."""
    v = _Virtuoso()
    _src(v).get_company_contracts("abc")
    count_q = next(q for q in v.queries if "COUNT(DISTINCT ?n)" in q)
    assert "isCurrent" in count_q
    assert 'can-modif' in count_q


def test_low_confidence_values_are_excluded_from_the_total():
    v = _Virtuoso()
    _src(v).get_company_contracts("abc")
    total_q = next(q for q in v.queries if "SUM(?v)" in q)
    assert "valueLowConfidence" in total_q
    assert "!BOUND(?low)" in total_q


# ── shape and safety ──────────────────────────────────────────────


def test_rows_carry_the_wire_shape_and_leak_no_internals():
    v = _Virtuoso(
        rows=[{
            "n": "http://x/Notice/1", "notice_id": "n-1", "title": "Works",
            "value_eur": "100.5", "award_date": "2026-01-01",
            "auth": "http://data.fontem.eu/id/Authority/a-1",
        }],
        auths=[{"a": "http://data.fontem.eu/id/Authority/a-1",
                "label": "City Council", "country": "POL"}],
    )
    out = _src(v).get_company_contracts("abc")
    row = out["contracts"][0]
    assert row["ted_notice_id"] == "n-1"
    assert row["authority"] == "City Council"
    assert row["authority_id"] == "a-1"
    assert row["authority_country"] == "POL"
    assert not [k for k in row if k.startswith("_")], "internal keys leaked"


def test_authorities_are_resolved_in_one_batched_query():
    """Joining the authority graph inside the rows query costs Virtuoso
    ~10,000s by its own estimate and is refused outright; a VALUES-bound
    second query runs in 0.016s."""
    v = _Virtuoso(
        rows=[{"n": f"http://x/{i}", "auth": f"http://data.fontem.eu/id/Authority/a-{i}"}
              for i in range(5)],
    )
    _src(v).get_company_contracts("abc")
    auth_queries = [q for q in v.queries if "graph/authority" in q]
    assert len(auth_queries) == 1
    assert auth_queries[0].count("VALUES") == 1


def test_rows_without_an_authority_still_render():
    v = _Virtuoso(rows=[{"n": "http://x/1", "notice_id": "n-1"}])
    row = _src(v).get_company_contracts("abc")["contracts"][0]
    assert row["authority"] is None
    assert row["authority_id"] is None


# ── delegation ────────────────────────────────────────────────────


def test_no_virtuoso_configured_delegates_everything():
    """An environment that has not enabled Virtuoso behaves exactly as
    before rather than showing an empty page."""
    fb = _Fallback()
    assert _src(None, fb).get_company_contracts("abc") == {"from": "neo4j"}
    assert fb.calls == ["get_company_contracts"]


def test_a_virtuoso_timeout_falls_back_rather_than_blanking_the_page():
    class _Slow:
        def query(self, _q):
            raise SparqlTimeout("too slow")

    fb = _Fallback()
    assert _src(_Slow(), fb).get_company_contracts("abc") == {"from": "neo4j"}
    assert fb.calls == ["get_company_contracts"]


@pytest.mark.parametrize("method", [
    "get_authority_contracts", "get_contract_detail", "get_sector_summary",
    "get_company_cohesion_grants", "get_single_bidder_stats",
    "get_single_bidder_by_country", "get_stored_publication_number",
])
def test_everything_else_stays_on_the_graph_store(method):
    """Only the company-contracts read moves. The rest — including
    anything needing real traversal — keeps using Neo4j."""
    fb = _Fallback()
    src = _src(_Virtuoso(), fb)
    getattr(src, method)("x")
    assert fb.calls == [method]


# ── wire shape ────────────────────────────────────────────────────


def test_returns_exactly_the_keys_the_neo4j_source_returns():
    """The regression this file failed to catch.

    The router reads company_name / country / total_contract_value_eur
    straight off this dict. Dropping the first two and renaming the
    third produced a 200 with a nameless company and no contracts —
    which renders a blank profile rather than raising, so nothing caught
    it until the e2e gate said "/company/<id> did not resolve".

    Asserting my own shape is worthless here; the shape has to be pinned
    against the implementation being replaced.
    """
    v = _Virtuoso(
        rows=[{"n": "http://x/1", "notice_id": "n-1"}],
        counts=[{"cnt": "7"}],
        totals=[{"total": "1234.5"}],
    )
    out = _src(v).get_company_contracts("abc")
    assert set(out) == {
        "gmr_id", "company_name", "country",
        "total_contract_value_eur", "contract_count", "contracts",
    }


def test_identity_prefers_the_requested_record():
    """The visitor asked for this record; its own name wins when it has
    one."""
    v = _Virtuoso(rows=[])

    def query(q):
        v.queries.append(q)
        if "?name" in q:
            return [{"name": "Own Name", "country": "POL"}]
        if "COUNT(DISTINCT ?n)" in q:
            return [{"cnt": "0"}]
        if "SUM(?v)" in q:
            return [{"total": "0"}]
        return []

    v.query = query
    out = _src(v).get_company_contracts("abc")
    assert out["company_name"] == "Own Name"
    first_name_q = next(q for q in v.queries if "?name" in q)
    assert "Company/abc" in first_name_q
    assert "sameAs" not in first_name_q, "the record's own name needs no closure"


def test_identity_falls_back_to_the_closure_when_the_record_is_stripped():
    """Historical sink bugs stripped subjects to a bare owl:sameAs.
    Verified on prod: company fb2107f4 carries ONLY that triple while
    its approved twin holds the real name. A nameless page is worse than
    the twin's name, and the closure is the same entity by construction.
    """
    calls: list[str] = []

    class _V:
        def query(self, q):
            calls.append(q)
            if "?name" in q:
                # first (own) lookup empty, second (closure) resolves
                return [] if "sameAs" not in q else [{"name": "Twin Name", "country": "POL"}]
            if "COUNT(DISTINCT ?n)" in q:
                return [{"cnt": "0"}]
            if "SUM(?v)" in q:
                return [{"total": "0"}]
            return []

    out = _src(_V()).get_company_contracts("abc")
    assert out["company_name"] == "Twin Name"
    assert out["country"] == "POL"
    assert sum(1 for q in calls if "?name" in q) == 2, "own first, then closure"


def test_totals_default_to_zero_not_none():
    """The router does `.get("total_contract_value_eur", 0)` but a
    present-and-None key defeats that default and reaches the template
    as null."""
    v = _Virtuoso(totals=[])
    out = _src(v).get_company_contracts("abc")
    assert out["total_contract_value_eur"] == 0
