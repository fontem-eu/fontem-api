"""One-shot script to apply the Phase 2 sanctions cutover Cypher.

Reads sanctions_neo4j_cutover.cypher and runs each statement.
Designed to run as a Helm pre-install/post-install hook, but
can also be invoked manually:

    python -m scripts.run_sanctions_cutover \\
        --neo4j-uri bolt://neo4j:7687

Idempotent — re-running after a successful cutover is a no-op:
the SanctionedEntity match returns 0 rows, the SANCTIONED edges
that point at SanctionRef are skipped (they don't match the
:SanctionedEntity target), and the constraint is already gone.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

CUTOVER = Path(__file__).resolve().parent / "sanctions_neo4j_cutover.cypher"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--neo4j-uri",
        default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    )
    p.add_argument(
        "--neo4j-user",
        default=os.environ.get("NEO4J_USER", "neo4j"),
    )
    p.add_argument(
        "--neo4j-password",
        default=os.environ.get("NEO4J_PASSWORD", ""),
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cypher = CUTOVER.read_text(encoding="utf-8")

    driver = GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    )
    try:
        with driver.session() as session:
            # Run each top-level statement separately. The
            # `CALL { ... } IN TRANSACTIONS` blocks need
            # `db.execute()` semantics so we split on the bare
            # semicolon followed by a blank line — keeping the
            # calls intact.
            for statement in _split_statements(cypher):
                logger.info("running:\n%s", statement.splitlines()[0])
                session.run(statement)
        logger.info("cutover complete")
    finally:
        driver.close()


def _split_statements(text: str) -> list[str]:
    """Split a cypher file on `;` at end-of-statement, preserving
    nested CALL { ... } blocks. The cypher we feed in here uses
    `};` only at block-end, never internally, so a regex-free
    string scan suffices.
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        buf.append(ch)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ";" and depth == 0:
            stmt = "".join(buf).strip()
            # Drop comment-only statements
            non_comment = "\n".join(
                ln for ln in stmt.splitlines()
                if ln.strip() and not ln.strip().startswith("//")
            )
            if non_comment:
                out.append(stmt)
            buf = []
    return out


if __name__ == "__main__":
    main()
