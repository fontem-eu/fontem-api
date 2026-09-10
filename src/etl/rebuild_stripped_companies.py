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

#: A damaged subject carries owl:sameAs and almost nothing else. Three is
#: the observed ceiling (the partials picked up a region or a label from a
#: later scoped write); a healthy company carries 7 or more and, crucially,
#: carries rdf:type -- which the strip always removed.
_MAX_PREDICATES = 3

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

    The predicate-count filter is what makes this precise. Selecting on
    owl:sameAs alone would be right today -- every sameAs subject in
    graph/company is damaged -- but that is a property of the incident,
    not an invariant, and a healthy company that legitimately gains an
    owl:sameAs later must not be rewritten by a re-run.
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
SELECT ?s (COUNT(DISTINCT ?p) AS ?np) WHERE {{
  GRAPH <{_G_COMPANY}> {{
    ?s <{_OWL_SAME_AS}> ?o .
    ?s ?p ?o2 .
  }}
  FILTER(STR(?s) > "{last}")
}}
GROUP BY ?s HAVING (COUNT(DISTINCT ?p) <= {_MAX_PREDICATES})
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
            rows = session.run(
                "MATCH (c) WHERE c.gmr_id IN $ids "
                "RETURN c.gmr_id AS gmr_id, labels(c) AS labels, "
                "  c.name AS name, c.country AS country, c.lei AS lei, "
                "  c.vat AS vat, c.cik AS cik, c.active AS active, "
                "  c.entity_kind AS entity_kind, "
                "  c.legal_form AS legal_form, c.postal_code AS postal_code",
                ids=chunk,
            ).data()
            seen = set()
            for r in rows:
                seen.add(r["gmr_id"])
                if ("InvestmentFund" in (r.get("labels") or [])
                        or r.get("entity_kind") == "FUND"):
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
