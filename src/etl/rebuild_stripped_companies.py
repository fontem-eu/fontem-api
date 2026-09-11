"""Rebuild company subjects Virtuoso lost, from Neo4j.

Two kinds of damage, chosen with ``--select``.

``stripped`` (default): the AssertSameAs wipe
---------------------------------------------
On 2026-09-03 the prod consolidator had event emission on while the
Virtuoso sink still treated AssertSameAs as an ordinary upsert. Ordinary
upserts were whole-subject replaces -- ``DELETE WHERE { <s> ?p ?o }`` --
so each assertion deleted the company's entire record and inserted a
lone ``owl:sameAs``. Emission was turned off the same day; the sink was
fixed later (AssertSameAs is now in ADDITIVE_EVENTS), which stops it
happening again but does not restore what was already lost.

Measured on prod 2026-09-10: 26,714 company subjects carry exactly one
predicate, 11 carry two and 27 carry three -- the partials are subjects
that were stripped totally and then had a predicate written back by a
later scoped write. Every one of the 26,752 subjects carrying
``owl:sameAs`` in graph/company is damaged; none carries ``rdf:type``,
which is the signature that distinguishes this from every other thin
subject in the graph.

``labelless``: company subjects with no name
--------------------------------------------
Measured on prod 2026-09-11: 2,579,228 company subjects have no
``rdfs:label`` -- 2,424,580 Company and 154,648 InvestmentFund -- and
2,359,235 of them carry nothing at all but their ``rdf:type``. Neo4j
holds them intact: 80 of 80 sampled have a name, each under the label
its subject says (Company -> GENERAL, InvestmentFund -> FUND).

Partial producers explain much of it. load_gleif_relationships sends
``{gmr_id, lei}`` and load_ted_contracts ``{name, country, active}``, and
until the sink scoped partial UpsertCompany events each one replaced the
whole subject: the last event of 369 of 500 damaged subjects is one of
those, against 148 of 1,000 healthy ones. Why the rest lost even the
fields a partial event writes is not established; the shared store,
replayed from the same log by the current sink, has none.

How the repair works
--------------------
Re-emit ``UpsertCompany`` built from the Neo4j node. Neo4j is the
source, not the event log: re-emitting a subject's last logged payload
restores exactly the partial description that did the damage.
``owl:sameAs`` survives -- the sink's replace never deletes it.

Run it only against a virtuoso-sink with fontem-virtuoso-sink#133. A
Company rebuild carries no entity_kind, which that sink applies as a
replace of the fields the event states; before it the same event was a
whole-subject replace, and a subject's fontem:subsidiaryOf edges live on
the subject -- 29 of 40 label-less subjects sampled on shared carry them.

What ``entity_kind`` does, and when the repair sends it
-------------------------------------------------------
An ``UpsertCompany`` carrying ``entity_kind`` makes the sink issue an
extra, UNFILTERED delete of the InvestmentFund subject -- unfiltered
meaning it does not exempt owl:sameAs -- and, for ``FUND``, write the
record at the InvestmentFund subject instead of the Company one. Prod
holds 245,585 funds, so neither is hypothetical.

The stripped repair therefore never sends it and skips funds. The
labelless repair needs it to reach the right subject, so it sends it
only when that is the point, and holds back any entity whose subjects
carry something the rebuild would delete without writing back --
owl:sameAs, fontem:subsidiaryOf edges (see ``would_lose``):

* a fund is rebuilt exactly as load_gleif writes one -- ``entity_kind``
  FUND at the Company IRI -- so the sink refreshes the InvestmentFund
  subject and drops any Company twin;
* an InvestmentFund subject whose node is not a fund is a stale twin,
  rebuilt with the node's own ``entity_kind`` so the sink drops it. With
  no kind to state it is reported: inventing one writes it to both
  stores.

Pace and restarts
-----------------
Every consumer reads the one ordered event log, so a burst holds every
later event -- the daily ETL included -- behind it until the slowest sink
drains it. The embedding sink kept up with the 30k repair at ~40
events/s; ``--max-rate`` (default 30/s) keeps this under that.

The labelless scan is page by page: select, look up, emit, then the next
page. The selection is the damage, so a restarted run finds only what is
still damaged; ``--start-after`` skips ahead to the last key logged.

Usage::

    python -m src.etl.rebuild_stripped_companies                  # report
    python -m src.etl.rebuild_stripped_companies --apply          # emit
    python -m src.etl.rebuild_stripped_companies --select labelless --limit 50000
    python -m src.etl.rebuild_stripped_companies --select labelless --apply
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from collections.abc import Callable, Iterator

from fontem_event_schemas import builders
from fontem_events import EventLog

from src.data.graph.neo4j_client import Neo4jClient
from src.data.sparql.virtuoso_client import VirtuosoClient

logger = logging.getLogger(__name__)

_G_COMPANY = "http://data.fontem.eu/graph/company"
_OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
# Both are RDF IRIs (the RDFS vocabulary, Fontem's subject namespace),
# not network endpoints. Schemes are spec-defined.
_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"  # NOSONAR
_ID_PREFIX = "http://data.fontem.eu/id/"  # NOSONAR
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"  # NOSONAR
_WDT_P17 = "http://www.wikidata.org/prop/direct/P17"  # NOSONAR
_FONTEM = "http://data.fontem.eu/ontology#"  # NOSONAR
_COMPANY_PREFIX = f"{_ID_PREFIX}Company/"

#: Virtuoso's ResultSetMaxRows. A page at or above this is assumed
#: truncated, because the server does not say which it is.
_RESULT_SET_MAX_ROWS = 50_000
_PAGE = 5_000

#: Events/s ceiling for --apply. Under the ~40/s the embedding sink held
#: on the 30k repair, so no backlog builds in front of the daily ETL.
_DEFAULT_MAX_RATE = 30.0

#: gmr_ids per would_lose check: two subject IRIs each, in the query string.
_HOLD_CHUNK = 50

#: Why a label-less subject was not rebuilt.
MISSING = "missing"
NAMELESS = "nameless"
UNKINDED_TWIN = "unkinded_twin"
HELD_WOULD_LOSE = "would_lose_data"
_SKIP_REASONS = (MISSING, NAMELESS, UNKINDED_TWIN, HELD_WOULD_LOSE)

#: Fields copied from the Neo4j node into the event payload. entity_kind
#: is deliberately absent -- see the module docstring for when it is added.
_FIELDS = ("name", "country", "lei", "vat", "cik", "active",
           "legal_form", "postal_code")

#: The GLEIF identity block, exactly as load_gleif sends it (it passes
#: builders.COMPANY_IDENTITY_FIELDS through `identity=`), minus
#: entity_kind for the same hazard. Taken from the canonical tuple, not
#: copied, so a field added to the schema reaches the repair too.
#:
#: Without this the repair was a quiet regression on a second store.
#: UpsertCompany also reaches the embedding sink, whose upsert is a whole
#: replace built from `name · aliases · (city, country, legal_form)`. On a
#: 498-company sample of the stripped set, 18% carry city and 6% aliases
#: in Neo4j; a payload without them would have overwritten those
#: companies' search vectors with thinner ones -- roughly 4,700 losing
#: city context and 1,650 losing aliases -- while "repairing" Virtuoso.
_IDENTITY_FIELDS = tuple(
    k for k in builders.COMPANY_IDENTITY_FIELDS if k != "entity_kind"
)


def _check_page_size(page: int, cap: int) -> None:
    if page >= cap:
        raise ValueError(
            f"page size {page} is not below Virtuoso's result-set cap "
            f"({cap}); a full page could not be told from a truncated one"
        )


def _keyset_page(virtuoso: VirtuosoClient, query: str, last: str,
                 cap: int) -> list[str]:
    """One keyset page of subject IRIs, refusing a truncated page and a
    key that does not advance."""
    rows = virtuoso.query(query)
    if len(rows) >= cap:
        raise RuntimeError(
            f"page of {len(rows)} rows hit Virtuoso's result-set cap; "
            "results may be silently truncated. Lower --page-size."
        )
    got = [r["s"] for r in rows if r.get("s")]
    if got and max(got) <= last:
        raise RuntimeError(
            f"keyset did not advance past {last!r}; refusing to loop"
        )
    return got


def find_stripped(virtuoso: VirtuosoClient, page: int = _PAGE,
                  cap: int = _RESULT_SET_MAX_ROWS) -> list[str]:
    """Company gmr_ids whose Virtuoso subject was stripped.

    Keyset-paged: ResultSetMaxRows truncates silently (HTTP 200, fewer
    rows) and MaxSortedTopRows makes OFFSET paging past row 10,000 a hard
    SR353, so neither a single scan nor offset paging is safe at 26,752
    rows.

    The signature is "carries owl:sameAs and has lost its rdf:type".
    That is exactly what a whole-subject wipe does -- it deletes the type
    with everything else -- and a healthy company always carries one, so
    the test excludes both intact records and any company that
    legitimately gains an owl:sameAs later. Selecting on owl:sameAs alone
    would be right today (every sameAs subject in graph/company is
    damaged) but that is a fact about the incident, not an invariant.

    The first version of this counted predicates per subject and kept
    those at or below three. It selected the same 26,752 subjects and was
    unusable: the aggregation runs across the whole graph for every page
    and had not returned after 15 minutes on prod. FILTER NOT EXISTS
    answers in 431ms, because it is a lookup rather than a group-by.
    """
    _check_page_size(page, cap)
    found: list[str] = []
    last = ""
    while True:
        got = _keyset_page(virtuoso, f"""
SELECT ?s WHERE {{
  GRAPH <{_G_COMPANY}> {{
    ?s <{_OWL_SAME_AS}> ?o .
    FILTER NOT EXISTS {{ ?s a ?t }}
  }}
  FILTER(STR(?s) > "{last}")
}}
ORDER BY ?s LIMIT {page}
""", last, cap)
        if not got:
            return found
        found.extend(s[len(_COMPANY_PREFIX):] for s in got
                     if s.startswith(_COMPANY_PREFIX))
        last = max(got)
        if len(got) < page:
            return found


def _labelless_query(after: str, page: int) -> str:
    return f"""
SELECT ?s WHERE {{
  GRAPH <{_G_COMPANY}> {{
    ?s a ?t .
    FILTER NOT EXISTS {{ ?s <{_RDFS_LABEL}> ?l }}
  }}
  FILTER(STR(?s) > "{after}")
}}
ORDER BY ?s LIMIT {page}
"""


def _subject_ref(iri: str) -> tuple[str, str] | None:
    """``(subject label, gmr_id)`` for a Company or InvestmentFund IRI."""
    if not iri.startswith(_ID_PREFIX):
        return None
    label, _, gid = iri[len(_ID_PREFIX):].partition("/")
    if label not in _LOOKUP_LABELS or not gid or "/" in gid:
        return None
    return label, gid


def iter_labelless(
    virtuoso: VirtuosoClient, page: int = _PAGE,
    cap: int = _RESULT_SET_MAX_ROWS, start_after: str = "",
) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """Yield ``(last key, [(subject label, gmr_id), ...])`` per page of
    company subjects that have no ``rdfs:label``.

    "Has no label", not "has nothing but a type". The second is the
    literal description of the damage and was the first query tried: its
    ``FILTER NOT EXISTS { ?s ?p ?o . FILTER(?p != rdf:type) }`` returned
    nothing in 500s on prod. A single-predicate NOT EXISTS is a lookup --
    5,000 rows in 12.5s -- and it also takes in the ~220k subjects that
    kept a stray lei or owl:sameAs but lost their name, which are just as
    broken.
    """
    _check_page_size(page, cap)
    last = start_after
    while True:
        got = _keyset_page(virtuoso, _labelless_query(last, page), last, cap)
        if not got:
            return
        last = max(got)
        # A subject with two types comes back twice.
        refs = [ref for s in dict.fromkeys(got) if (ref := _subject_ref(s))]
        yield last, refs
        if len(got) < page:
            return


#: Looked up one label at a time, never label-less. Both labels carry a
#: gmr_id index (company_gmr_id, investmentfund_gmr_id), and only a
#: labelled MATCH can use one. The first version matched `(c)` with no
#: label so it would find funds too; Neo4j planned that as an
#: AllNodesScan -- every node in prod, per 500-id batch, on the instance
#: serving the live API. The prod dry run sat at ~1 core for 18 minutes
#: without finishing the first phase. Per label it is a NodeIndexSeek.
_LOOKUP_LABELS = ("Company", "InvestmentFund")


def _is_fund(row: dict) -> bool:
    return ("InvestmentFund" in (row.get("labels") or [])
            or (row.get("props") or {}).get("entity_kind") == "FUND")


def _lookup_query(label: str) -> str:
    # The whole property map, filtered in Python to what the schema
    # allows. Enumerating columns here is how the identity block went
    # missing the first time; the schema is additionalProperties:false,
    # so the filter below -- not this query -- is what keeps stray node
    # properties (name_clean, last_consolidated_at) out of the event.
    return (
        f"MATCH (c:{label}) WHERE c.gmr_id IN $ids "
        "RETURN c.gmr_id AS gmr_id, labels(c) AS labels, "
        "  properties(c) AS props"
    )


def _lookup(session, chunk: list[str]) -> dict[str, dict]:
    """One row per gmr_id. Querying per label means a node that carries
    BOTH labels comes back twice; fund classification wins, because
    repairing a fund as a Company is a silent no-op."""
    by_id: dict[str, dict] = {}
    for label in _LOOKUP_LABELS:
        for r in session.run(_lookup_query(label), ids=chunk).data():
            prev = by_id.get(r["gmr_id"])
            if prev is None or _is_fund(r):
                by_id[r["gmr_id"]] = r
    return by_id


def _payload(gmr_id: str, props: dict) -> dict:
    return {
        "gmr_id": gmr_id,
        **{f: props.get(f) for f in _FIELDS},
        "identity": {k: props.get(k) for k in _IDENTITY_FIELDS},
    }


def load_from_neo4j(neo4j: Neo4jClient, gmr_ids: list[str],
                    batch: int = 500) -> tuple[list[dict], list[str], list[str]]:
    """Return (payloads, missing_ids, fund_ids) for the stripped repair.

    Funds are separated rather than repaired: the renderer would write
    them at the InvestmentFund subject and leave the Company subject
    stripped. Missing ids are the dedup losers -- nodes the consolidator
    removed -- which have no source to rebuild from.
    """
    payloads: list[dict] = []
    missing: list[str] = []
    funds: list[str] = []
    with neo4j.session() as session:
        for start in range(0, len(gmr_ids), batch):
            chunk = gmr_ids[start:start + batch]
            by_id = _lookup(session, chunk)
            for r in by_id.values():
                if _is_fund(r):
                    funds.append(r["gmr_id"])
                    continue
                props = r.get("props") or {}
                if not props.get("name"):
                    # A nameless node rebuilds a subject with no label,
                    # which is not a repair -- report it instead.
                    missing.append(r["gmr_id"])
                    continue
                payloads.append(_payload(r["gmr_id"], props))
            missing.extend(i for i in chunk if i not in by_id)
    return payloads, missing, funds


def _labelless_payload(row: dict | None, gid: str,
                       subjects: set[str]) -> tuple[dict | None, str | None]:
    """(payload, None) for a rebuildable entity, else (None, reason)."""
    if row is None:
        return None, MISSING
    props = row.get("props") or {}
    if not props.get("name"):
        return None, NAMELESS
    payload = _payload(gid, props)
    if _is_fund(row):
        payload["identity"]["entity_kind"] = "FUND"
    elif "Company" not in subjects:
        if not props.get("entity_kind"):
            return None, UNKINDED_TWIN
        payload["identity"]["entity_kind"] = props["entity_kind"]
    return payload, None


def load_for_labelless(
    neo4j: Neo4jClient, refs: list[tuple[str, str]], batch: int = 500,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Return (payloads, skipped ids by reason) for label-less subjects.

    See the module docstring for when a payload carries entity_kind.
    """
    subjects: dict[str, set[str]] = {}
    for label, gid in refs:
        subjects.setdefault(gid, set()).add(label)
    ids = list(subjects)
    payloads: list[dict] = []
    skipped: dict[str, list[str]] = {MISSING: [], NAMELESS: [], UNKINDED_TWIN: []}
    with neo4j.session() as session:
        for start in range(0, len(ids), batch):
            by_id = _lookup(session, ids[start:start + batch])
            for gid in ids[start:start + batch]:
                payload, reason = _labelless_payload(by_id.get(gid), gid, subjects[gid])
                if payload is None:
                    skipped[reason].append(gid)
                else:
                    payloads.append(payload)
    return payloads, skipped


#: Every predicate an UpsertCompany rebuild writes -- the virtuoso-sink's
#: COMPANY_FIELD_PREDICATES -- plus rdf:type. Mirrored, not imported: the
#: sink is a separate service. A field it gains later is missing here,
#: which only makes would_lose more cautious.
_REWRITTEN = frozenset({
    _RDF_TYPE, _RDFS_LABEL, _WDT_P17,
    *(f"{_FONTEM}{local}" for local in (
        "lei", "vat", "cik", "legalForm", "postalCode", "entityKind",
        "registeredAs", "registeredAt", "jurisdiction", "registrationStatus",
        "entityCreationDate", "address", "city", "region", "hqAddress",
        "hqCity", "hqRegion", "hqPostalCode", "hqCountry", "active", "alias",
    )),
})


def would_lose(virtuoso: VirtuosoClient, gmr_ids: list[str],
               chunk: int = _HOLD_CHUNK) -> set[str]:
    """The gmr_ids whose Company or InvestmentFund subject carries a
    predicate an entity_kind rebuild would delete without writing back.

    Checked for every payload that carries entity_kind. The sink answers
    one with an UNFILTERED delete of the InvestmentFund subject and a
    whole-subject replace of the Company subject that spares only
    owl:sameAs. That is safe for the fields the rebuild rewrites and
    destructive for anything else a subject holds: fontem:subsidiaryOf
    edges are deleted, and an owl:sameAs on the Company subject is left
    alone on an otherwise empty subject -- the 2026-09-03 signature.
    """
    found: set[str] = set()
    for start in range(0, len(gmr_ids), chunk):
        values = " ".join(
            f"<{_ID_PREFIX}{label}/{gid}>"
            for gid in gmr_ids[start:start + chunk] for label in _LOOKUP_LABELS
        )
        rows = virtuoso.query(
            f"SELECT DISTINCT ?s ?p WHERE {{ GRAPH <{_G_COMPANY}> {{ "
            f"VALUES ?s {{ {values} }} ?s ?p ?o }} }}"
        )
        found.update(
            ref[1] for r in rows
            if r.get("p") not in _REWRITTEN
            and (ref := _subject_ref(r.get("s") or ""))
        )
    return found


class RateLimit:
    """Hold emission at or under ``max_rate`` events/s over the whole run."""

    def __init__(self, max_rate: float,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._rate = max_rate
        self._clock = clock
        self._sleep = sleep
        self._start = clock()
        self._sent = 0

    def consumed(self, n: int) -> None:
        self._sent += n
        ahead = self._sent / self._rate - (self._clock() - self._start)
        if ahead > 0:
            self._sleep(ahead)


def emit_rebuilds(log: EventLog, payloads: list[dict], batch: int = 500,
                  rate: RateLimit | None = None) -> int:
    """Emit one UpsertCompany per repairable subject, at the Company IRI
    -- where load_gleif emits every company, funds included."""
    sent = 0
    for start in range(0, len(payloads), batch):
        chunk = payloads[start:start + batch]
        with log.batch(uuid.uuid4(),
                       producer="rebuild_stripped_companies") as emit:
            for p in chunk:
                emit.upsert(
                    "UpsertCompany",
                    iri=f"{_COMPANY_PREFIX}{p['gmr_id']}",
                    domain="company",
                    payload=builders.upsert_company(**p),
                )
                sent += 1
        if rate is not None:
            rate.consumed(len(chunk))
        logger.info("emitted %d/%d UpsertCompany events", sent, len(payloads))
    return sent


def _rebuild_page(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    virtuoso: VirtuosoClient, neo4j: Neo4jClient, log: EventLog | None,
    refs: list[tuple[str, str]], batch: int, rate: RateLimit | None,
) -> tuple[int, int, dict[str, list[str]]]:
    """Rebuild one page. Returns (rebuilt, funds, skipped ids by reason);
    without a log nothing is emitted and ``rebuilt`` is what would be."""
    payloads, skipped = load_for_labelless(neo4j, refs, batch)
    held = would_lose(virtuoso, [
        p["gmr_id"] for p in payloads if p["identity"].get("entity_kind")
    ])
    payloads = [p for p in payloads if p["gmr_id"] not in held]
    skipped[HELD_WOULD_LOSE] = sorted(held)
    funds = sum(p["identity"].get("entity_kind") == "FUND" for p in payloads)
    rebuilt = (emit_rebuilds(log, payloads, batch, rate)
               if log is not None else len(payloads))
    return rebuilt, funds, skipped


class _Tally:
    """Running totals, and up to 20 example ids per reason not rebuilt."""

    def __init__(self) -> None:
        self.totals = dict.fromkeys(("selected", "rebuilt", "funds",
                                     *_SKIP_REASONS), 0)
        self._samples: dict[str, list[str]] = {k: [] for k in _SKIP_REASONS}

    def add(self, rebuilt: int, funds: int,
            skipped: dict[str, list[str]]) -> None:
        self.totals["rebuilt"] += rebuilt
        self.totals["funds"] += funds
        for reason, ids in skipped.items():
            self.totals[reason] += len(ids)
            room = 20 - len(self._samples[reason])
            self._samples[reason].extend(ids[:max(room, 0)])

    def log_samples(self) -> None:
        for reason, ids in self._samples.items():
            for gid in ids:
                logger.warning("not rebuilt (%s): %s", reason, gid)


def run_labelless(  # pylint: disable=too-many-arguments
    virtuoso: VirtuosoClient, neo4j: Neo4jClient, log: EventLog | None, *,
    page: int = _PAGE, batch: int = 500, limit: int | None = None,
    start_after: str = "", rate: RateLimit | None = None,
) -> dict[str, int]:
    """Select, look up and -- given a log -- emit, one page at a time."""
    tally = _Tally()
    t0 = time.monotonic()
    for n, (last, refs) in enumerate(
            iter_labelless(virtuoso, page, start_after=start_after), 1):
        if limit is not None:
            refs = refs[:max(limit - tally.totals["selected"], 0)]
            if not refs:
                break
        tally.totals["selected"] += len(refs)
        tally.add(*_rebuild_page(virtuoso, neo4j, log, refs, batch, rate))
        logger.info(
            "page %d, last key %s: %d selected; totals %s (%.1f rebuilt/s)",
            n, last, len(refs), tally.totals,
            tally.totals["rebuilt"] / max(time.monotonic() - t0, 1e-9),
        )
    tally.log_samples()
    return tally.totals


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Emit the events. Without this the script only reports.",
    )
    parser.add_argument("--select", choices=("stripped", "labelless"),
                        default="stripped")
    parser.add_argument("--page-size", type=int, default=_PAGE)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="labelless: stop after this many subjects (a sampled dry run).",
    )
    parser.add_argument(
        "--start-after", default="",
        help="labelless: resume past this subject IRI (a logged last key).",
    )
    parser.add_argument(
        "--max-rate", type=float, default=_DEFAULT_MAX_RATE,
        help="labelless: events/s ceiling with --apply; 0 disables it.",
    )
    return parser


def _main_labelless(args, virtuoso: VirtuosoClient, neo4j: Neo4jClient) -> None:
    log = EventLog.from_env() if args.apply else None
    rate = (RateLimit(args.max_rate)
            if log is not None and args.max_rate > 0 else None)
    totals = run_labelless(
        virtuoso, neo4j, log, page=args.page_size, batch=args.batch,
        limit=args.limit, start_after=args.start_after, rate=rate,
    )
    logger.info("%s: %s", "emitted" if log is not None else "dry run", totals)


def _main_stripped(args, virtuoso: VirtuosoClient, neo4j: Neo4jClient) -> None:
    stripped = find_stripped(virtuoso, args.page_size)
    logger.info("%d stripped company subjects in %s", len(stripped), _G_COMPANY)

    payloads, missing, funds = load_from_neo4j(neo4j, stripped, args.batch)
    logger.info(
        "%d rebuildable, %d with no usable source in Neo4j, %d funds skipped",
        len(payloads), len(missing), len(funds),
    )
    for gid in missing[:20]:
        logger.warning("no source to rebuild from: %s", gid)
    for gid in funds[:20]:
        logger.warning("fund, skipped (renders at the InvestmentFund "
                       "subject): %s", gid)

    if not args.apply:
        logger.info("dry run: would emit %d UpsertCompany events",
                    len(payloads))
        return
    log = EventLog.from_env()
    sent = emit_rebuilds(log, payloads, args.batch)
    logger.info("emitted %d UpsertCompany events", sent)


def main(argv=None) -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _parser().parse_args(argv)
    virtuoso = VirtuosoClient.from_env()
    # Reads NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD from the
    # environment, the same way every other ETL constructs it.
    neo4j = Neo4jClient()
    if args.select == "labelless":
        _main_labelless(args, virtuoso, neo4j)
    else:
        _main_stripped(args, virtuoso, neo4j)


if __name__ == "__main__":
    main()
