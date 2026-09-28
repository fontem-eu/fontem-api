"""Give the notices already stored the title language new ones now carry.

Step 1 of the title-language rollout (fontem-api #485/#490, in prod since
2026-09-27) makes every newly loaded contract and cohesion notice carry
``title_lang``. This is step 2: the existing ones, brought level WITHOUT
reprocessing. Re-emitting old notices is not safe here: a shared re-run of
April overwrote an eForms notice held at version 27 with version 25 and
regressed its contract's canonical date. Unlike ``correct_scale_errors``,
which appends corrective events for the sinks, this adds one field to the
stored events and writes the graph directly, bypassing the loader and the
sink, so nothing but the language can change.

Three stages, each resumable (``migrations.title_lang_progress``) and safe
to re-run:

``map``
    Parse each source once with the live parser: the TED monthly package
    (the package store, else TED's CDN, which also fills the store), the
    per-notice XML in ted-raw, and Kohesio's per-country CSV. Records
    notice id -> ``title_lang`` in ``migrations.title_lang_map`` (NULL when
    the notice states no recognisable language), and disclosure id ->
    English title in ``migrations.title_lang_cohesion``.
``events``
    Add ``title_lang``, and nothing else, to the ``UpsertContract`` and
    Kohesio ``UpsertDisclosure`` events that lack it, in seq batches over the
    ``(domain, seq)`` index. A Kohesio event gets "en" only when its stored
    title is Kohesio's English name for that project. ``contract_ojs`` is a
    second pass over the contract events for the legacy notices an older
    loader keyed by their OJ S reference (``2021/S 129-344226``) rather than
    the publication number (``344226-2021``) the map holds.
``graph``
    Read the events back in seq order and set only ``title_lang`` on
    ``:Notice``, on the ``:Contract`` whose canonical notice it is, and on
    the Kohesio ``:Disclosure``. Where a notice states no language, a
    ``title_lang`` a translator guessed on its contract is removed; every
    cohesion ``title_lang`` written before step 1 was such a guess, and so is
    every one on an OJ-S-keyed contract.

Usage::

    python -m src.etl.migrate_title_lang map --from 2011-01 --to 2026-08
    python -m src.etl.migrate_title_lang map --raw --cohesion
    python -m src.etl.migrate_title_lang events [--dry-run]
    python -m src.etl.migrate_title_lang events --domains contract_ojs
    python -m src.etl.migrate_title_lang graph [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import psycopg
from neo4j import GraphDatabase

from eforms.filters import awards_and_modifications
from eforms.parser import parse as parse_notice_xml
from eforms.stream import stream_notices

from src.data.ted_raw_store import TedPackageStore, TedRawStore
from src.etl.load_eu_knowledge_graph import (
    EU_COUNTRIES,
    download_country_csv,
    parse_kohesio_csv,
)
from src.etl.load_ted_contracts import _download_monthly, notice_key  # noqa: PLC2701

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS migrations;
CREATE TABLE IF NOT EXISTS migrations.title_lang_map (
    notice_id  text PRIMARY KEY,
    -- NULL: the notice states no recognisable language. The pattern is the
    -- one UpsertContract.title_lang validates against (event-schemas 0.11.0).
    title_lang text CHECK (title_lang ~ '^[a-z]{2}$'),
    source     text NOT NULL
);
CREATE TABLE IF NOT EXISTS migrations.title_lang_cohesion (
    disclosure_id text PRIMARY KEY,
    title         text NOT NULL,
    title_lang    text NOT NULL CHECK (title_lang ~ '^[a-z]{2}$')
);
CREATE TABLE IF NOT EXISTS migrations.title_lang_progress (
    stage      text PRIMARY KEY,
    cursor     text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

_UPSERT_MAP = """
INSERT INTO migrations.title_lang_map (notice_id, title_lang, source)
VALUES (%s, %s, %s)
ON CONFLICT (notice_id) DO UPDATE
   SET title_lang = EXCLUDED.title_lang, source = EXCLUDED.source
"""
_UPSERT_COHESION = """
INSERT INTO migrations.title_lang_cohesion (disclosure_id, title, title_lang)
VALUES (%s, %s, %s)
ON CONFLICT (disclosure_id) DO UPDATE
   SET title = EXCLUDED.title, title_lang = EXCLUDED.title_lang
"""

#: A legacy notice an older loader keyed by its OJ S reference
#: (``2021/S 129-344226``), and the publication number the map holds for it
#: (``344226-2021``). The rewrite only goes this way: the issue number (129)
#: is not in the publication number.
OJS_ID_SQL = r"'^\d{4}/S '"
_OJS_TO_PUBNUM_SQL = (r"regexp_replace(e.payload->>'ted_notice_id', "
                      r"'^(\d{4})/S \d+-(\d+)$', '\2-\1')")


@dataclass(frozen=True)
class EventsPass:
    """One pass of the events stage: which events, and the map they join."""

    domain: str
    event_type: str
    table: str
    join: str


#: Each pass, by the name its progress is kept under. The (domain, seq)
#: index keeps each window to that domain's rows, and
#: ``NOT (payload ? 'title_lang')`` makes a re-run, and every event loaded
#: since step 1, a no-op.
_EVENTS_PASSES = {
    "contract": EventsPass(
        "contract", "UpsertContract", "migrations.title_lang_map",
        "m.notice_id = e.payload->>'ted_notice_id' AND m.title_lang IS NOT NULL"),
    # A separate pass rather than an OR in the join, which would lose the
    # hash join on every window of the first.
    "contract_ojs": EventsPass(
        "contract", "UpsertContract", "migrations.title_lang_map",
        f"e.payload->>'ted_notice_id' ~ {OJS_ID_SQL} "
        f"AND m.notice_id = {_OJS_TO_PUBNUM_SQL} AND m.title_lang IS NOT NULL"),
    # A Kohesio title is stated English only when it is Kohesio's English name.
    "eu_cohesion": EventsPass(
        "eu_cohesion", "UpsertDisclosure", "migrations.title_lang_cohesion",
        "m.disclosure_id = e.payload->>'disclosure_id' AND m.title = e.payload->>'title'"),
}
_EVENTS_FILTER = ("e.domain = %(domain)s AND e.seq > %(lo)s AND e.seq <= %(hi)s "
                  "AND e.event_type = %(event_type)s AND NOT (e.payload ? 'title_lang')")


def events_sql(name: str, *, dry_run: bool) -> str:
    """The UPDATE for one seq window of a pass, or its COUNT for a dry run."""
    p = _EVENTS_PASSES[name]
    if dry_run:
        return (f"SELECT count(*) FROM events.entity_events AS e JOIN {p.table} AS m "
                f"ON {p.join} WHERE {_EVENTS_FILTER}")
    return ("UPDATE events.entity_events AS e "
            "SET payload = jsonb_set(e.payload, '{title_lang}', to_jsonb(m.title_lang)) "
            f"FROM {p.table} AS m WHERE {p.join} AND {_EVENTS_FILTER}")


_GRAPH_READ = {
    "contract": """
        SELECT payload->>'ted_notice_id', payload->>'title_lang'
          FROM events.entity_events
         WHERE domain = 'contract' AND seq > %(lo)s AND seq <= %(hi)s
           AND event_type = 'UpsertContract' AND payload ? 'title_lang'
         ORDER BY seq
    """,
    "eu_cohesion": """
        SELECT payload->>'disclosure_id', payload->>'title_lang'
          FROM events.entity_events
         WHERE domain = 'eu_cohesion' AND seq > %(lo)s AND seq <= %(hi)s
           AND event_type = 'UpsertDisclosure' AND payload ? 'title_lang'
         ORDER BY seq
    """,
}
_GRAPH_SET = {
    # The contract entity's ted_notice_id is its canonical notice's (the
    # sink denormalises it), so the entity takes that notice's language.
    "contract": """
        UNWIND $rows AS row
        OPTIONAL MATCH (n:Notice {ted_notice_id: row.id})
        FOREACH (_ IN CASE WHEN n IS NULL THEN [] ELSE [1] END |
                 SET n.title_lang = row.lang)
        WITH row
        OPTIONAL MATCH (c:Contract {ted_notice_id: row.id})
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
                 SET c.title_lang = row.lang)
    """,
    "eu_cohesion": """
        UNWIND $rows AS row
        MATCH (d:Disclosure {system: 'eu-cohesion', disclosure_id: row.id})
        SET d.title_lang = row.lang
    """,
}
#: A notice that states no language: whatever title_lang its contract
#: carries was written by a translator's guess.
_GRAPH_CLEAR_CONTRACTS = """
    UNWIND $ids AS id
    MATCH (c:Contract {ted_notice_id: id})
    WHERE c.title_lang IS NOT NULL
    REMOVE c.title_lang
"""
#: Before step 1 nothing stated a cohesion title's language, so every value
#: present then was a translator's guess (23 prod projects say "lt" on English
#: titles). Cleared first; the events then set what the source states.
_GRAPH_CLEAR_COHESION = """
    MATCH (d:Disclosure {system: 'eu-cohesion'})
    WHERE d.title_lang IS NOT NULL
    WITH d LIMIT 10000
    REMOVE d.title_lang
    RETURN count(d) AS cleared
"""
#: Nor did anything state one on a contract keyed by its OJ S reference: the
#: loader that keyed them so predates title_lang (15,239 in prod, 2026-09-28).
_GRAPH_CLEAR_OJS_CONTRACTS = """
    MATCH (c:Contract)
    WHERE c.title_lang IS NOT NULL AND c.ted_notice_id =~ '^[0-9]{4}/S .*'
    WITH c LIMIT 10000
    REMOVE c.title_lang
    RETURN count(c) AS cleared
"""

_LEGACY_PUBNUM = re.compile(r"^0*(\d+)-(\d{4})$")


def notice_ids(notice) -> list[str]:
    """Every id the event log may hold for this notice.

    Its key as the loader computes it, and for a legacy publication number
    both the zero-padded form the current loader writes ("000123-2020") and
    the unpadded one an older loader wrote ("123-2020").
    """
    key = notice_key(notice)
    if not key:
        return []
    m = _LEGACY_PUBNUM.match(key)
    if not m:
        return [key]
    number, year = int(m.group(1)), m.group(2)
    return list(dict.fromkeys([key, f"{number:06d}-{year}", f"{number}-{year}"]))


def latest_per_id(rows: Iterable[tuple[str, str]]) -> list[dict]:
    """Rows in seq order reduced to the last language each id was given."""
    latest: dict[str, str] = {}
    for ident, lang in rows:
        if ident and lang:
            latest[ident] = lang
    return [{"id": i, "lang": lang} for i, lang in latest.items()]


def _get_cursor(conn, stage: str) -> str | None:
    row = conn.execute("SELECT cursor FROM migrations.title_lang_progress "
                       "WHERE stage = %s", (stage,)).fetchone()
    return row[0] if row else None


def _set_cursor(conn, stage: str, cursor: str) -> None:
    conn.execute(
        "INSERT INTO migrations.title_lang_progress (stage, cursor) VALUES (%s, %s) "
        "ON CONFLICT (stage) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()",
        (stage, cursor),
    )


# ── map ─────────────────────────────────────────────────────────────


def map_notices(conn, notices: Iterable, source: str) -> int:
    """Record every notice's ids and title language; later sources win."""
    rows: list[tuple] = []
    count = 0
    with conn.cursor() as cur:
        for notice in notices:
            count += 1
            rows.extend((i, notice.title_lang, source) for i in notice_ids(notice))
            if len(rows) >= 5000:
                cur.executemany(_UPSERT_MAP, rows)
                rows.clear()
        if rows:
            cur.executemany(_UPSERT_MAP, rows)
    return count


def map_months(conn, months: Iterable[tuple[int, int]], package_store) -> int:
    """Parse each monthly package once; a finished month is skipped."""
    total = 0
    with tempfile.TemporaryDirectory() as workdir:
        for year, month in months:
            stage = f"map:ted-{year}-{month:02d}"
            if _get_cursor(conn, stage) == "done":
                continue
            archive = _download_monthly(year, month, Path(workdir),
                                        package_store=package_store)
            n = map_notices(conn, awards_and_modifications(stream_notices(archive)),
                            source=stage[len("map:"):])
            _set_cursor(conn, stage, "done")
            conn.commit()
            archive.unlink(missing_ok=True)
            logger.info("%s: %d notices mapped", stage, n)
            total += n
    return total


def map_raw(conn, raw_store: TedRawStore) -> int:
    """The per-notice XML the daily loader kept (the month without a
    package yet). Resumes after the last object recorded."""
    stage = "map:ted-raw"
    after = _get_cursor(conn, stage) or ""
    total = 0

    def parsed() -> Iterator:
        nonlocal after
        for name, xml in raw_store.iter_xml(start_after=after):
            after = name
            yield parse_notice_xml(xml)

    notices = awards_and_modifications(parsed())
    batch: list = []
    for notice in notices:
        batch.append(notice)
        if len(batch) >= 2000:
            total += map_notices(conn, batch, source="ted-raw")
            _set_cursor(conn, stage, after)
            conn.commit()
            batch.clear()
    total += map_notices(conn, batch, source="ted-raw")
    _set_cursor(conn, stage, after)
    conn.commit()
    return total


def map_cohesion(conn, countries: Iterable[str]) -> int:
    """Kohesio's English names, exactly as the loader derives them."""
    total = 0
    for cc in countries:
        stage = f"map:kohesio-{cc}"
        if _get_cursor(conn, stage) == "done":
            continue
        rows = [(r["qid"], r["title"], r["title_lang"])
                for r in parse_kohesio_csv(download_country_csv(cc), since=None)
                if r.get("title_lang") and r.get("title")]
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_COHESION, rows)
        _set_cursor(conn, stage, "done")
        conn.commit()
        logger.info("%s: %d English titles", stage, len(rows))
        total += len(rows)
    return total


# ── events ──────────────────────────────────────────────────────────


def _max_seq(conn) -> int:
    return conn.execute("SELECT coalesce(max(seq), 0) FROM events.entity_events").fetchone()[0]


def migrate_events(conn, name: str, *, batch: int, pause_s: float, dry_run: bool) -> int:
    """Add title_lang to this pass's events, window by window up to the
    newest event at the start (everything after it carries the field)."""
    stage = f"events:{name}"
    lo, end = int(_get_cursor(conn, stage) or 0), _max_seq(conn)
    touched = 0
    sql, events_pass = events_sql(name, dry_run=dry_run), _EVENTS_PASSES[name]
    while lo < end:
        hi = min(lo + batch, end)
        cur = conn.execute(sql, {"domain": events_pass.domain, "lo": lo, "hi": hi,
                                 "event_type": events_pass.event_type})
        touched += cur.fetchone()[0] if dry_run else cur.rowcount
        if not dry_run:
            _set_cursor(conn, stage, str(hi))
        # Commit every window, dry run included: one transaction across the
        # whole log would hold a snapshot open for hours.
        conn.commit()
        if not dry_run:
            time.sleep(pause_s)
        lo = hi
    logger.info("%s: %d events %s", stage, touched, "would change" if dry_run else "changed")
    return touched


# ── graph ───────────────────────────────────────────────────────────


def migrate_graph(conn, driver, domain: str, *, batch: int, dry_run: bool) -> int:
    """Set title_lang on the nodes, straight from the events."""
    stage = f"graph:{domain}"
    lo, end = int(_get_cursor(conn, stage) or 0), _max_seq(conn)
    written = 0
    if not dry_run and lo == 0 and domain in _GRAPH_CLEAR_FIRST:
        _clear_guesses(driver, domain)
    while lo < end:
        hi = min(lo + batch, end)
        rows = latest_per_id(conn.execute(_GRAPH_READ[domain], {"lo": lo, "hi": hi}))
        if rows and not dry_run:
            with driver.session() as session:
                session.run(_GRAPH_SET[domain], rows=rows).consume()
        written += len(rows)
        if not dry_run:
            _set_cursor(conn, stage, str(hi))
        conn.commit()
        lo = hi
    logger.info("%s: %d ids %s", stage, written, "would be set" if dry_run else "set")
    return written


#: Guesses removed before a domain's first graph window, so that the stated
#: languages its events then set are all that remain.
_GRAPH_CLEAR_FIRST = {
    "contract": _GRAPH_CLEAR_OJS_CONTRACTS,
    "eu_cohesion": _GRAPH_CLEAR_COHESION,
}


def _clear_guesses(driver, domain: str) -> int:
    cleared = 0
    with driver.session() as session:
        while True:
            n = session.run(_GRAPH_CLEAR_FIRST[domain]).single()["cleared"]
            cleared += n
            if n == 0:
                break
    logger.info("graph:%s: cleared %d guessed title_lang", domain, cleared)
    return cleared


def clear_contract_guesses(conn, driver, *, batch: int, dry_run: bool) -> int:
    """Contracts whose notice states no language lose a guessed title_lang."""
    after, cleared = "", 0
    while True:
        ids = [r[0] for r in conn.execute(
            "SELECT notice_id FROM migrations.title_lang_map "
            "WHERE title_lang IS NULL AND notice_id > %s ORDER BY notice_id LIMIT %s",
            (after, batch))]
        if not ids:
            break
        if not dry_run:
            with driver.session() as session:
                session.run(_GRAPH_CLEAR_CONTRACTS, ids=ids).consume()
        cleared += len(ids)
        after = ids[-1]
    logger.info("graph:contract: %d notices state no language", cleared)
    return cleared


# ── cli ─────────────────────────────────────────────────────────────


def _months(start: str, end: str) -> list[tuple[int, int]]:
    y, m = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("map", "events", "graph"))
    parser.add_argument("--from", dest="from_month", help="map: first month, YYYY-MM")
    parser.add_argument("--to", dest="to_month", help="map: last month, YYYY-MM")
    parser.add_argument("--raw", action="store_true", help="map: the ted-raw notice XMLs")
    parser.add_argument("--cohesion", action="store_true", help="map: Kohesio's CSVs")
    parser.add_argument("--countries", default=",".join(EU_COUNTRIES))
    parser.add_argument(
        "--domains", default=None,
        help="events: passes to run (default contract,contract_ojs,eu_cohesion); "
             "graph: domains (default contract,eu_cohesion)")
    parser.add_argument("--batch", type=int, default=20000,
                        help="events/graph: seq window per statement")
    parser.add_argument("--pause", type=float, default=0.2,
                        help="events: seconds between windows, to spare the database")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    with psycopg.connect(os.environ["EVENTS_DATABASE_URL"]) as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()
        if args.stage == "map":
            if args.from_month:
                map_months(conn, _months(args.from_month, args.to_month or args.from_month),
                           TedPackageStore.from_env())
            if args.raw and (store := TedRawStore.from_env()) is not None:
                logger.info("ted-raw: %d notices mapped", map_raw(conn, store))
            if args.cohesion:
                map_cohesion(conn, args.countries.split(","))
            return 0
        default = ",".join(_EVENTS_PASSES if args.stage == "events" else _GRAPH_READ)
        domains = [d for d in (args.domains or default).split(",") if d]
        if args.stage == "events":
            for domain in domains:
                migrate_events(conn, domain, batch=args.batch, pause_s=args.pause,
                               dry_run=args.dry_run)
            return 0
        driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(
            os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]))
        try:
            for domain in domains:
                migrate_graph(conn, driver, domain, batch=args.batch, dry_run=args.dry_run)
            if "contract" in domains:
                clear_contract_guesses(conn, driver, batch=5000, dry_run=args.dry_run)
        finally:
            driver.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
