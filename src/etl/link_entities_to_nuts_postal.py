"""
Link entities to NUTS regions via postal code
==============================================
Upgrades LOCATED_IN edges for Company (and Authority) nodes that carry a
``postal_code`` property to point at their NUTS 3 region instead of NUTS 0.

Lookup data: ``src/etl/data/PCODE_2025_NUTS-2024_v2.0.zip`` — bundled in
the image because Eurostat GISCO is unreachable from the cluster.

CSV format (semicolon-delimited, values wrapped in single quotes):
    NUTS3;CODE
    'DE212';'80331'

Country matching: Company.country is ISO alpha-3 in the graph (alpha-3
everywhere convention). The postal CSV is keyed on NUTS alpha-2 country
codes, so we convert alpha-3 -> alpha-2 via LocationService.alpha3_to_alpha2
(which maps GRC -> EL, the NUTS code for Greece).

Usage:
    python -m src.etl.link_entities_to_nuts_postal
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import time
import zipfile

from neo4j import GraphDatabase

from src.services.location_service import LocationService

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_POSTAL_ZIP = os.path.join(_DATA_DIR, "PCODE_2025_NUTS-2024_v2.0.zip")
_POSTAL_CSV = "PCODE_2025_NUTS-2024_v2.0.csv"

BATCH_SIZE = 5000


def load_postal_lookup(zip_path: str = _POSTAL_ZIP) -> dict[tuple[str, str], str]:
    """Return a dict mapping (nuts_country_alpha2, postal_code_upper) → nuts3_code."""
    lookup: dict[tuple[str, str], str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(_POSTAL_CSV) as raw:
            # CSV has a UTF-8 BOM; wrap in TextIOWrapper to handle it
            text = io.TextIOWrapper(raw, encoding="utf-8-sig")
            reader = csv.reader(text, delimiter=";")
            next(reader)  # skip header
            for row in reader:
                if len(row) < 2:
                    continue
                nuts3 = row[0].strip().strip("'")
                code = row[1].strip().strip("'").upper().replace(" ", "")
                if len(nuts3) >= 2:
                    country = nuts3[:2]
                    lookup[(country, code)] = nuts3
    logger.info("Loaded %d postal → NUTS3 mappings", len(lookup))
    return lookup


# Paginate the company scan so a 3M+ company graph never lands entirely in
# memory. Each page is fetched, resolved against the postal lookup, and its
# resolved rows MERGE-flushed before the next page — bounding memory to one
# page plus its resolved subset.
_FETCH_PAGE = """
MATCH (c:Company)
WHERE c.postal_code IS NOT NULL AND c.country IS NOT NULL
RETURN c.gmr_id AS gmr_id, c.country AS country, c.postal_code AS postal_code
ORDER BY c.gmr_id
SKIP $skip LIMIT $page
"""

_MERGE_Q = """
UNWIND $batch AS row
MATCH (c:Company {gmr_id: row.gmr_id})
MATCH (n:NUTSRegion {code: row.nuts3})
MERGE (c)-[:LOCATED_IN]->(n)
"""


def _resolve(rows, lookup):
    """Map a page of company rows to {gmr_id, nuts3} for those whose postal
    code resolves in the lookup."""
    out = []
    for row in rows:
        # Company.country is ISO alpha-3 in the graph (alpha-3 everywhere);
        # the postal table is keyed on the NUTS alpha-2 country (GRC -> EL).
        nuts_ctry = LocationService.alpha3_to_alpha2(row["country"])
        if not nuts_ctry:
            continue
        code = (row["postal_code"] or "").upper().replace(" ", "")
        nuts3 = lookup.get((nuts_ctry, code))
        if nuts3:
            out.append({"gmr_id": row["gmr_id"], "nuts3": nuts3})
    return out


def link_companies(session, lookup: dict, batch_size: int = BATCH_SIZE) -> int:
    """Upgrade Company LOCATED_IN edges to NUTS 3 where postal code resolves.

    Scans companies with a postal code in ``batch_size`` pages; for each page
    the resolvable rows are MERGE-flushed to a ``LOCATED_IN`` edge on the
    NUTS-3 region (keyed on ``NUTSRegion.code``). Idempotent — re-running only
    adds still-missing edges.
    """
    created = 0
    scanned = 0
    resolved_total = 0
    skip = 0
    while True:
        page = session.run(_FETCH_PAGE, skip=skip, page=batch_size).data()
        if not page:
            break
        scanned += len(page)
        resolved = _resolve(page, lookup)
        resolved_total += len(resolved)
        if resolved:
            summary = session.run(_MERGE_Q, batch=resolved).consume()
            created += summary.counters.relationships_created
        skip += batch_size
        if scanned % (batch_size * 20) == 0:
            logger.info("Scanned %d companies, %d edges created so far", scanned, created)

    logger.info(
        "Scanned %d companies, resolved %d to NUTS 3, %d new edges",
        scanned, resolved_total, created,
    )
    return created


def run(driver) -> dict:
    """Upgrade Company LOCATED_IN edges to NUTS 3 via postal code."""
    lookup = load_postal_lookup()
    t0 = time.time()
    with driver.session() as session:
        created = link_companies(session, lookup)
    return {"created": created, "elapsed_s": round(time.time() - t0, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Upgrade Company LOCATED_IN edges to NUTS 3 via postal code"
    )
    parser.add_argument(
        "--neo4j-uri",
        default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    )
    parser.add_argument(
        "--neo4j-user",
        default=os.environ.get("NEO4J_USER", "neo4j"),
    )
    parser.add_argument(
        "--neo4j-password",
        default=os.environ.get("NEO4J_PASSWORD", ""),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = run(driver)
    finally:
        driver.close()

    logger.info(
        "Done: %d LOCATED_IN edges upgraded to NUTS 3 in %.1fs",
        summary["created"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
