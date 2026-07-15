"""Materialize the CELEX legal-act spine from the Virtuoso mirror into Neo4j.

P1+P2 of the petitions plan. Reverses the earlier neo4j-materializer
deferral (owner decision 2026-07-14): petitions live in the graph, and
their point is the link to legislative outcomes, so a traversable
``:LegalAct`` spine is required.

Per the original architecture decision the materializer WRITES NEO4J
DIRECTLY — no event round-trip ("the materializer can write them
itself"). It is a projection of the mirror, rebuildable at any time;
the mirror stays the source of truth.

Two passes, one daily run (after the mirror delta):
  1. spine  — sector-3 works (legal acts) → MERGE (:LegalAct {celex})
              with eli / doc type / dates / EN+FR titles. Idempotent
              keyset sweep; a full pass is cheap enough to run daily.
  2. links  — for every :Petition, resolve registration_decision_celex
              and answer_refs (C(YYYY)N → candidate sector-5 CELEX) in
              the mirror; where found, MERGE the :LegalAct and the
              (:Petition)-[:REGISTERED_BY|:ANSWERED_BY]->(:LegalAct)
              edge, provenance on the edge. Refs that don't resolve
              stay as node properties — never a dangling edge.

Usage:
    python -m src.etl.legislative.materialize_legal_acts            # both passes
    python -m src.etl.legislative.materialize_legal_acts --spine-only
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

MIRROR_GRAPH = "http://data.fontem.eu/graph/mirror/cellar/eu"
PAGE = 2000
_ANSWER_REF_RE = re.compile(r"^C\((\d{4})\)(\d{1,5})$")

_SPINE_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?celex (SAMPLE(?d) AS ?date) (SAMPLE(?e) AS ?eli)
       (SAMPLE(?tEn) AS ?title_en) (SAMPLE(?tFr) AS ?title_fr)
       (SAMPLE(?f) AS ?in_force)
WHERE {{
  GRAPH <{graph}> {{
    ?w cdm:resource_legal_id_celex ?cx .
    BIND(STR(?cx) AS ?celex)
    FILTER(STRSTARTS(?celex, "3") && ?celex > "{after}")
    OPTIONAL {{ ?w cdm:work_date_document ?dd . BIND(STR(?dd) AS ?d) }}
    OPTIONAL {{ ?w cdm:resource_legal_eli ?el . BIND(STR(?el) AS ?e) }}
    OPTIONAL {{ ?w cdm:resource_legal_in-force ?ff . BIND(STR(?ff) AS ?f) }}
    OPTIONAL {{
      ?xe cdm:expression_belongs_to_work ?w .
      ?xe cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/ENG> .
      ?xe cdm:expression_title ?tEn .
    }}
    OPTIONAL {{
      ?xf cdm:expression_belongs_to_work ?w .
      ?xf cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/FRA> .
      ?xf cdm:expression_title ?tFr .
    }}
  }}
}}
GROUP BY ?celex
ORDER BY ?celex
LIMIT {page}
"""

_LOOKUP_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?celex (SAMPLE(?d) AS ?date) (SAMPLE(?t) AS ?title)
WHERE {{
  GRAPH <{graph}> {{
    ?w cdm:resource_legal_id_celex ?cx .
    BIND(STR(?cx) AS ?celex)
    VALUES ?celex {{ {values} }}
    OPTIONAL {{ ?w cdm:work_date_document ?dd . BIND(STR(?dd) AS ?d) }}
    OPTIONAL {{
      ?xe cdm:expression_belongs_to_work ?w .
      ?xe cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/ENG> .
      ?xe cdm:expression_title ?t .
    }}
  }}
}}
GROUP BY ?celex
"""

_CELEX_SECTOR_3 = {
    "L": "Directive", "R": "Regulation", "D": "Decision",
    "H": "Recommendation", "A": "Opinion", "C": "Declaration",
}


def doc_type(celex: str) -> str:
    """Best-effort human label from the CELEX sector/type code."""
    if celex.startswith("3"):
        return _CELEX_SECTOR_3.get(celex[5:6], "Legal act")
    if celex.startswith("5"):
        return "Preparatory act"
    return "Legal document"


def answer_ref_to_celex(ref: str) -> str | None:
    """C(2026)4110 → candidate CELEX 52026DC4110 (Commission C-doc)."""
    m = _ANSWER_REF_RE.match(ref or "")
    if not m:
        return None
    return f"5{m.group(1)}DC{int(m.group(2)):04d}"


def sparql(endpoint: str, query: str) -> list[dict]:
    """SELECT against the mirror; unwraps bindings to plain strings.

    Timeout defaults high: prod-sized spine pages (keyset GROUP BY over
    ~500k works with title joins) run minutes when Virtuoso is busy —
    e.g. while the full-text index build churns. 120s produced
    ReadTimeouts on the first prod run.
    """
    timeout = float(os.environ.get("MATERIALIZER_SPARQL_TIMEOUT", "600"))
    resp = httpx.post(
        endpoint, data={"query": query,
                        "format": "application/sparql-results+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    out = []
    for b in resp.json()["results"]["bindings"]:
        out.append({k: v.get("value") for k, v in b.items()})
    return out


def materialize_spine(endpoint: str, driver) -> int:
    """Keyset-sweep sector-3 works into :LegalAct nodes."""
    with driver.session() as session:
        session.run(
            "CREATE CONSTRAINT legal_act_celex IF NOT EXISTS "
            "FOR (a:LegalAct) REQUIRE a.celex IS UNIQUE"
        )
    total = 0
    after = ""
    while True:
        rows = sparql(endpoint, _SPINE_QUERY.format(
            graph=MIRROR_GRAPH, after=after, page=PAGE))
        if not rows:
            break
        batch = [{
            "celex": r["celex"],
            "date_document": r.get("date"),
            "eli": r.get("eli"),
            "in_force": r.get("in_force"),
            "title_en": r.get("title_en"),
            "title_fr": r.get("title_fr"),
            "doc_type": doc_type(r["celex"]),
        } for r in rows]
        with driver.session() as session:
            session.run(
                "UNWIND $batch AS row "
                "MERGE (a:LegalAct {celex: row.celex}) "
                "SET a.date_document = row.date_document, "
                "    a.eli = row.eli, a.in_force = row.in_force, "
                "    a.doc_type = row.doc_type, "
                "    a.title_en = coalesce(row.title_en, a.title_en), "
                "    a.title_fr = coalesce(row.title_fr, a.title_fr), "
                "    a.source = 'cellar-mirror'",
                batch=batch,
            )
        total += len(rows)
        after = rows[-1]["celex"]
        logger.info("spine: %d nodes (through %s)", total, after)
        if len(rows) < PAGE:
            break
    return total


def link_petitions(endpoint: str, driver) -> dict:  # pylint: disable=too-many-locals
    """Resolve petition CELEX refs in the mirror; edge only when found."""
    with driver.session() as session:
        petitions = session.run(
            "MATCH (p:Petition) "
            "RETURN p.system AS system, p.petition_id AS pid, "
            "       p.registration_decision_celex AS reg, "
            "       p.answer_refs AS refs"
        ).data()
    wanted: dict[str, list[tuple]] = {}
    for p in petitions:
        if p.get("reg"):
            wanted.setdefault(p["reg"], []).append(
                (p["system"], p["pid"], "REGISTERED_BY"))
        for ref in p.get("refs") or []:
            if cx := answer_ref_to_celex(ref):
                wanted.setdefault(cx, []).append(
                    (p["system"], p["pid"], "ANSWERED_BY"))
    if not wanted:
        return {"petitions": len(petitions), "resolved": 0, "edges": 0}

    values = " ".join(f'"{c}"' for c in sorted(wanted))
    found = {r["celex"]: r for r in sparql(
        endpoint, _LOOKUP_QUERY.format(graph=MIRROR_GRAPH, values=values))}

    edges = 0
    with driver.session() as session:
        for celex, row in found.items():
            session.run(
                "MERGE (a:LegalAct {celex: $celex}) "
                "ON CREATE SET a.title_en = $title, "
                "  a.date_document = $date, a.doc_type = $doc_type, "
                "  a.source = 'cellar-mirror'",
                celex=celex, title=row.get("title"),
                date=row.get("date"), doc_type=doc_type(celex),
            )
            for system, pid, rel in wanted[celex]:
                session.run(
                    f"MATCH (p:Petition {{system: $system, petition_id: $pid}}) "
                    f"MATCH (a:LegalAct {{celex: $celex}}) "
                    f"MERGE (p)-[r:{rel}]->(a) "
                    f"SET r.provenance = 'eci-register', "
                    f"    r.matched = 'celex'",
                    system=system, pid=pid, celex=celex,
                )
                edges += 1
    unresolved = sorted(set(wanted) - set(found))
    if unresolved:
        logger.info("unresolved CELEX refs (mirror coverage): %s",
                    ", ".join(unresolved[:10]))
    return {"petitions": len(petitions), "resolved": len(found),
            "edges": edges, "unresolved": len(unresolved)}


def main(argv=None):
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Materialize the CELEX spine + petition links")
    parser.add_argument("--spine-only", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    endpoint = os.environ.get("VIRTUOSO_SPARQL_URL")
    if not endpoint:
        logger.error("VIRTUOSO_SPARQL_URL is required")
        return 1
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI"),
        auth=(os.environ.get("NEO4J_USER"),
              os.environ.get("NEO4J_PASSWORD")),
    )
    try:
        n = materialize_spine(endpoint, driver)
        logger.info("spine complete: %d acts", n)
        if not args.spine_only:
            stats = link_petitions(endpoint, driver)
            logger.info("links: %s", stats)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
