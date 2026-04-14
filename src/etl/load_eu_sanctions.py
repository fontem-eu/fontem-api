"""
EU Consolidated Financial Sanctions List → Neo4j
=================================================
Downloads (or reads a local copy of) the EU consolidated sanctions XML
and MERGEs SanctionedEntity nodes into Neo4j.  Attempts to match
sanctioned entities against existing Company nodes by name, creating
SANCTIONED relationships for confirmed matches and SAME_AS edges for
fuzzy matches.

Usage:
    python -m src.etl.load_eu_sanctions --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_eu_sanctions --file /tmp/sanctions.xml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET

import httpx
from neo4j import GraphDatabase

from . import gmr_id

logger = logging.getLogger(__name__)

SANCTIONS_URL = (
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/"
    "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"
)

BATCH_SIZE = 500

CONSTRAINT_CYPHER = """
CREATE CONSTRAINT sanctioned_entity_id IF NOT EXISTS
FOR (s:SanctionedEntity) REQUIRE s.entity_id IS UNIQUE
"""

MERGE_ENTITY = """
UNWIND $batch AS row
MERGE (s:SanctionedEntity {entity_id: row.entity_id})
SET s.name             = row.name,
    s.entity_type      = row.entity_type,
    s.aliases          = row.aliases,
    s.nationality      = row.nationality,
    s.designation_date = row.designation_date,
    s.sanction_regime  = row.sanction_regime,
    s.legal_basis      = row.legal_basis,
    s.listing_reason   = row.listing_reason,
    s.eu_reference     = row.eu_reference
"""

MATCH_COMPANY_EXACT = """
UNWIND $batch AS row
MATCH (s:SanctionedEntity {entity_id: row.entity_id})
WITH s, row
MATCH (c:Company)
WHERE c.name = s.name
MERGE (c)-[:SANCTIONED {source: 'eu_consolidated', since: row.designation_date}]->(s)
"""

MATCH_COMPANY_FUZZY = """
UNWIND $batch AS row
MATCH (s:SanctionedEntity {entity_id: row.entity_id})
WITH s, row
WHERE s.name IS NOT NULL AND size(s.name) > 3
WITH s, row,
     reduce(n = s.name, c IN ['+','-','&&','||','!','(',')','{','}',
            '[',']','^','"','~','*','?',':','\\\\','/']
            | replace(n, c, ' ')) AS clean_name
WHERE size(trim(clean_name)) > 3
CALL db.index.fulltext.queryNodes('company_name_ft', clean_name)
     YIELD node AS c, score
WHERE score > 1.5
WITH s, c, score ORDER BY score DESC LIMIT 1
MERGE (s)-[:SAME_AS {
    confidence: 0.6,
    method: 'sanction_name_match',
    detected_at: datetime(),
    reviewed: false
}]->(c)
"""

CREATE_FT_INDEX = """
CREATE FULLTEXT INDEX company_name_ft IF NOT EXISTS
FOR (c:Company) ON EACH [c.name]
"""


def _text(elem, tag):
    """Get text of a child element, or empty string."""
    child = elem.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_entity_type(entity_el):
    """Determine whether the sanctioned subject is a person or entity."""
    subject_type_el = entity_el.find("subjectType")
    if subject_type_el is not None:
        raw = (subject_type_el.text or "").strip().lower()
    else:
        raw = "entity"
    return "person" if "person" in raw else "entity"


def _collect_names(entity_el):
    """Return (primary_name, aliases) from an entity element."""
    names = []
    for name_el in entity_el.iter():
        name_tag = name_el.tag.split("}")[-1] if "}" in name_el.tag else name_el.tag
        if name_tag in ("wholeName", "lastName", "name"):
            name_text = (name_el.text or "").strip()
            if name_text and len(name_text) > 1:
                names.append(name_text)
    primary = names[0] if names else ""
    aliases = names[1:] if len(names) > 1 else []
    return primary, aliases


def _find_nationality(entity_el):
    """Extract nationality from citizenship/country child elements."""
    for cit_el in entity_el.iter():
        cit_tag = cit_el.tag.split("}")[-1] if "}" in cit_el.tag else cit_el.tag
        if cit_tag in ("citizenship", "country"):
            nationality = (cit_el.text or "").strip()
            if nationality:
                return nationality
    return ""


def parse_sanctions_xml(xml_bytes):
    """Parse EU sanctions XML and yield entity dicts."""
    root = ET.fromstring(xml_bytes)

    for entity_el in root.iter():
        tag = entity_el.tag.split("}")[-1] if "}" in entity_el.tag else entity_el.tag
        if tag not in ("sanctionEntity", "SubjectType"):
            continue

        eu_ref = (
            _text(entity_el, "euReferenceNumber")
            or _text(entity_el, "logicalId")
            or entity_el.attrib.get("logicalId", "")
        )
        if not eu_ref:
            continue

        entity_type = _parse_entity_type(entity_el)
        primary_name, aliases = _collect_names(entity_el)
        nationality = _find_nationality(entity_el)

        designation_date = _text(entity_el, "designationDate") or ""
        regime = _text(entity_el, "programme") or _text(entity_el, "regime") or ""
        legal_basis = _text(entity_el, "legalBasis") or ""
        reason = _text(entity_el, "remark") or ""

        entity_id = str(gmr_id.from_name("EU", f"sanction:{eu_ref}"))

        yield {
            "entity_id": entity_id,
            "eu_reference": eu_ref,
            "name": primary_name,
            "entity_type": entity_type,
            "aliases": aliases,
            "nationality": nationality,
            "designation_date": designation_date[:10],
            "sanction_regime": regime[:200],
            "legal_basis": legal_basis[:200],
            "listing_reason": reason[:500],
        }


def load_into_neo4j(driver, entities):
    """MERGE SanctionedEntity nodes and match against Company nodes."""
    total = 0
    matched_exact = 0
    matched_fuzzy = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        session.run(CREATE_FT_INDEX)
        logger.info("Constraints and indexes ensured")

        for entity in entities:
            batch.append(entity)
            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_ENTITY, batch=batch)
                result = session.run(MATCH_COMPANY_EXACT, batch=batch)
                matched_exact += result.consume().counters.relationships_created
                result = session.run(MATCH_COMPANY_FUZZY, batch=batch)
                matched_fuzzy += result.consume().counters.relationships_created
                total += len(batch)
                batch = []
                if total % 2000 < BATCH_SIZE:
                    logger.info("  %d entities loaded", total)

        if batch:
            session.run(MERGE_ENTITY, batch=batch)
            result = session.run(MATCH_COMPANY_EXACT, batch=batch)
            matched_exact += result.consume().counters.relationships_created
            result = session.run(MATCH_COMPANY_FUZZY, batch=batch)
            matched_fuzzy += result.consume().counters.relationships_created
            total += len(batch)

    elapsed = time.time() - t0
    return {
        "total": total,
        "matched_exact": matched_exact,
        "matched_fuzzy": matched_fuzzy,
        "elapsed_s": round(elapsed, 1),
    }


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Load EU Consolidated Sanctions List into Neo4j"
    )
    parser.add_argument("--file", help="Path to local sanctions XML file")
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

    # Load XML
    if args.file:
        logger.info("Reading local file: %s", args.file)
        try:
            with open(args.file, "rb") as fh:
                xml_bytes = fh.read()
        except OSError:
            logger.exception("Failed to read file %s", args.file)
            sys.exit(1)
    else:
        logger.info("Downloading sanctions list from %s", SANCTIONS_URL)
        try:
            resp = httpx.get(SANCTIONS_URL, timeout=120, follow_redirects=True)
            resp.raise_for_status()
            xml_bytes = resp.content
        except httpx.HTTPError:
            logger.exception("Failed to download sanctions list")
            sys.exit(1)

    logger.info("Downloaded/read %d KB", len(xml_bytes) // 1024)
    entities = list(parse_sanctions_xml(xml_bytes))
    logger.info("Parsed %d sanctioned entities", len(entities))

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, entities)
    finally:
        driver.close()

    logger.info(
        "Done: %d entities, %d exact matches, %d fuzzy matches in %.1fs",
        summary["total"],
        summary["matched_exact"],
        summary["matched_fuzzy"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
