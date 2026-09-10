"""Purge the Notice subjects a mis-routed contract value rollup created.

A ``collapse_modifications`` value rollup carries ``contract_key``, and
until fontem-virtuoso-sink#130 that routed it through
``contract_notice_subject()`` to ``.../id/Notice/<ted_notice_id>``. No
full ``UpsertContract`` in the log carries ``contract_key`` -- all
844,029 of them are pre-native -- so every notice's real triples live at
``.../id/Contract/<ted_notice_id>`` and no Notice subject is ever
created by anything else.

The rollup therefore landed alone on a subject of its own, carrying
nothing but ``fontem:isCurrent`` (and ``fontem:currentValue`` when the
collapse produced a figure). Observed on shared 2026-09-10: 27,142 such
subjects, 27,141 of which have a ``Contract/<same-id>`` twin holding the
notice's actual triples.

Because the read path looks for ``isCurrent`` on the contract subject
and never found one, ``_CANONICAL`` fell through to
``notice_type != 'can-modif'`` and hid every contract whose only notice
is a modification -- 21,102 of them.

The sink fix redirects new rollups to the real subject, but it cannot
remove what the old code already wrote: nothing addresses these Notice
subjects any more, so no upsert refreshes them and no ``Delete*`` names
them. ``PurgeSubject`` passes an IRI through byte-for-byte and is the
only event that can.

Safety
------
Dry-run by default; ``--apply`` is required to emit anything. A subject
is a purge candidate ONLY when every predicate on it is a rollup
predicate -- if the old code ever wrote a real notice there, that
subject holds the only copy of those triples and must not be removed. A
candidate with no live ``Contract`` twin is reported and SKIPPED unless
``--allow-orphans``, so the script never deletes the last representation
of a notice.

Run this AFTER the sink carrying #130 is deployed, and rewind the
``virtuoso_sink`` offset to just before the rollup block afterwards so
the values land on the real subjects.

Usage::

    python -m src.etl.purge_orphan_rollup_subjects            # report
    python -m src.etl.purge_orphan_rollup_subjects --apply    # emit
"""

from __future__ import annotations

import argparse
import logging
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog

from src.data.sparql.virtuoso_client import VirtuosoClient

logger = logging.getLogger(__name__)

_DEFAULT_GRAPH = "http://data.fontem.eu/graph/contract"

_FONTEM = "http://data.fontem.eu/ontology#"
_NOTICE_PREFIX = "http://data.fontem.eu/id/Notice/"
_CONTRACT_PREFIX = "http://data.fontem.eu/id/Contract/"

# The only predicates a value rollup renders. A candidate subject must
# carry nothing outside this set -- anything else means a real event
# rendered there and the subject is not disposable.
_ROLLUP_PREDICATES = (f"{_FONTEM}isCurrent", f"{_FONTEM}currentValue")

# Virtuoso's ResultSetMaxRows, from virtuoso.ini on shared and prod. A
# result at or above this is assumed truncated, because the server does
# not say which it is.
_RESULT_SET_MAX_ROWS = 50_000
_PAGE = 10_000

# Twin lookups go out as VALUES blocks on a GET query string, and
# Virtuoso answers 400 Bad Request once that URL gets long rather than
# saying the request was too large. 1,000 IRIs per chunk is ~64KB of
# URL and is refused; 100 is ~6.5KB and is not.
_TWIN_CHUNK = 100

_REASON = (
    "orphan Notice subject created by a mis-routed contract value rollup "
    "(fontem-virtuoso-sink#130); the notice's triples live at {live}"
)


def _twin(subject_iri: str) -> str:
    """The Contract subject holding the notice's real triples."""
    return _CONTRACT_PREFIX + subject_iri[len(_NOTICE_PREFIX):]


def find_candidates(virtuoso: VirtuosoClient, graph_iri: str,
                    page: int = _PAGE,
                    cap: int = _RESULT_SET_MAX_ROWS) -> list[str]:
    """Every Notice subject carrying only rollup predicates.

    Keyset-paginated for the same two reasons purge_stranded_subjects is:
    ResultSetMaxRows truncates a large result silently (HTTP 200, just
    fewer rows), and MaxSortedTopRows makes OFFSET paging past row 10,000
    a hard SR353 error rather than a truncation.
    """
    if page >= cap:
        raise ValueError(
            f"page size {page} is not below Virtuoso's result-set cap "
            f"({cap}); a full page could not be told from a truncated one"
        )
    not_rollup = ", ".join(f"<{p}>" for p in _ROLLUP_PREDICATES)
    found: set[str] = set()
    last = ""
    while True:
        rows = virtuoso.query(
            f"SELECT DISTINCT ?s WHERE {{ GRAPH <{graph_iri}> {{ "
            f"?s <{_FONTEM}isCurrent> ?v . "
            f"FILTER NOT EXISTS {{ ?s ?p ?o . "
            f"FILTER(?p NOT IN ({not_rollup})) }} }} "
            f'FILTER(STRSTARTS(STR(?s), "{_NOTICE_PREFIX}")) '
            f'FILTER(STR(?s) > "{last}") }} ORDER BY ?s LIMIT {page}'
        )
        if len(rows) >= cap:
            raise RuntimeError(
                f"page of {len(rows)} rows hit Virtuoso's result-set cap; "
                "results may be silently truncated. Lower --page-size."
            )
        got = [r["s"] for r in rows if r.get("s")]
        if not got:
            return sorted(found)
        found.update(got)
        nxt = max(got)
        if nxt <= last:
            raise RuntimeError(
                f"keyset did not advance past {last!r}; refusing to loop"
            )
        last = nxt
        if len(rows) < page:
            return sorted(found)


def partition_by_twin(virtuoso: VirtuosoClient, graph_iri: str,
                      candidates: list[str],
                      chunk_size: int = _TWIN_CHUNK) -> tuple[list[str], list[str]]:
    """Split candidates into (has_contract_twin, no_twin).

    Asked as one VALUES query per chunk rather than a self-join over the
    whole graph: the join is quadratic across 122K subjects and
    Virtuoso's cost estimator refuses it. Chunked small because the
    query rides in a GET URL -- see _TWIN_CHUNK.
    """
    with_twin, without = [], []
    for start in range(0, len(candidates), chunk_size):
        chunk = candidates[start:start + chunk_size]
        values = " ".join(f"<{_twin(s)}>" for s in chunk)
        rows = virtuoso.query(
            f"SELECT DISTINCT ?t WHERE {{ GRAPH <{graph_iri}> {{ "
            f"?t ?p ?o }} VALUES ?t {{ {values} }} }}"
        )
        live = {r["t"] for r in rows if r.get("t")}
        for s in chunk:
            (with_twin if _twin(s) in live else without).append(s)
    return with_twin, without


def emit_purges(log: EventLog, graph_iri: str, subjects: list[str],
                batch: int = 500) -> int:
    """Emit one PurgeSubject per orphan subject."""
    sent = 0
    for start in range(0, len(subjects), batch):
        chunk = subjects[start:start + batch]
        with log.batch(uuid.uuid4(),
                       producer="purge_orphan_rollup_subjects") as emit:
            for subject in chunk:
                emit.control("PurgeSubject", builders.purge_subject(
                    graph_iri=graph_iri,
                    subject_iri=subject,
                    reason=_REASON.format(live=_twin(subject)),
                    # These IRIs are ordinary -- percent-encoding
                    # produces them unchanged -- so the sink cannot tell
                    # one from a live subject by its IRI. Declaring the
                    # predicates lets it check the store and refuse if
                    # the subject holds anything this script did not
                    # account for. Without it the purge is refused
                    # outright (fontem-virtuoso-sink#131).
                    only_predicates=list(_ROLLUP_PREDICATES),
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
        help="Also purge candidates with NO live Contract twin.",
    )
    parser.add_argument("--page-size", type=int, default=_PAGE)
    args = parser.parse_args(argv)

    virtuoso = VirtuosoClient.from_env()
    candidates = find_candidates(virtuoso, args.graph, args.page_size)
    with_twin, without = partition_by_twin(virtuoso, args.graph, candidates)
    logger.info(
        "%d orphan rollup subjects in %s: %d with a Contract twin, "
        "%d without", len(candidates), args.graph, len(with_twin),
        len(without),
    )
    for subject in without[:20]:
        logger.warning("no Contract twin, skipping: %s", subject)

    targets = with_twin + without if args.allow_orphans else with_twin
    if not args.apply:
        logger.info("dry run: would emit %d PurgeSubject events", len(targets))
        return
    log = EventLog.from_env()
    sent = emit_purges(log, args.graph, targets)
    logger.info("emitted %d PurgeSubject events", sent)


if __name__ == "__main__":
    main()
