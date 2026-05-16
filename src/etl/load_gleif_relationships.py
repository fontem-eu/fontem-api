"""
GLEIF Level 2 (Relationships) → events.entity_events
====================================================
Downloads the GLEIF relationship records (parent-subsidiary) and
emits ``UpsertRelationship`` events with predicate ``subsidiaryOf``.
Sinks resolve LEI → Company gmr_id via ``gmr_id.from_lei`` and
materialise SUBSIDIARY_OF edges in Neo4j and the corresponding
fontem:subsidiaryOf triples in Virtuoso.

Usage:
    python -m src.etl.load_gleif_relationships
    python -m src.etl.load_gleif_relationships --file /tmp/gleif-rr.zip
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import uuid
import zipfile
from xml.etree.ElementTree import iterparse

import httpx
from fontem_event_schemas import builders
from fontem_events import EventLog

from . import gmr_id
from ._http import HTTP_HEADERS

logger = logging.getLogger(__name__)

GLEIF_RR_API = "https://leidata.gleif.org/api/v1/concatenated-files/rr"
NS = "http://www.gleif.org/data/schema/rr/2016"

_t = f"{{{NS}}}"
TAG_RECORD = f"{_t}RelationshipRecord"
TAG_RELATIONSHIP = f"{_t}Relationship"
TAG_START_NODE = f"{_t}StartNode"
TAG_END_NODE = f"{_t}EndNode"
TAG_NODE_ID = f"{_t}NodeID"
TAG_REL_TYPE = f"{_t}RelationshipType"
TAG_REL_STATUS = f"{_t}RelationshipStatus"


def resolve_latest_url() -> str:
    """Query the GLEIF API for the latest Level 2 file URL."""
    resp = httpx.get(f"{GLEIF_RR_API}?page=0&pageSize=1", timeout=30,
                     headers=HTTP_HEADERS)
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise RuntimeError("GLEIF API returned no relationship files")
    file_id = data[0]["id"]
    return f"{GLEIF_RR_API}/get/{file_id}/zip"


def download_zip(url: str) -> io.BytesIO:
    """Download a ZIP file into memory."""
    logger.info("Downloading %s ...", url)
    buf = io.BytesIO()
    with httpx.stream("GET", url, timeout=600, follow_redirects=True,
                      headers=HTTP_HEADERS) as r:
        r.raise_for_status()
        total = 0
        for chunk in r.iter_bytes(chunk_size=256 * 1024):
            buf.write(chunk)
            total += len(chunk)
    buf.seek(0)
    logger.info("Downloaded %.0f MB", total / 1e6)
    return buf


def _text(parent, tag):
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else None


def parse_relationships(xml_stream):
    """Yield (child_lei, parent_lei, rel_type) for ACTIVE direct- or
    ultimate-consolidation relationships."""
    for _event, elem in iterparse(xml_stream, events=("end",)):
        if elem.tag != TAG_RECORD:
            continue

        rel = elem.find(TAG_RELATIONSHIP)
        if rel is None:
            elem.clear()
            continue

        start_node = rel.find(TAG_START_NODE)
        end_node = rel.find(TAG_END_NODE)
        if start_node is None or end_node is None:
            elem.clear()
            continue

        child_lei = _text(start_node, TAG_NODE_ID)
        parent_lei = _text(end_node, TAG_NODE_ID)
        rel_type = _text(rel, TAG_REL_TYPE)
        status = _text(rel, TAG_REL_STATUS)

        elem.clear()

        if not child_lei or not parent_lei:
            continue
        if status and status != "ACTIVE":
            continue

        if rel_type == "IS_DIRECTLY_CONSOLIDATED_BY":
            yield child_lei, parent_lei, "direct"
        elif rel_type == "IS_ULTIMATELY_CONSOLIDATED_BY":
            yield child_lei, parent_lei, "ultimate"


def emit_relationships(log: EventLog, records) -> dict:
    """Emit one UpsertRelationship per (child_lei, parent_lei) record.

    Predicate is ``subsidiaryOf``; the ``properties`` bag carries
    ``consolidation_type`` (direct vs ultimate) so downstream queries
    can distinguish them.
    """
    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()
    with log.batch(batch_id, producer="load_gleif_relationships") as emit:
        for child_lei, parent_lei, consolidation_type in records:
            child_id = str(gmr_id.from_lei(child_lei))
            parent_id = str(gmr_id.from_lei(parent_lei))
            emit.upsert(
                "UpsertRelationship",
                # IRI key = src + predicate + dst — keeps the event
                # row addressable for replay.
                iri=(
                    f"http://data.fontem.eu/id/Relationship/"
                    f"{child_id}-subsidiaryOf-{parent_id}"
                ),
                domain="company",
                payload=builders.upsert_relationship(
                    src_iri=f"http://data.fontem.eu/id/Company/{child_id}",
                    dst_iri=f"http://data.fontem.eu/id/Company/{parent_id}",
                    predicate="subsidiaryOf",
                    properties={"consolidation_type": consolidation_type},
                ),
            )
            total += 1
            if total % 50000 == 0:
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed else 0
                logger.info(
                    "  %d relationships emitted (%.0f/s)", total, rate,
                )
    elapsed = time.time() - t0
    logger.info("Done: %d relationships in %.1fs", total, elapsed)
    return {"total": total, "elapsed_s": round(elapsed, 1)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertRelationship events for GLEIF Level 2",
    )
    parser.add_argument("--file", help="Path to a local GLEIF RR ZIP file")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.file:
        logger.info("Reading local file: %s", args.file)
        zf = zipfile.ZipFile(args.file)
    else:
        url = resolve_latest_url()
        buf = download_zip(url)
        zf = zipfile.ZipFile(buf)

    xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
    if not xml_names:
        logger.error("No XML file found in ZIP")
        sys.exit(1)

    xml_name = xml_names[0]
    logger.info("Parsing %s ...", xml_name)

    log = EventLog.from_env()
    try:
        with zf.open(xml_name) as xml_stream:
            records = parse_relationships(xml_stream)
            emit_relationships(log, records)
    finally:
        log.close()


if __name__ == "__main__":
    main()
