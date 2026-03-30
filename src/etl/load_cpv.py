"""Load CPV (Common Procurement Vocabulary) reference taxonomy into Neo4j.

The CPV codelist is published by the EU as part of the eForms SDK.
We embed the top-level divisions (~45 codes) directly and load
detailed codes from TED data as we encounter them.

Usage:
    python -m src.etl.load_cpv --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# Top-level CPV divisions (2-digit codes)
CPV_DIVISIONS = {
    "03": "Agricultural, farming, fishing, forestry products",
    "09": "Petroleum products, fuel, electricity, energy",
    "14": "Mining, basic metals, related products",
    "15": "Food, beverages, tobacco, related products",
    "16": "Agricultural machinery",
    "18": "Clothing, footwear, luggage, accessories",
    "19": "Leather and textile fabrics",
    "22": "Printed matter, related products",
    "24": "Chemical products",
    "30": "Office and computing machinery, equipment",
    "31": "Electrical machinery, apparatus, equipment",
    "32": "Radio, television, communication equipment",
    "33": "Medical equipments, pharmaceuticals",
    "34": "Transport equipment and related products",
    "35": "Security, fire-fighting, police equipment",
    "37": "Musical instruments, sport goods, games",
    "38": "Laboratory, optical, precision equipments",
    "39": "Furniture, furnishings, domestic appliances",
    "41": "Collected and purified water",
    "42": "Industrial machinery",
    "43": "Machinery for mining, quarrying, construction",
    "44": "Construction structures and materials",
    "45": "Construction work",
    "48": "Software package and information systems",
    "50": "Repair and maintenance services",
    "51": "Installation services",
    "55": "Hotel, restaurant and retail trade services",
    "60": "Transport services",
    "63": "Supporting and auxiliary transport services",
    "64": "Postal and telecommunications services",
    "65": "Public utilities",
    "66": "Financial and insurance services",
    "70": "Real estate services",
    "71": "Architectural, construction, engineering services",
    "72": "IT services: consulting, software, internet",
    "73": "Research and development services",
    "75": "Administration, defence, social security",
    "76": "Services related to oil and gas industry",
    "77": "Agricultural, forestry, horticultural services",
    "79": "Business services: law, marketing, consulting",
    "80": "Education and training services",
    "85": "Health and social work services",
    "90": "Sewage, refuse, cleaning, environmental services",
    "92": "Recreational, cultural, sporting services",
    "98": "Other community, social, personal services",
}


def load_cpv_divisions(driver):
    """Load top-level CPV divisions into Neo4j."""
    query = """
    UNWIND $batch AS row
    MERGE (cpv:CPV {code: row.code})
    SET cpv.description = row.description,
        cpv.division    = row.division
    """
    batch = [
        {"code": code + "000000", "description": desc, "division": code}
        for code, desc in CPV_DIVISIONS.items()
    ]
    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT cpv_code IF NOT EXISTS "
            "FOR (cpv:CPV) REQUIRE cpv.code IS UNIQUE"
        )
        session.run(query, batch=batch)
    logger.info("Loaded %d CPV divisions", len(batch))
    return len(batch)


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Load CPV codes into Neo4j")
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "gmr-neo4j-2026"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        load_cpv_divisions(driver)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
