"""Load CPV (Common Procurement Vocabulary) reference taxonomy
into the event log as ``UpsertTaxonomyCode`` events.

The CPV codelist is published by the EU as part of the eForms SDK.
We embed the top-level divisions (~45 codes) directly and load
detailed codes from TED data as we encounter them. Each code emits
one event keyed by (system='cpv', code=<8-digit>); sinks materialise
:CPV nodes and skos:broader / CHILD_OF edges from parent_code.

Usage:
    python -m src.etl.load_cpv
"""
from __future__ import annotations

import argparse
import logging
import time
import uuid

from fontem_event_schemas import builders
from fontem_events import EventLog

logger = logging.getLogger(__name__)

SYSTEM = "cpv"

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


# Detailed CPV codes that frequently appear in EU procurement data
# without descriptions. Sourced from the official CPV 2008 taxonomy.
CPV_DETAILED = {
    "09310000": "Electricity",
    "09320000": "Steam, hot water and associated products",
    "14210000": "Gravel, sand, crusite stone and aggregates",
    "15300000": "Fruit, vegetables and related products",
    "15800000": "Miscellaneous food products",
    "15900000": "Beverages, tobacco and related products",
    "22100000": "Printed books, brochures and leaflets",
    "30200000": "Computer equipment and supplies",
    "30210000": "Data-processing machines (hardware)",
    "33100000": "Medical equipments",
    "33140000": "Medical consumables",
    "33600000": "Pharmaceutical products",
    "33690000": "Various medicinal products",
    "33700000": "Personal care products",
    "34100000": "Motor vehicles",
    "34110000": "Passenger cars",
    "34140000": "Heavy-duty motor vehicles",
    "34300000": "Parts and accessories for vehicles",
    "34900000": "Miscellaneous transport equipment and spare parts",
    "35100000": "Emergency and security equipment",
    "38000000": "Laboratory, optical and precision equipments",
    "39100000": "Furniture",
    "39200000": "Furnishing",
    "42400000": "Lifting and handling equipment",
    "44100000": "Construction materials and associated items",
    "44200000": "Structural products",
    "45100000": "Site preparation work",
    "45200000": "Works for complete or part construction",
    "45230000": "Construction of pipelines, communication, power lines and highways",
    "45233000": "Construction, foundation and surface works for highways",
    "45234100": "Railway construction works",
    "45300000": "Building installation work",
    "45400000": "Building completion work",
    "48000000": "Software package and information systems",
    "48800000": "Information systems and servers",
    "50000000": "Repair and maintenance services",
    "50100000": "Repair, maintenance and associated services of vehicles",
    "55000000": "Hotel, restaurant and retail trade services",
    "60000000": "Transport services (excl. waste transport)",
    "60100000": "Road transport services",
    "63500000": "Travel agency, tour operator, tourist assistance",
    "66000000": "Financial and insurance services",
    "66500000": "Insurance and pension services",
    "66510000": "Insurance services",
    "71300000": "Hydraulic engineering services",
    "71500000": "Construction-related services",
    "73000000": "Research and development services and related consultancy services",
    "77300000": "Horticultural services",
    "79000000": "Business services: law, marketing, consulting, recruitment",
    "79200000": "Accounting, auditing and fiscal services",
    "79300000": "Market and economic research; polling and statistics",
    "79400000": "Business and management consultancy services",
    "79500000": "Office-support services",
    "79600000": "Recruitment services",
    "79700000": "Investigation and security services",
    "79800000": "Printing and related services",
    "79900000": "Miscellaneous business and business-related services",
    "80500000": "Training services",
    "85100000": "Health services",
    "85300000": "Social work and related services",
    "90500000": "Refuse and waste related services",
    "90600000": "Cleaning services for urban/rural areas",
    "90900000": "Cleaning and sanitation services",
    "92500000": "Library, archives, museums and other cultural services",
}


def load_cpv_divisions(log: EventLog) -> int:
    """Emit UpsertTaxonomyCode events for the embedded CPV catalogue.

    Returns the number of codes emitted (~100). Idempotent — re-running
    re-emits the same (system, code) keys; sinks MERGE.
    """
    batch_id = uuid.uuid4()
    total = 0
    t0 = time.time()
    with log.batch(batch_id, producer="load_cpv") as emit:
        # Top-level divisions: emit as 8-digit codes (eg "45000000") so
        # they share the same key namespace as detailed codes.
        for code, desc in CPV_DIVISIONS.items():
            full = code + "000000"
            emit.upsert(
                "UpsertTaxonomyCode",
                iri=f"http://data.fontem.eu/id/Cpv/{full}",
                domain="cpv",
                payload=builders.upsert_taxonomy_code(
                    system=SYSTEM, code=full,
                    label=desc, label_lang="en",
                    level=0,
                ),
            )
            total += 1
        # Detailed codes: parent_code = first 2 digits + "000000".
        for code, desc in CPV_DETAILED.items():
            emit.upsert(
                "UpsertTaxonomyCode",
                iri=f"http://data.fontem.eu/id/Cpv/{code}",
                domain="cpv",
                payload=builders.upsert_taxonomy_code(
                    system=SYSTEM, code=code,
                    label=desc, label_lang="en",
                    parent_code=code[:2] + "000000",
                    level=1,
                ),
            )
            total += 1
    elapsed = time.time() - t0
    logger.info(
        "Done: %d CPV codes (%d divisions + %d detailed) in %.1fs",
        total, len(CPV_DIVISIONS), len(CPV_DETAILED), elapsed,
    )
    return total


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Emit UpsertTaxonomyCode events for the CPV catalogue",
    )
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = EventLog.from_env()
    try:
        load_cpv_divisions(log)
    finally:
        log.close()


if __name__ == "__main__":
    main()
