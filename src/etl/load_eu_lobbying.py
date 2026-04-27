"""
Load EU Transparency Register data into Neo4j.

Downloads the daily XML dump from the Transparency Register and creates
Lobbyist nodes with REPRESENTS relationships to matched Company nodes.

Usage:
    python -m src.etl.load_eu_lobbying [--neo4j-uri URI] [--neo4j-user USER] [--neo4j-password PWD]

Idempotent: uses MERGE on identificationCode — safe to re-run.
"""
from __future__ import annotations

import argparse
import logging
import os
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TR_XML_URL = "https://transparency-register.europa.eu/odplastorganisationxml_en"

BATCH_SIZE = 500

# Cypher for creating the constraint (idempotent)
CONSTRAINT_CYPHER = """
CREATE CONSTRAINT lobbyist_tr_id IF NOT EXISTS
FOR (l:Lobbyist) REQUIRE l.tr_id IS UNIQUE
"""

# Cypher for merging a lobbyist
MERGE_LOBBYIST = """
UNWIND $batch AS row
MERGE (l:Lobbyist {tr_id: row.tr_id})
SET l.name            = row.name,
    l.acronym         = row.acronym,
    l.country         = row.country,
    l.country_iso     = row.country_iso,
    l.city            = row.city,
    l.category        = row.category,
    l.entity_form     = row.entity_form,
    l.website         = row.website,
    l.goals           = row.goals,
    l.ep_passes       = row.ep_passes,
    l.members_fte     = row.members_fte,
    l.cost_min        = row.cost_min,
    l.cost_max        = row.cost_max,
    l.registration_date = row.registration_date,
    l.last_updated    = row.last_updated
"""

# Minimum name length for fuzzy lobbyist→company matching. Below this,
# topical name overlap dominates the signal — see the false positives
# we observed: Federación Española del Vino → Federación Española de
# Triatlón (both 6+ chars but the *prefix* match is what fooled the
# old score>2.0 floor; the higher floor below catches that).
MIN_NAME_LEN = 6

# Match lobbyist → company using the full-text index. Hardened guards:
# 1. Both sides must declare a country, equal after ISO normalisation
#    (no NULL-bypass — the previous OR-chain disabled the guard whenever
#    either side was NULL, and l.country_iso was NULL on every existing
#    lobbyist node).
# 2. Score floor lifted 2.0 → 4.0 (single-token overlap was ~2.0).
# 3. REPRESENTS edges carry reviewed:false so the manual-review UI can
#    stage them. NEVER auto-confident — every match here is a candidate.
# 4. Loader is idempotent: ON CREATE writes the metadata, ON MATCH
#    keeps a human's reviewed=true sticky.
MATCH_COMPANY = """
UNWIND $batch AS row
MATCH (l:Lobbyist {tr_id: row.tr_id})
WHERE l.name IS NOT NULL
  AND size(l.name) >= $min_name_len
  AND coalesce(l.country_iso, '') <> ''
WITH l,
     reduce(s = l.name, c IN ['+','-','&&','||','!','(',')','{','}',
            '[',']','^','"','~','*','?',':','\\\\','/']
            | replace(s, c, ' ')) AS clean_name
WHERE size(trim(clean_name)) >= $min_name_len
CALL db.index.fulltext.queryNodes('company_name_ft', clean_name)
     YIELD node AS c, score
WHERE score > 4.0
  AND coalesce(c.country, '') = l.country_iso
WITH l, c, score ORDER BY score DESC LIMIT 1
MERGE (l)-[r:REPRESENTS]->(c)
ON CREATE SET r.confidence = round(score * 1000) / 1000.0,
              r.method = 'fulltext_lobbyist',
              r.detected_at = datetime(),
              r.reviewed = false
"""

# Ensure full-text index exists
CREATE_FT_INDEX = """
CREATE FULLTEXT INDEX company_name_ft IF NOT EXISTS FOR (c:Company) ON EACH [c.name]
"""

# Country name normalization (TR uses full names, Company nodes use ISO)
_COUNTRY_MAP = {
    "UNITED STATES": "US", "UNITED KINGDOM": "GB", "GERMANY": "DEU",
    "FRANCE": "FRA", "SPAIN": "ESP", "ITALY": "ITA", "NETHERLANDS": "NLD",
    "BELGIUM": "BEL", "SWEDEN": "SWE", "AUSTRIA": "AUT", "DENMARK": "DNK",
    "FINLAND": "FIN", "IRELAND": "IRL", "POLAND": "POL", "PORTUGAL": "PRT",
    "CZECH REPUBLIC": "CZE", "ROMANIA": "ROU", "HUNGARY": "HUN",
    "GREECE": "GRC", "LUXEMBOURG": "LUX", "CROATIA": "HRV", "BULGARIA": "BGR",
    "SLOVAKIA": "SVK", "SLOVENIA": "SVN", "LITHUANIA": "LTU", "LATVIA": "LVA",
    "ESTONIA": "EST", "MALTA": "MLT", "CYPRUS": "CYP", "SWITZERLAND": "CHE",
    "NORWAY": "NOR", "JAPAN": "JPN", "CANADA": "CAN", "AUSTRALIA": "AUS",
    "CHINA": "CHN", "INDIA": "IND", "BRAZIL": "BRA",
}

# Create interest relationships
MERGE_INTERESTS = """
UNWIND $batch AS row
MATCH (l:Lobbyist {tr_id: row.tr_id})
WITH l, row
UNWIND row.interests AS interest_name
MERGE (i:LobbyInterest {name: interest_name})
MERGE (l)-[:INTERESTED_IN]->(i)
"""


def _text(elem: ET.Element | None, path: str) -> str:
    """Extract text from an XML element at the given path."""
    if elem is None:
        return ""
    child = elem.find(path)
    return (child.text or "").strip() if child is not None else ""


def _parse_entity(elem: ET.Element) -> dict[str, Any]:
    """Parse an interestRepresentative XML element into a flat dict."""
    tr_id = _text(elem, "identificationCode")
    name_el = elem.find("name")
    name = _text(name_el, "originalName") if name_el is not None else ""

    head_office = elem.find("headOffice")
    country = _text(head_office, "country") if head_office is not None else ""
    city = _text(head_office, "city") if head_office is not None else ""

    # Financial data
    cost_min = 0
    cost_max = 0
    fin = elem.find("financialData")
    if fin is not None:
        closed = fin.find("closedYear")
        if closed is not None:
            costs = closed.find("costs")
            if costs is not None:
                range_el = costs.find("range")
                if range_el is not None:
                    try:
                        cost_max = int(_text(range_el, "max") or "0")
                    except ValueError:
                        pass
                    try:
                        cost_min = int(_text(range_el, "min") or "0")
                    except ValueError:
                        pass

    # EP accredited passes
    ep_passes = 0
    try:
        ep_passes = int(_text(elem, "EPAccreditedNumber") or "0")
    except ValueError:
        pass

    # Members FTE
    members_fte = 0.0
    members_el = elem.find("members")
    if members_el is not None:
        try:
            members_fte = float(_text(members_el, "membersFTE") or "0")
        except ValueError:
            pass

    # Interests
    interests = []
    interests_el = elem.find("interests")
    if interests_el is not None:
        for interest in interests_el.findall("interest"):
            interest_name = _text(interest, "name")
            if interest_name:
                interests.append(interest_name)

    return {
        "tr_id": tr_id,
        "name": name,
        "acronym": _text(elem, "acronym"),
        "country": country,
        "country_iso": _COUNTRY_MAP.get(country.upper(), country),
        "city": city,
        "category": _text(elem, "registrationCategory"),
        "entity_form": _text(elem, "entityForm"),
        "website": _text(elem, "webSiteURL"),
        "goals": (_text(elem, "goals") or "")[:500],
        "ep_passes": ep_passes,
        "members_fte": members_fte,
        "cost_min": cost_min,
        "cost_max": cost_max,
        "registration_date": _text(elem, "registrationDate")[:10],
        "last_updated": _text(elem, "lastUpdateDate")[:10],
        "interests": interests,
    }


def load_eu_lobbying(neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> None:
    """Download TR XML and load into Neo4j."""
    logger.info("Downloading EU Transparency Register XML from %s ...", TR_XML_URL)

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        resp = client.get(TR_XML_URL)
        resp.raise_for_status()
    xml_bytes = resp.content
    logger.info("Downloaded %d MB", len(xml_bytes) // (1024 * 1024))

    # Clean invalid XML character references before parsing
    import re  # pylint: disable=import-outside-toplevel
    xml_text = xml_bytes.decode("utf-8", errors="replace")
    xml_text = re.sub(r"&#x[0-9a-fA-F]{1,2};", "", xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)

    root = ET.fromstring(xml_text)
    meta = root.find("metaData")
    if meta is not None:
        logger.info("Export date: %s, entities: %s",
                     _text(meta, "exportDate"), _text(meta, "numberOfIR"))

    result_list = root.find("resultList")
    if result_list is None:
        logger.error("No resultList found in XML")
        return

    entities = []
    for elem in result_list:
        tag = elem.tag.split("}")[-1]
        if tag == "interestRepresentative":
            parsed = _parse_entity(elem)
            if parsed["tr_id"]:
                entities.append(parsed)

    logger.info("Parsed %d lobbyist entities", len(entities))

    # Load into Neo4j
    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

    with driver.session() as session:
        session.run(CONSTRAINT_CYPHER)
        logger.info("Constraint ensured")

        # Batch merge lobbyists
        for i in range(0, len(entities), BATCH_SIZE):
            batch = entities[i : i + BATCH_SIZE]
            session.run(MERGE_LOBBYIST, batch=batch)
            session.run(MERGE_INTERESTS, batch=batch)
            logger.info("  %d / %d lobbyists loaded", min(i + BATCH_SIZE, len(entities)), len(entities))

        # Create full-text index for fuzzy company matching
        logger.info("Ensuring full-text index on Company.name...")
        session.run(CREATE_FT_INDEX)

        # Try to match to existing companies (full-text fuzzy search)
        logger.info("Matching lobbyists to existing Company nodes...")
        matched = 0
        for i in range(0, len(entities), BATCH_SIZE):
            batch = entities[i : i + BATCH_SIZE]
            result = session.run(
                MATCH_COMPANY, batch=batch, min_name_len=MIN_NAME_LEN,
            )
            summary = result.consume()
            matched += summary.counters.relationships_created
            if (i + BATCH_SIZE) % 2000 < BATCH_SIZE:
                logger.info("  %d / %d checked, %d matches so far",
                            min(i + BATCH_SIZE, len(entities)), len(entities), matched)
        logger.info("Company matching done: %d REPRESENTS relationships created", matched)

    driver.close()

    logger.info("Done: %d lobbyists loaded, interests linked, company matches attempted", len(entities))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load EU Transparency Register into Neo4j")
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    args = parser.parse_args()

    load_eu_lobbying(args.neo4j_uri, args.neo4j_user, args.neo4j_password)
