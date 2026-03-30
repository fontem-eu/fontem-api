"""
GLEIF Level 1 ETL — load Company nodes into Neo4j from the concatenated file.

Usage::

    # From a local ZIP (already downloaded):
    python -m src.etl.load_gleif --file /tmp/gleif-lei2.zip

    # Auto-download the latest file from GLEIF:
    python -m src.etl.load_gleif

The script is idempotent: ``MERGE`` on ``gmr_id`` prevents duplicates.
Re-running refreshes names, status, country, and legal form.
"""
from __future__ import annotations

import argparse
import io
import logging
import time
import zipfile
from typing import Iterator
from xml.etree.ElementTree import iterparse

import httpx
from neo4j import GraphDatabase

from . import gmr_id

logger = logging.getLogger(__name__)

# LEI-CDF 3.1 XML namespace
NS = "http://www.gleif.org/data/schema/leidata/2016"

# GLEIF concatenated file API
GLEIF_API = "https://leidata.gleif.org/api/v1/concatenated-files/lei2"

BATCH_SIZE = 2000

# ── XML parsing ──────────────────────────────────────────────────────────


def _find_text(element, path: str) -> str | None:
    """Find text of a namespaced child element."""
    node = element.find(path, {"lei": NS})
    return node.text.strip() if node is not None and node.text else None


def parse_gleif_xml(stream) -> Iterator[dict]:
    """
    Stream-parse a GLEIF LEI-CDF XML file and yield one dict per record.

    Uses iterparse to keep memory constant regardless of file size.
    """
    tag_record = f"{{{NS}}}LEIRecord"
    tag_lei = f"{{{NS}}}LEI"

    for _, elem in iterparse(stream, events=("end",)):
        if elem.tag != tag_record:
            continue

        lei_node = elem.find(tag_lei)
        if lei_node is None or not lei_node.text:
            elem.clear()
            continue

        lei = lei_node.text.strip()
        entity = elem.find(f"{{{NS}}}Entity")
        if entity is None:
            elem.clear()
            continue

        name = _find_text(entity, "lei:LegalName")
        country = _find_text(entity, "lei:LegalAddress/lei:Country")
        status = _find_text(entity, "lei:EntityStatus")

        legal_form_node = entity.find(
            f"{{{NS}}}LegalForm"
        )
        legal_form = None
        if legal_form_node is not None:
            legal_form = (
                _find_text(legal_form_node, "lei:EntityLegalFormCode")
                or _find_text(legal_form_node, "lei:OtherLegalForm")
            )

        yield {
            "lei": lei,
            "name": name or "",
            "country": country or "",
            "legal_form": legal_form or "",
            "active": status == "ACTIVE",
        }

        elem.clear()


# ── File download ────────────────────────────────────────────────────────


def get_latest_download_url() -> str:
    """Query GLEIF API for the latest concatenated file download URL."""
    resp = httpx.get(
        GLEIF_API, params={"page": 0, "pageSize": 1}, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise RuntimeError("No GLEIF concatenated files found")
    file_id = data[0]["id"]
    return f"{GLEIF_API}/get/{file_id}/zip"


def download_gleif_zip(url: str, dest: str) -> str:
    """Download the GLEIF ZIP file to *dest*, showing progress."""
    logger.info("Downloading %s -> %s", url, dest)
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1_048_576):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(
                        f"\r  {downloaded // 1_048_576} / "
                        f"{total // 1_048_576} MB ({pct}%)",
                        end="", flush=True,
                    )
        print()
    return dest


def open_gleif_zip(path: str):
    """Open the GLEIF ZIP and return a file-like object for the XML inside."""
    zf = zipfile.ZipFile(path)  # pylint: disable=consider-using-with
    names = zf.namelist()
    xml_name = next((n for n in names if n.endswith(".xml")), names[0])
    logger.info("Reading %s from ZIP", xml_name)
    return io.BufferedReader(zf.open(xml_name))


# ── Neo4j loading ────────────────────────────────────────────────────────

MERGE_QUERY = """
UNWIND $batch AS row
MERGE (c:Company {gmr_id: row.gmr_id})
SET c.lei        = row.lei,
    c.name       = row.name,
    c.country    = row.country,
    c.legal_form = row.legal_form,
    c.active     = row.active
"""


def load_into_neo4j(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    driver,
    records: Iterator[dict],
    batch_size: int = BATCH_SIZE,
    constraint: bool = True,
) -> dict:
    """
    Load parsed GLEIF records into Neo4j in batches.

    Returns a summary dict with counts.
    """
    if constraint:
        with driver.session() as s:
            s.run(
                "CREATE CONSTRAINT company_gmr_id IF NOT EXISTS "
                "FOR (c:Company) REQUIRE c.gmr_id IS UNIQUE"
            )

    loaded = 0
    skipped = 0
    batch: list[dict] = []

    for rec in records:
        lei = rec.get("lei", "")
        if len(lei) != 20:
            skipped += 1
            continue

        rec["gmr_id"] = gmr_id.from_lei(lei)
        batch.append(rec)

        if len(batch) >= batch_size:
            _flush(driver, batch)
            loaded += len(batch)
            if loaded % 50_000 == 0:
                logger.info("  ... %d companies loaded", loaded)
            batch = []

    if batch:
        _flush(driver, batch)
        loaded += len(batch)

    return {"loaded": loaded, "skipped": skipped}


def _flush(driver, batch: list[dict]) -> None:
    with driver.session() as s:
        s.run(MERGE_QUERY, batch=batch)


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: download (or use local) GLEIF ZIP, parse, load into Neo4j."""
    parser = argparse.ArgumentParser(
        description="Load GLEIF LEI data into Neo4j"
    )
    parser.add_argument(
        "--file", help="Path to a local GLEIF ZIP file (skip download)"
    )
    parser.add_argument("--neo4j-uri", default="bolt://neo4j:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="")
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help="Records per Neo4j transaction"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Resolve ZIP path
    if args.file:
        zip_path = args.file
    else:
        url = get_latest_download_url()
        zip_path = "/tmp/gleif-lei2.zip"
        download_gleif_zip(url, zip_path)

    # Parse + load
    logger.info("Opening ZIP: %s", zip_path)
    xml_stream = open_gleif_zip(zip_path)

    driver = GraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )

    t0 = time.time()
    logger.info("Loading into Neo4j at %s ...", args.neo4j_uri)
    summary = load_into_neo4j(
        driver, parse_gleif_xml(xml_stream), args.batch_size
    )
    elapsed = time.time() - t0

    driver.close()
    xml_stream.close()

    logger.info(
        "Done in %.1fs — %d companies loaded, %d skipped",
        elapsed, summary["loaded"], summary["skipped"],
    )


if __name__ == "__main__":
    main()
