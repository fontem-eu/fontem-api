"""Purge subjects stranded by the 2026-06-07 IRI percent-encoding fix.

The Virtuoso sink builds a subject IRI from the entity's natural key.
Commit 37af28e (fontem-virtuoso-sink, 2026-06-07) made it percent-encode
non-ASCII characters, because Virtuoso's SPARQL parser choked on raw
Unicode in an IRI. That changed the entity's identity:

    http://data.fontem.eu/id/Listing/BJÖRN.ST        (before)
    http://data.fontem.eu/id/Listing/BJ%C3%96RN.ST   (after)

Virtuoso treats those as two distinct subjects and does no
normalisation. The sink's upsert is "delete this subject, insert this
subject", so after the fix it maintains only the encoded one and the
old subject is stranded — stale triples that no load will ever refresh
and no delete will ever remove.

They cannot be removed by a Delete* event either: quote() is idempotent
on the encoded form and no input maps back to the raw form, so a
Delete* naming the raw IRI encodes to the LIVE subject and deletes that
instead. PurgeSubject is the only event that can name them, and it
passes the IRI through byte-for-byte.

Observed on shared 2026-09-07: 3,166 stranded subjects, all in
graph/listing — tickers are the only non-ASCII natural key in the
model, everything else is UUID-keyed, which a scan of every fontem
graph confirms. Each contradicts its live twin: BJÖRN.ST says active
with exchange ST and currency SEK, BJ%C3%96RN.ST says inactive. Both
still carry listingOf to the same company, so that company's listings
render twice with conflicting facts.

Safety
------
Dry-run by default; --apply is required to emit anything. A stranded
subject with no live twin is reported and SKIPPED unless --allow-orphans
is given: purging it would remove the only copy of that data rather
than a duplicate. On shared all 3,166 have a twin, so nothing is
skipped there, but the check is what makes the script safe to point at
prod without re-deriving the analysis.

Usage::

    python -m src.etl.purge_stranded_subjects                  # report
    python -m src.etl.purge_stranded_subjects --apply          # emit
    python -m src.etl.purge_stranded_subjects --graph <iri>    # scope
"""

from __future__ import annotations

import argparse
import logging
import uuid
from urllib.parse import quote

from fontem_event_schemas import builders
from fontem_events import EventLog

from src.data.sparql.virtuoso_client import VirtuosoClient

logger = logging.getLogger(__name__)

# Must match _IRI_SAFE in fontem-virtuoso-sink's sink.py. If the two
# ever disagree this script would classify subjects by a rule the sink
# no longer writes by, and "stranded" would stop meaning "unreachable".
_IRI_SAFE = "%:/?#[]@!$&'()*+,;=._-~"

_DEFAULT_GRAPH = "http://data.fontem.eu/graph/listing"

_REASON = (
    "stranded by the 2026-06-07 IRI percent-encoding fix "
    "(fontem-virtuoso-sink 37af28e); live subject is {live}"
)


def is_stranded(subject_iri: str) -> bool:
    """True when the sink can no longer address this subject.

    The sink writes every subject through quote(); if that changes the
    IRI, nothing the sink writes today can ever land on this subject
    again, and no Delete* event can name it.
    """
    return quote(subject_iri, safe=_IRI_SAFE) != subject_iri


def find_stranded(virtuoso: VirtuosoClient, graph_iri: str) -> tuple[list[str], list[str]]:
    """Return (stranded_with_live_twin, stranded_without_twin).

    Classified client-side from one flat subject list rather than with a
    self-join in SPARQL: the join is quadratic over a graph with 124K
    subjects and Virtuoso's cost estimator refuses it, while the flat
    scan is one cheap query.
    """
    rows = virtuoso.query(
        f"SELECT DISTINCT ?s WHERE {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} }}"
    )
    subjects = {r["s"] for r in rows if r.get("s")}
    stranded = sorted(s for s in subjects if is_stranded(s))
    with_twin, without_twin = [], []
    for s in stranded:
        (with_twin if quote(s, safe=_IRI_SAFE) in subjects
         else without_twin).append(s)
    return with_twin, without_twin


def emit_purges(
    log: EventLog, graph_iri: str, subjects: list[str], batch: int = 500,
) -> int:
    """Emit one PurgeSubject per stranded subject."""
    sent = 0
    for start in range(0, len(subjects), batch):
        chunk = subjects[start:start + batch]
        with log.batch(uuid.uuid4(), producer="purge_stranded_subjects") as emit:
            for subject in chunk:
                emit.control("PurgeSubject", builders.purge_subject(
                    graph_iri=graph_iri,
                    subject_iri=subject,
                    reason=_REASON.format(live=quote(subject, safe=_IRI_SAFE)),
                ))
                sent += 1
        logger.info("emitted %d/%d PurgeSubject events", sent, len(subjects))
    return sent


def main(argv=None) -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default=_DEFAULT_GRAPH)
    parser.add_argument(
        "--apply", action="store_true",
        help="Emit the events. Without this the script only reports.",
    )
    parser.add_argument(
        "--allow-orphans", action="store_true",
        help="Also purge stranded subjects that have NO live twin. "
             "That removes the only copy of the data, not a duplicate.",
    )
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    virtuoso = VirtuosoClient.from_env()
    with_twin, without_twin = find_stranded(virtuoso, args.graph)

    logger.info("graph %s", args.graph)
    logger.info("  stranded with a live twin : %d", len(with_twin))
    logger.info("  stranded with NO twin     : %d", len(without_twin))
    for s in without_twin[:20]:
        logger.warning("  no live twin, skipping: %s", s)

    targets = list(with_twin)
    if args.allow_orphans:
        targets += without_twin
    if args.limit:
        targets = targets[:args.limit]

    if not args.apply:
        logger.info(
            "dry run: would emit %d PurgeSubject events (use --apply)",
            len(targets),
        )
        return

    log = EventLog.from_env()
    sent = emit_purges(log, args.graph, targets)
    logger.info("emitted %d PurgeSubject events", sent)


if __name__ == "__main__":
    main()
