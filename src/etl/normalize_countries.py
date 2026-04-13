"""
Country Code Normalization → ISO alpha-3
==========================================
Migrates all Company, Authority, and Contract nodes from ISO alpha-2
to ISO alpha-3 country codes using pycountry.

Usage:
    python -m src.etl.normalize_countries --neo4j-uri bolt://localhost:7687
"""
from __future__ import annotations

import argparse
import logging
import os
import time

import pycountry
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# Build alpha-2 → alpha-3 mapping from pycountry (249 countries)
_A2_TO_A3: dict[str, str] = {}
for _c in pycountry.countries:
    _A2_TO_A3[_c.alpha_2] = _c.alpha_3


def alpha2_to_alpha3(code: str) -> str:
    """Convert an ISO alpha-2 code to alpha-3. Returns input if already alpha-3 or unknown."""
    if len(code) == 3:
        return code  # already alpha-3
    return _A2_TO_A3.get(code, code)


def normalize_graph(driver):
    """Normalize all alpha-2 country codes to alpha-3 in the graph.

    Processes one country code at a time to avoid transaction memory limits.
    """
    t0 = time.time()
    total = {"Company": 0, "Authority": 0, "Contract": 0}

    for a2, a3 in _A2_TO_A3.items():
        with driver.session() as session:
            for label in ["Company", "Authority", "Contract"]:
                result = session.run(
                    f"MATCH (n:{label} {{country: $a2}}) "
                    f"SET n.country = $a3 "
                    f"RETURN count(n) AS updated",
                    a2=a2, a3=a3,
                ).single()
                updated = result["updated"]
                if updated > 0:
                    total[label] += updated
                    logger.info(
                        "  %s %s → %s: %d nodes", label, a2, a3, updated,
                    )

    elapsed = time.time() - t0
    for label, count in total.items():
        logger.info("%s normalized: %d", label, count)
    logger.info("Done in %.1fs", elapsed)
    return total


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Normalize country codes to ISO alpha-3",
    )
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    try:
        normalize_graph(driver)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
