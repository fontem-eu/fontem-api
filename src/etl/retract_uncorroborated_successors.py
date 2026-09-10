"""Retract successor_lei_match merges that rest on nothing discriminating.

Why these exist
---------------
`successor_lei_match` recognises "same entity re-registered with a new
LEI": one record active, one retired, same normalised name and country,
plus a corroborating attribute. For a window in September 2026 the
corroborator list included ``legal_form``, which is a CATEGORY rather
than an identifier — OV32 is the ELF code for an Italian S.R.L. and
157,598 Italian companies carry it. "Both are an S.R.L." therefore
corroborated every pair of same-named Italian companies in the country.

Observed on shared 2026-09-09: 46 distinct "FUTURA S.R.L." records
across Reggio Emilia, Ancona and Pisa (postal 42015 / 60030 / 56029 /
56038, all different LEIs) auto-merged into one entity at confidence
0.98, with 41 "ALBA S.R.L." beside them. 7,873 of 12,148 successor
edges rested on legal_form alone.

fontem-consolidator#231 removed legal_form from the corroborator set,
so no new ones are produced. This withdraws the ones already asserted.

What counts as corroborated
---------------------------
Exactly what the fixed rule accepts: an agreeing postal code (compared
with whitespace and case normalised, because "821 09" and "82109" are
the same Slovak code), or an agreeing hard identifier — vat,
registered_as, cik. A pair with any of those is left alone.

What a retraction does
----------------------
RetractSameAs removes the owl:sameAs from Virtuoso in both directions
AND writes a :NOT_SAME_AS in Neo4j, which stops the rules re-proposing
the pair. That is stronger than what we know — we know the assertion
lacked evidence, not that the two are definitely different companies —
but leaving a wrong owl:sameAs in place is worse, and the reason text
records exactly what happened so an operator can revisit a pair.

Usage::

    python -m src.etl.retract_uncorroborated_successors            # report
    python -m src.etl.retract_uncorroborated_successors --apply    # emit
"""

from __future__ import annotations

import argparse
import logging
import os
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

_ID = "http://data.fontem.eu/id"

_REASON = (
    "asserted by successor_lei_match while legal_form was accepted as "
    "corroboration; legal_form is a category, not an identifier "
    "(fontem-consolidator#231). Nothing discriminating agreed: postal "
    "codes {a_post} vs {b_post}, no matching vat/registered_as/cik."
)

#: Directed successor edges whose only support was the shared legal
#: form. Mirrors the fixed rule's corroborator set exactly, so this can
#: never retract a pair the rule would still assert today.
_FIND = """
MATCH (a:Company)-[r:SAME_AS_CANDIDATE {status:'approved'}]->(b:Company)
WHERE r.method = 'successor_lei_match'
   OR 'successor_lei_match' IN r.detection_rules
WITH a, b,
  (a.postal_code IS NOT NULL AND b.postal_code IS NOT NULL
   AND replace(toUpper(a.postal_code),' ','')
     = replace(toUpper(b.postal_code),' ','')) AS postal_ok,
  (a.vat IS NOT NULL AND b.vat IS NOT NULL AND a.vat = b.vat) AS vat_ok,
  (a.registered_as IS NOT NULL AND b.registered_as IS NOT NULL
   AND a.registered_as = b.registered_as) AS reg_ok,
  (a.cik IS NOT NULL AND b.cik IS NOT NULL AND a.cik = b.cik) AS cik_ok
WHERE NOT (postal_ok OR vat_ok OR reg_ok OR cik_ok)
RETURN a.gmr_id AS a_id, b.gmr_id AS b_id,
       a.name AS a_name, b.name AS b_name,
       a.postal_code AS a_post, b.postal_code AS b_post
"""


def find_uncorroborated(driver) -> list[dict]:
    """Every asserted successor pair with no discriminating agreement."""
    with driver.session() as session:
        return session.run(_FIND).data()


def emit_retractions(log: EventLog, pairs: list[dict], batch: int = 500) -> int:
    sent = 0
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        with log.batch(uuid.uuid4(),
                       producer="retract_uncorroborated_successors") as emit:
            for p in chunk:
                emit.upsert(
                    "RetractSameAs",
                    iri=f"{_ID}/Company/{p['a_id']}",
                    domain="company",
                    payload=builders.retract_same_as(
                        a_iri=f"{_ID}/Company/{p['a_id']}",
                        b_iri=f"{_ID}/Company/{p['b_id']}",
                        reason=_REASON.format(
                            a_post=p.get("a_post") or "none",
                            b_post=p.get("b_post") or "none",
                        ),
                        reviewer="retract_uncorroborated_successors",
                        retracted_method="successor_lei_match",
                    ),
                )
                sent += 1
        logger.info("emitted %d/%d RetractSameAs", sent, len(pairs))
    return sent


def main(argv=None) -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Emit the events. Without this, report only.")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              os.environ["NEO4J_PASSWORD"]),
    )
    try:
        pairs = find_uncorroborated(driver)
    finally:
        driver.close()

    logger.info("uncorroborated successor assertions: %d", len(pairs))
    for p in pairs[:10]:
        logger.info("  %s (%s) == %s (%s)",
                    p["a_name"], p.get("a_post"), p["b_name"], p.get("b_post"))
    if args.limit:
        pairs = pairs[:args.limit]

    if not args.apply:
        logger.info("dry run: would emit %d RetractSameAs (use --apply)",
                    len(pairs))
        return
    sent = emit_retractions(EventLog.from_env(), pairs)
    logger.info("emitted %d RetractSameAs events", sent)


if __name__ == "__main__":
    main()
