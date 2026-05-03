"""
EU Consolidated Financial Sanctions List → Neo4j + Virtuoso
============================================================
Downloads (or reads a local copy of) the EU consolidated sanctions XML
and writes it to two stores during the Phase 2 dual-write window:

  * Virtuoso (authoritative going forward) — entities pushed via
    SHACL-validated Turtle into the
    ``http://data.fontem.eu/graph/sanctions`` named graph.
  * Neo4j (legacy, removed by the Phase 2 cutover) — SanctionedEntity
    nodes + SANCTIONED edges from /resolve hits, kept until all read
    paths have moved off Neo4j.

When ``VIRTUOSO_SPARQL_ENDPOINT`` is set the Virtuoso write happens
first and aborts the run on validation failure; the Neo4j step only
runs if Virtuoso wrote successfully (or if Virtuoso isn't configured,
which is the staging fallback). Match resolution continues to write
SANCTIONED edges in Neo4j until the cutover step retires that table.

Usage:
    python -m src.etl.load_eu_sanctions --neo4j-uri bolt://localhost:7687
    python -m src.etl.load_eu_sanctions --file /tmp/sanctions.xml \\
        --virtuoso-sparql-endpoint http://virtuoso.gmr.svc.cluster.local:8890/sparql
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
from ._hooks import resolve_entity
from .rdf_sanctions_writer import RdfSanctionsWriter

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

# Sanction → Company linking is delegated to gmr-consolidator's
# /resolve endpoint. Each SanctionedEntity record carries a primary
# `name` (often a short acronym) and a list of `aliases` (the actual
# multilingual entity names). We try /resolve once with the primary
# name, then fall back to each alias if no confident match. All
# guards (MIN_NAME_LEN=6, country agreement, score floor) live in
# the resolver — a single source of truth for matching.

# The MIN_NAME_LEN constant is kept local for backward-compatible
# imports in test_load_eu_sanctions.py; the resolver enforces the
# same value internally.
MIN_NAME_LEN = 6

# Cypher to write SANCTIONED edges from resolver results. Sanctions
# matches always start as `reviewed=false` regardless of tier, because
# the consequence of a wrong attribution is severe (defamation risk)
# and the upstream data is too sparse to lean on hard IDs alone (the
# EU sanctions list carries no LEIs).
MERGE_SANCTIONED = """
UNWIND $rows AS row
MATCH (s:SanctionedEntity {entity_id: row.entity_id})
MATCH (c:Company {gmr_id: row.gmr_id})
MERGE (c)-[r:SANCTIONED {source: 'eu_consolidated'}]->(s)
ON CREATE SET r.since = row.designation_date,
              r.tier = row.tier,
              r.confidence = row.confidence,
              r.matched_via_alias = row.matched_via_alias,
              r.method = 'resolver',
              r.detected_at = datetime(),
              r.reviewed = false
"""

NS = "http://eu.europa.ec/fpi/fsd/export"


def _tag(local: str) -> str:
    """Build a namespace-qualified tag name."""
    return f"{{{NS}}}{local}"


def _child_text(elem, local_name: str) -> str:
    """Get text content of a namespace-qualified child element."""
    child = elem.find(_tag(local_name))
    return (child.text or "").strip() if child is not None else ""


def _child_attr(elem, local_name: str, attr: str) -> str:
    """Get an attribute from a namespace-qualified child element."""
    child = elem.find(_tag(local_name))
    if child is not None:
        return (child.attrib.get(attr) or "").strip()
    return ""


def _parse_entity_type(entity_el):
    """Determine whether the sanctioned subject is a person or entity.

    The subjectType element has a ``code`` attribute: 'person' or 'enterprise'.
    """
    subject_type_el = entity_el.find(_tag("subjectType"))
    if subject_type_el is not None:
        raw = (subject_type_el.attrib.get("code") or "").strip().lower()
    else:
        raw = "entity"
    return "person" if "person" in raw else "entity"


def _collect_names(entity_el):
    """Return (primary_name, aliases) from nameAlias child elements.

    Names are stored as attributes on ``nameAlias`` elements:
    ``wholeName``, ``firstName``, ``lastName``.
    """
    names = []
    for alias_el in entity_el.findall(_tag("nameAlias")):
        whole = (alias_el.attrib.get("wholeName") or "").strip()
        if whole and len(whole) > 1:
            names.append(whole)
            continue
        # Fall back to firstName + lastName
        first = (alias_el.attrib.get("firstName") or "").strip()
        last = (alias_el.attrib.get("lastName") or "").strip()
        combined = f"{first} {last}".strip()
        if combined and len(combined) > 1:
            names.append(combined)

    # De-duplicate while preserving order
    seen = set()
    unique = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)

    primary = unique[0] if unique else ""
    aliases = unique[1:] if len(unique) > 1 else []
    return primary, aliases


def _find_nationality(entity_el):
    """Extract nationality from citizenship child elements.

    The ``citizenship`` element has ``countryIso2Code`` and
    ``countryDescription`` attributes.
    """
    for cit_el in entity_el.findall(_tag("citizenship")):
        country = (cit_el.attrib.get("countryIso2Code") or "").strip()
        if country and country != "00":
            return country
        desc = (cit_el.attrib.get("countryDescription") or "").strip()
        if desc and desc != "UNKNOWN":
            return desc
    return ""


def _find_designation_date(entity_el):
    """Extract the earliest regulation publication date as designation date."""
    earliest = ""
    for reg_el in entity_el.findall(_tag("regulation")):
        pub_date = (reg_el.attrib.get("publicationDate") or "").strip()[:10]
        if pub_date and (not earliest or pub_date < earliest):
            earliest = pub_date
    return earliest


def _find_programme(entity_el):
    """Extract the sanction programme/regime from regulation elements."""
    for reg_el in entity_el.findall(_tag("regulation")):
        prog = (reg_el.attrib.get("programme") or "").strip()
        if prog:
            return prog
    return ""


def parse_sanctions_xml(xml_bytes):
    """Parse EU sanctions XML and yield entity dicts.

    The XML uses namespace ``http://eu.europa.ec/fpi/fsd/export``.
    Each ``sanctionEntity`` element contains nameAlias, subjectType,
    citizenship, regulation, and remark children.
    """
    root = ET.fromstring(xml_bytes)

    for entity_el in root.findall(_tag("sanctionEntity")):
        eu_ref = (
            entity_el.attrib.get("euReferenceNumber")
            or entity_el.attrib.get("logicalId")
            or ""
        ).strip()
        if not eu_ref:
            continue

        entity_type = _parse_entity_type(entity_el)
        primary_name, aliases = _collect_names(entity_el)
        nationality = _find_nationality(entity_el)
        designation_date = _find_designation_date(entity_el)
        regime = _find_programme(entity_el)
        reason = _child_text(entity_el, "remark")

        # Legal basis from the regulation numberTitle attribute
        legal_basis = ""
        reg_el = entity_el.find(_tag("regulation"))
        if reg_el is not None:
            legal_basis = (reg_el.attrib.get("numberTitle") or "").strip()

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


def _resolve_sanction_to_company(entity: dict) -> dict | None:
    """Try /resolve with the sanction's primary name, then each alias.

    Returns a row dict ready for MERGE_SANCTIONED, or None if no
    confident match was found across name + aliases.

    The EU XML stores the short code in `name` (e.g. "AMD") and the
    real legal entity name in `aliases` (e.g. "Aran Modern Devices").
    Iterating aliases lets us catch real matches that the primary
    name alone would miss; the resolver's MIN_NAME_LEN guard rejects
    each acronym attempt automatically.
    """
    nationality = entity.get("nationality") or ""
    if not nationality:
        return None
    candidates = [entity.get("name") or ""]
    candidates.extend(entity.get("aliases") or [])
    for idx, candidate_name in enumerate(candidates):
        if not candidate_name:
            continue
        result = resolve_entity(
            entity_type="Company",
            name=candidate_name,
            country=nationality,
        )
        if result is None or result.match is None:
            continue
        if result.hint != "matched":
            continue
        return {
            "entity_id": entity["entity_id"],
            "gmr_id": result.match.gmr_id,
            "designation_date": entity["designation_date"],
            "tier": result.match.tier,
            "confidence": result.match.confidence,
            "matched_via_alias": idx > 0,
        }
    return None


def load_into_neo4j(driver, entities):
    """MERGE SanctionedEntity nodes; resolve each via /resolve and write
    a SANCTIONED edge for confident matches only.

    The previous in-cypher MATCH_COMPANY_EXACT / _FUZZY paths produced
    8 false positives in production (defamation risk). The /resolve
    service is now the single guard implementation; this loader only
    persists what the resolver confidently identifies.
    """
    total = 0
    matched = 0
    matched_via_alias = 0
    no_match = 0
    batch = []
    t0 = time.time()

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraints ensured")

        all_entities: list[dict] = []
        for entity in entities:
            batch.append(entity)
            all_entities.append(entity)
            if len(batch) >= BATCH_SIZE:
                session.run(MERGE_ENTITY, batch=batch)
                total += len(batch)
                batch = []
                if total % 2000 < BATCH_SIZE:
                    logger.info("  %d entities loaded", total)

        if batch:
            session.run(MERGE_ENTITY, batch=batch)
            total += len(batch)

        # Resolve sanctions → companies via /resolve. One HTTP call per
        # alias-attempt; most sanctions have no Company match at all so
        # this fans out fast. EU sanctions list is small (~3k entries).
        logger.info("Resolving sanction → company links via /resolve ...")
        rows: list[dict] = []
        for entity in all_entities:
            row = _resolve_sanction_to_company(entity)
            if row is None:
                no_match += 1
                continue
            rows.append(row)
            matched += 1
            if row["matched_via_alias"]:
                matched_via_alias += 1

        if rows:
            for i in range(0, len(rows), BATCH_SIZE):
                chunk = rows[i : i + BATCH_SIZE]
                session.run(MERGE_SANCTIONED, rows=chunk)

    elapsed = time.time() - t0
    return {
        "total": total,
        "matched": matched,
        "matched_via_alias": matched_via_alias,
        "no_match": no_match,
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
    # Virtuoso side of the dual-write. If unset, the loader skips
    # the RDF push and only writes Neo4j (legacy behaviour). CI
    # passes the in-cluster sparql endpoint so Phase 2 dev/staging/
    # prod runs all dual-write.
    parser.add_argument(
        "--virtuoso-sparql-endpoint",
        default=os.environ.get("VIRTUOSO_SPARQL_ENDPOINT", ""),
    )
    parser.add_argument(
        "--virtuoso-dba-user",
        default=os.environ.get("VIRTUOSO_DBA_USER", "dba"),
    )
    parser.add_argument(
        "--virtuoso-dba-password",
        default=os.environ.get("VIRTUOSO_DBA_PASSWORD", ""),
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
    all_entities = list(parse_sanctions_xml(xml_bytes))
    logger.info("Parsed %d sanctioned entities total", len(all_entities))

    # GDPR: skip natural persons — only process non-person entities
    entities = [e for e in all_entities if e["entity_type"] != "person"]
    skipped = len(all_entities) - len(entities)
    logger.info(
        "Filtered to %d non-person entities (skipped %d persons)",
        len(entities), skipped,
    )

    # Virtuoso write goes first — SHACL validation is a hard gate.
    # If validation fails we abort before touching Neo4j; that
    # prevents a half-loaded state where the legacy nodes carry
    # data that the authoritative store rejected.
    rdf_summary = None
    if args.virtuoso_sparql_endpoint:
        logger.info(
            "Writing %d entities to Virtuoso at %s",
            len(entities), args.virtuoso_sparql_endpoint,
        )
        writer = RdfSanctionsWriter(
            sparql_endpoint=args.virtuoso_sparql_endpoint,
            dba_user=args.virtuoso_dba_user,
            dba_password=args.virtuoso_dba_password,
        )
        rdf_summary = writer.write(entities)
        logger.info(
            "Virtuoso: wrote %d entities (%d triples), skipped %d persons",
            rdf_summary.written, rdf_summary.triples_pushed,
            rdf_summary.skipped_persons,
        )
    else:
        logger.warning(
            "VIRTUOSO_SPARQL_ENDPOINT is unset — skipping Virtuoso write "
            "(legacy Neo4j-only mode)."
        )

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        summary = load_into_neo4j(driver, entities)
    finally:
        driver.close()

    logger.info(
        "Done: %d entities, %d resolver matches (%d via alias), "
        "%d no_match in %.1fs",
        summary["total"],
        summary["matched"],
        summary["matched_via_alias"],
        summary["no_match"],
        summary["elapsed_s"],
    )


if __name__ == "__main__":
    main()
