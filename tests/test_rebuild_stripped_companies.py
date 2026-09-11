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


def _neo4j(records, seen_queries=None):
    """Answers each labelled lookup with the records carrying that label,
    the way the real index seek would."""
    client = MagicMock()
    session = client.session.return_value
    session.__enter__ = lambda s: s
    session.__exit__ = lambda s, *a: None

    def _run(query, **_kw):
        if seen_queries is not None:
            seen_queries.append(query)
        label = query.split("MATCH (c:")[1].split(")")[0]
        res = MagicMock()
        res.data.return_value = [
            {"gmr_id": r["gmr_id"], "labels": r.get("labels") or [],
             "props": {k: v for k, v in r.items() if k != "labels"}}
            for r in records if label in (r.get("labels") or [])
        ]
        return res

    session.run.side_effect = _run
    return client


def test_entity_kind_is_never_sent():
    """Two hazards ride on it. An UpsertCompany carrying entity_kind
    makes the sink issue an extra UNFILTERED delete of the sibling
    InvestmentFund subject -- unfiltered meaning it does not exempt
    owl:sameAs, so the repair would destroy the very assertion it is
    meant to preserve."""
    assert "entity_kind" not in rsc._FIELDS
    assert "entity_kind" not in rsc._IDENTITY_FIELDS
    payloads, _, _ = rsc.load_from_neo4j(_neo4j([
        {"gmr_id": "a", "labels": ["Company"], "name": "Acme",
         "country": "FRA", "entity_kind": "GENERAL", "lei": None, "vat": None,
         "cik": None, "active": True, "legal_form": None,
         "postal_code": None},
    ]), ["a"])
    assert payloads and "entity_kind" not in payloads[0]
    # ...and not smuggled in through the identity block either, which is
    # where load_gleif sends it.
    assert "entity_kind" not in payloads[0]["identity"]


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


def test_selection_is_the_damage_signature_not_just_sameas():
    """The signature is "carries owl:sameAs AND has lost its rdf:type" --
    exactly what a whole-subject wipe leaves behind. A healthy company
    always carries a type, so this excludes intact records and any
    company that legitimately gains an owl:sameAs later. Selecting on
    owl:sameAs alone would be right today but is a fact about the
    incident, not an invariant.

    It also has to be a lookup rather than an aggregation: the
    predicate-counting version selected the same 26,752 subjects and had
    not returned after 15 minutes on prod, because the group-by runs
    across the whole graph for every page. This answers in 431ms."""
    v = _Virtuoso([f"{rsc._COMPANY_PREFIX}a"])
    rsc.find_stripped(v, page=10)
    q = v.queries[0]
    assert "FILTER NOT EXISTS { ?s a ?t }" in q
    assert "owl#sameAs" in q
    assert "COUNT(" not in q, "per-page aggregation is what made this unusable"
    assert "GROUP BY" not in q


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


def test_every_lookup_is_labelled_so_it_can_use_an_index():
    """The regression. The first version matched `(c)` with no label so it
    would find funds too, and Neo4j planned that as an AllNodesScan --
    every node in prod, per 500-id batch, on the instance serving the
    live API. The prod dry run sat at ~1 core for 18 minutes without
    finishing. A labelled MATCH is a NodeIndexSeek (company_gmr_id,
    investmentfund_gmr_id)."""
    queries = []
    rsc.load_from_neo4j(_neo4j([], seen_queries=queries), ["a"])
    assert queries, "no lookup ran"
    for q in queries:
        assert "MATCH (c:" in q, f"label-less lookup would scan every node: {q}"
    labels = {q.split("MATCH (c:")[1].split(")")[0] for q in queries}
    assert labels == {"Company", "InvestmentFund"}


def test_a_node_with_both_labels_is_classified_once_as_a_fund():
    """Querying per label returns a dual-labelled node twice. It must
    count once, and as a fund: repairing a fund as a Company leaves the
    stripped Company subject stripped while reporting success."""
    both = {"gmr_id": "d1", "labels": ["Company", "InvestmentFund"],
            "name": "Dual", "country": "LUX", "entity_kind": None,
            "lei": None, "vat": None, "cik": None, "active": True,
            "legal_form": None, "postal_code": None}
    payloads, missing, funds = rsc.load_from_neo4j(_neo4j([both]), ["d1"])
    assert not payloads
    assert funds == ["d1"]
    assert not missing


_SHAKTI = {  # a real stripped prod company with the full GLEIF block
    "gmr_id": "013aea9c-e2b3-5c2d-a7be-65e618627964", "labels": ["Company"],
    "name": "SHAKTI ENTERPRISES", "name_clean": "shaktienterprises",
    "country": "IND", "active": True, "lei": "9845001C3AME84BC4428",
    "legal_form": "4QIE", "postal_code": "202001",
    "entity_kind": "SOLE_PROPRIETOR", "registration_status": "LAPSED",
    "registered_as": "0606008845", "registered_at": "RA000709",
    "jurisdiction": "IN", "entity_creation_date": "2007-02-07T00:00:00+00:00",
    "address": "B - 1, SECTOR - 1, TALANAGARI INDUSTRIAL AREA, UPSIDA",
    "city": "ALIGARH", "region": "IN-UP",
    "hq_address": "B - 1, SECTOR - 1, TALANAGARI INDUSTRIAL AREA, UPSIDA",
    "hq_city": "ALIGARH", "hq_region": "IN-UP", "hq_country": "IND",
    "hq_postal_code": "202001", "aliases": ["GAURAV MITTAL"],
    "last_consolidated_at": "2026-09-02T18:30:25.413Z",
}


def test_the_identity_block_is_carried_from_neo4j():
    """UpsertCompany also reaches the embedding sink, whose upsert is a
    whole replace built from `name · aliases · (city, country,
    legal_form)`. Dropping city and aliases would overwrite good search
    vectors with thinner ones -- ~4,700 companies losing city context and
    ~1,650 losing aliases, extrapolated from a prod sample."""
    payloads, _, _ = rsc.load_from_neo4j(_neo4j([_SHAKTI]), [_SHAKTI["gmr_id"]])
    ident = payloads[0]["identity"]
    assert ident["city"] == "ALIGARH"
    assert ident["aliases"] == ["GAURAV MITTAL"]
    assert ident["hq_country"] == "IND"
    assert ident["registration_status"] == "LAPSED"


def test_node_properties_outside_the_schema_never_reach_the_event():
    """The lookup returns properties(c), which includes bookkeeping the
    schema does not allow (name_clean, last_consolidated_at). The schema
    is additionalProperties:false, so a stray key fails validation --
    loudly, but only after the event is built."""
    payloads, _, _ = rsc.load_from_neo4j(_neo4j([_SHAKTI]), [_SHAKTI["gmr_id"]])
    p = payloads[0]
    flat = set(p) | set(p["identity"])
    assert "name_clean" not in flat
    assert "last_consolidated_at" not in flat


def test_the_built_payload_validates_against_the_real_schema():
    """The check that would have caught both earlier gaps at once. The
    UpsertCompany schema is additionalProperties:false, so building the
    actual event from a real prod node and validating it proves every
    field is one the schema knows, under the name it expects."""
    from fontem_event_schemas import validate  # pylint: disable=import-outside-toplevel
    from fontem_event_schemas import builders  # pylint: disable=import-outside-toplevel
    payloads, _, _ = rsc.load_from_neo4j(_neo4j([_SHAKTI]), [_SHAKTI["gmr_id"]])
    event_payload = builders.upsert_company(**payloads[0])
    validate("UpsertCompany", 1, event_payload)
    assert event_payload["city"] == "ALIGARH"
    assert event_payload["aliases"] == ["GAURAV MITTAL"]
    assert "entity_kind" not in event_payload


def test_a_thin_company_still_validates():
    """Most of the stripped set carries only name/country/active. Their
    identity block is all None, which the builder drops -- the payload
    must still be valid, not fail on an empty mapping."""
    from fontem_event_schemas import validate  # pylint: disable=import-outside-toplevel
    from fontem_event_schemas import builders  # pylint: disable=import-outside-toplevel
    thin = {"gmr_id": "0000a491-707c-55ed-a0bc-dc35e9f282ea",
            "labels": ["Company"], "name": "Autocares La Inmaculada, SL",
            "name_clean": "autocareslainmaculadasl", "country": "ESP",
            "active": True}
    payloads, _, _ = rsc.load_from_neo4j(_neo4j([thin]), [thin["gmr_id"]])
    event_payload = builders.upsert_company(**payloads[0])
    validate("UpsertCompany", 1, event_payload)
    assert set(event_payload) == {"gmr_id", "name", "country", "active"}
