"""
NUTS Region Hierarchy → events.entity_events
=============================================
Loads the NUTS (Nomenclature of Territorial Units for Statistics)
hierarchy as ``UpsertTaxonomyCode`` events with parent_code links
covering all four levels (0: countries, 1: major regions,
2: basic regions, 3: small regions). The sink derives PART_OF
edges (CHILD_OF in event-log terminology) from parent_code.

Entity → region linking is a separate concern (see
``link_entities_to_nuts``); this script only populates the
reference hierarchy.

Usage:
    python -m src.etl.load_nuts
    python -m src.etl.load_nuts --file /tmp/NUTS2024.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
import uuid

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

from src.etl._http import HTTP_HEADERS
from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

NUTS_CSV_URL = (
    "https://ec.europa.eu/eurostat/cache/GISCO/distribution/"
    "v2/nuts/csv/NUTS_AT_2024.csv"
)
SYSTEM = "nuts"


def _parent_code(code: str) -> str | None:
    """Derive the parent NUTS code by removing the last character."""
    if len(code) <= 2:
        return None
    return code[:-1]


def parse_nuts_csv(csv_text: str):
    """Parse a CSV with at least a ``NUTS_ID`` (or ``code``) column.

    Yields dicts with keys: code, name, level, parent, country_alpha3.
    """
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=",")
    fieldnames = [f.strip().strip("﻿") for f in (reader.fieldnames or [])]
    reader.fieldnames = fieldnames

    code_col = None
    name_col = None
    for col in fieldnames:
        upper = col.upper()
        if upper in ("NUTS_ID", "CODE"):
            code_col = col
        if upper in ("NUTS_NAME", "NAME", "LABEL", "DESCRIPTION"):
            name_col = col

    if code_col is None:
        raise ValueError(
            f"CSV must have a NUTS_ID or CODE column, got: {fieldnames}"
        )

    for row in reader:
        code = (row.get(code_col) or "").strip()
        if not code or len(code) < 2 or len(code) > 5:
            continue
        name = (row.get(name_col) or "").strip() if name_col else ""
        level = len(code) - 2
        country_alpha3 = LocationService.country_from_nuts(code) or ""
        yield {
            "code": code,
            "name": name or code,
            "level": level,
            "parent": _parent_code(code),
            "country_alpha3": country_alpha3,
        }


def download_nuts_csv() -> str:
    """Download the NUTS CSV from Eurostat."""
    logger.info("Downloading NUTS CSV from %s", NUTS_CSV_URL)
    resp = httpx.get(NUTS_CSV_URL, timeout=60, follow_redirects=True,
                     headers=HTTP_HEADERS)
    resp.raise_for_status()
    return resp.text


def emit_nuts(log: EventLog, regions) -> dict:
    """Emit one UpsertTaxonomyCode event per region.

    ``country_alpha3`` rides along under the schema's open
    ``description`` field is a misuse — instead we drop it; the
    sink can recover it from the country prefix when needed.
    """
    batch_id = uuid.uuid4()
    total = 0
    by_level: dict[int, int] = {}
    t0 = time.time()
    with log.batch(batch_id, producer="load_nuts") as emit:
        for region in regions:
            emit.upsert(
                "UpsertTaxonomyCode",
                iri=f"http://data.fontem.eu/id/Nuts/{region['code']}",
                domain="nuts",
                payload=builders.upsert_taxonomy_code(
                    system=SYSTEM,
                    code=region["code"],
                    label=region.get("name") or region["code"],
                    parent_code=region.get("parent"),
                    level=region.get("level"),
                ),
            )
            total += 1
            lvl = region.get("level", -1)
            by_level[lvl] = by_level.get(lvl, 0) + 1
    elapsed = time.time() - t0
    logger.info(
        "Done: %d NUTS regions in %.1fs (by level: %s)",
        total, elapsed, by_level,
    )
    return {"total": total, "by_level": by_level, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertTaxonomyCode events for the NUTS hierarchy",
    )
    parser.add_argument(
        "--file",
        help="Path to a local CSV with NUTS_ID and NUTS_NAME columns",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, encoding="utf-8") as fh:
                csv_text = fh.read()
        except OSError:
            logger.exception("Failed to read file %s", args.file)
            sys.exit(1)
    else:
        csv_text = download_nuts_csv()

    regions = list(parse_nuts_csv(csv_text))
    if not regions:
        logger.error("Parsed zero regions from CSV — aborting")
        sys.exit(1)
    logger.info("Parsed %d NUTS regions", len(regions))

    log = EventLog.from_env()
    try:
        emit_nuts(log, regions)
    finally:
        log.close()


if __name__ == "__main__":
    main()
