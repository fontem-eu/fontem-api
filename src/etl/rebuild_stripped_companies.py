"""Rebuild the company subjects an AssertSameAs wiped out of Virtuoso.

On 2026-09-03 the prod consolidator had event emission on while the
Virtuoso sink still treated AssertSameAs as an ordinary upsert. Ordinary
upserts are whole-subject replaces -- ``DELETE WHERE { <s> ?p ?o }`` --
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

How the repair works
--------------------
Re-emit ``UpsertCompany``. The renderer produces the full subject and
the sink's replace is whole-subject, so the record comes back complete.
Crucially ``owl:sameAs`` SURVIVES: it is in ``_PRESERVED_ON_REPLACE``
and the delete clause exempts it, so the repair does not undo the
identity assertion that caused the damage.

Neo4j is the source, not the event log. Re-emitting each subject's last
logged payload would faithfully restore the *pre-incident* state, which
for most of these was already thinner than what Neo4j holds today.
Verified on prod: of the fully stripped ids, all but 123 exist as
:Company with name and country populated.

Two hazards this deliberately avoids
------------------------------------
An ``UpsertCompany`` carrying ``entity_kind`` makes the sink issue an
extra, UNFILTERED delete of the sibling ``InvestmentFund`` subject --
unfiltered meaning it does not exempt owl:sameAs. And when
``entity_kind`` is ``FUND`` the renderer writes to the InvestmentFund
subject entirely, so the stripped ``Company/<id>`` subject would be left
exactly as stripped. Prod holds 245,585 funds, so neither is
hypothetical. This script therefore SKIPS any entity Neo4j reports as a
fund and never sends ``entity_kind``; those are reported for separate
handling rather than silently mangled.

Usage::

    python -m src.etl.rebuild_stripped_companies             # report
    python -m src.etl.rebuild_stripped_companies --apply     # emit
"""

from __future__ import annotations

import argparse
import logging
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog

from src.data.graph.neo4j_client import Neo4jClient
from src.data.sparql.virtuoso_client import VirtuosoClient

logger = logging.getLogger(__name__)

_G_COMPANY = "http://data.fontem.eu/graph/company"
_OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
_COMPANY_PREFIX = "http://data.fontem.eu/id/Company/"

#: Virtuoso's ResultSetMaxRows. A page at or above this is assumed
#: truncated, because the server does not say which it is.
_RESULT_SET_MAX_ROWS = 50_000
_PAGE = 5_000

#: Fields copied from the Neo4j node into the event payload. entity_kind
#: is deliberately absent -- see the hazards in the module docstring.
_FIELDS = ("name", "country", "lei", "vat", "cik", "active",
           "legal_form", "postal_code")


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
    if page >= cap:
        raise ValueError(
            f"page size {page} is not below Virtuoso's result-set cap "
            f"({cap}); a full page could not be told from a truncated one"
        )
    found: list[str] = []
    last = ""
    while True:
        rows = virtuoso.query(f"""
SELECT ?s WHERE {{
  GRAPH <{_G_COMPANY}> {{
    ?s <{_OWL_SAME_AS}> ?o .
    FILTER NOT EXISTS {{ ?s a ?t }}
  }}
  FILTER(STR(?s) > "{last}")
}}
ORDER BY ?s LIMIT {page}
""")
        if len(rows) >= cap:
            raise RuntimeError(
                f"page of {len(rows)} rows hit Virtuoso's result-set cap; "
                "results may be silently truncated. Lower --page-size."
            )
        got = [r["s"] for r in rows if r.get("s")]
        if not got:
            return found
        found.extend(s[len(_COMPANY_PREFIX):] for s in got
                     if s.startswith(_COMPANY_PREFIX))
        nxt = max(got)
        if nxt <= last:
            raise RuntimeError(
                f"keyset did not advance past {last!r}; refusing to loop"
            )
        last = nxt
        if len(rows) < page:
            return found


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
            or row.get("entity_kind") == "FUND")


def _lookup_query(label: str) -> str:
    return (
        f"MATCH (c:{label}) WHERE c.gmr_id IN $ids "
        "RETURN c.gmr_id AS gmr_id, labels(c) AS labels, "
        "  c.name AS name, c.country AS country, c.lei AS lei, "
        "  c.vat AS vat, c.cik AS cik, c.active AS active, "
        "  c.entity_kind AS entity_kind, "
        "  c.legal_form AS legal_form, c.postal_code AS postal_code"
    )


def load_from_neo4j(neo4j: Neo4jClient, gmr_ids: list[str],
                    batch: int = 500) -> tuple[list[dict], list[str], list[str]]:
    """Return (payloads, missing_ids, fund_ids).

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
            # One row per gmr_id. Querying per label means a node that
            # carries BOTH labels comes back twice; fund classification
            # wins, because repairing a fund as a Company is the silent
            # no-op the module docstring warns about.
            by_id: dict[str, dict] = {}
            for label in _LOOKUP_LABELS:
                for r in session.run(_lookup_query(label), ids=chunk).data():
                    prev = by_id.get(r["gmr_id"])
                    if prev is None or _is_fund(r):
                        by_id[r["gmr_id"]] = r
            seen = set(by_id)
            for r in by_id.values():
                if _is_fund(r):
                    funds.append(r["gmr_id"])
                    continue
                if not r.get("name"):
                    # A nameless node rebuilds a subject with no label,
                    # which is not a repair -- report it instead.
                    missing.append(r["gmr_id"])
                    continue
                payloads.append({
                    "gmr_id": r["gmr_id"],
                    **{f: r.get(f) for f in _FIELDS},
                })
            missing.extend(i for i in chunk if i not in seen)
    return payloads, missing, funds


def emit_rebuilds(log: EventLog, payloads: list[dict],
                  batch: int = 500) -> int:
    """Emit one UpsertCompany per repairable subject."""
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
        logger.info("emitted %d/%d UpsertCompany events", sent, len(payloads))
    return sent


def main(argv=None) -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Emit the events. Without this the script only reports.",
    )
    parser.add_argument("--page-size", type=int, default=_PAGE)
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args(argv)

    virtuoso = VirtuosoClient.from_env()
    # Reads NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD from the
    # environment, the same way every other ETL constructs it.
    neo4j = Neo4jClient()
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


if __name__ == "__main__":
    main()
