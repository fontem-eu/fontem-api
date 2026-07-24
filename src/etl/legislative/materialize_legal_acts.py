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
import unicodedata
from datetime import date
import re
import sys

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

MIRROR_GRAPH = "http://data.fontem.eu/graph/mirror/cellar/eu"
PAGE = 2000
_ANSWER_REF_RE = re.compile(r"^C\((\d{4})\)(\d{1,5})$")

# Two-phase paging: the celex keyset page is index-cheap (no joins, no
# aggregation); details are then fetched VALUES-bound per page. The
# original single-query sweep (GROUP BY + expression joins over the whole
# graph, repeated per page) participated in THREE prod Virtuoso crashes
# whenever any other load ran concurrently (index build 2x, walk 1x).
_SPINE_KEYS_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex
WHERE {{
  GRAPH <{graph}> {{
    ?w cdm:resource_legal_id_celex ?cx .
    BIND(STR(?cx) AS ?celex)
    FILTER(STRSTARTS(?celex, "3") && ?celex > "{after}")
  }}
}}
ORDER BY ?celex
LIMIT {page}
"""

# VALUES are typed ^^xsd:string to match the stored literals directly —
# Virtuoso 7 distinguishes plain from typed literals, and a STR() filter
# instead of a direct match defeats the index entirely (measured: 300s+
# timeout vs 1.4s for a 2000-value page on the 538k-work prod mirror).
_SPINE_DETAILS_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT (STR(?cx) AS ?celex) (SAMPLE(?d) AS ?date) (SAMPLE(?e) AS ?eli)
       (SAMPLE(?tEn) AS ?title_en) (SAMPLE(?tFr) AS ?title_fr)
       (SAMPLE(?f) AS ?in_force)
WHERE {{
  GRAPH <{graph}> {{
    VALUES ?cx {{ {values} }}
    ?w cdm:resource_legal_id_celex ?cx .
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
GROUP BY ?cx
"""

_LOOKUP_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT (STR(?cx) AS ?celex) (SAMPLE(?d) AS ?date) (SAMPLE(?t) AS ?title)
WHERE {{
  GRAPH <{graph}> {{
    VALUES ?cx {{ {values} }}
    ?w cdm:resource_legal_id_celex ?cx .
    OPTIONAL {{ ?w cdm:work_date_document ?dd . BIND(STR(?dd) AS ?d) }}
    OPTIONAL {{
      ?xe cdm:expression_belongs_to_work ?w .
      ?xe cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/ENG> .
      ?xe cdm:expression_title ?t .
    }}
  }}
}}
GROUP BY ?cx
"""

_ANSWER_REF_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT (STR(?cx) AS ?celex) (SAMPLE(STR(?dd)) AS ?date)
                (SAMPLE(?t) AS ?title)
WHERE {{
  GRAPH <{graph}> {{
    ?w cdm:work_id_document ?wid .
    FILTER(STRSTARTS(STR(?wid), "immc:{ref}/"))
    ?w cdm:resource_legal_id_celex ?cx .
    OPTIONAL {{ ?w cdm:work_date_document ?dd }}
    OPTIONAL {{
      ?xe cdm:expression_belongs_to_work ?w .
      ?xe cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/ENG> .
      ?xe cdm:expression_title ?t .
    }}
  }}
}}
GROUP BY ?cx
"""

_ANSWER_CANDIDATES_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT (STR(?cx) AS ?celex) (SAMPLE(STR(?dd)) AS ?date)
                (SAMPLE(?t) AS ?title)
WHERE {{
  GRAPH <{graph}> {{
    ?e cdm:expression_title ?t .
    ?t bif:contains "communication AND commission AND citizens AND initiative" .
    ?e cdm:expression_belongs_to_work ?w .
    ?e cdm:expression_uses_language
      <http://publications.europa.eu/resource/authority/language/ENG> .
    ?w cdm:resource_legal_id_celex ?cx .
    FILTER(STRSTARTS(STR(?cx), "5"))
    OPTIONAL {{ ?w cdm:work_date_document ?dd }}
  }}
}}
GROUP BY ?cx
LIMIT 400
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


def _norm(text: str) -> str:
    """Casefold, strip diacritics/punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


_TOKEN_STOP = frozenset(
    {"the", "a", "an", "of", "for", "in", "on", "and", "to", "eu", "european"})


def _tokens(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in _TOKEN_STOP}


# The Commission answers ECIs with a communication titled on this fixed
# institutional pattern; matching is restricted to that document class so
# title matching never roams the open corpus.
ANSWER_CLASS_RE = re.compile(
    r"^\s*communication from the commission on the european "
    r"citizens.{0,3}initiative", re.I)

#: |work_date_document - register answered_date| ceiling for a title match.
ANSWER_DATE_TOLERANCE_DAYS = 45


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def match_answers(petitions: list[dict], candidates: dict) -> dict:
    """Assign answer communications to ANSWERED petitions, fail-closed.

    ``candidates`` maps celex -> {"title": str, "date": str}. Tiers:
    T1 exact normalized-substring, T2 token containment. A petition links
    only when its tier yields EXACTLY one candidate AND the candidate's
    document date agrees with the register's answered_date within
    ANSWER_DATE_TOLERANCE_DAYS. Assignments must be injective: a candidate
    claimed by two petitions drops both (validated empirically 2026-07-24:
    10/10 real T1 pairs linked at delta <= 30d; the one near-miss —
    register "cultures" vs title "culture" — was correctly refused).
    Returns pid -> (celex, tier, delta_days).
    """
    picked: dict = {}
    for pet in petitions:
        title = pet.get("title") or ""
        norm_title = _norm(title)
        toks = _tokens(title)
        hits = [cx for cx, c in candidates.items()
                if norm_title and norm_title in _norm(c["title"])]
        tier = "title-substring"
        if not hits:
            hits = [cx for cx, c in candidates.items()
                    if toks and toks <= set(_norm(c["title"]).split())]
            tier = "title-tokens"
        if len(hits) != 1:
            continue
        answered = _parse_date(pet.get("answered_date"))
        doc_date = _parse_date(candidates[hits[0]].get("date"))
        if not answered or not doc_date:
            continue
        delta = abs((answered - doc_date).days)
        if delta > ANSWER_DATE_TOLERANCE_DAYS:
            continue
        picked[pet["pid"]] = (hits[0], tier, delta)

    claims: dict = {}
    for pid, (celex, _, _) in picked.items():
        claims.setdefault(celex, []).append(pid)
    return {pid: v for pid, v in picked.items()
            if len(claims[v[0]]) == 1}


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
        keys = sparql(endpoint, _SPINE_KEYS_QUERY.format(
            graph=MIRROR_GRAPH, after=after, page=PAGE))
        if not keys:
            break
        values = " ".join(f'"{k["celex"]}"^^xsd:string' for k in keys)
        rows = sparql(endpoint, _SPINE_DETAILS_QUERY.format(
            graph=MIRROR_GRAPH, values=values))
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
        after = keys[-1]["celex"]
        logger.info("spine: %d nodes (through %s)", total, after)
        if len(keys) < PAGE:
            break
    return total


def resolve_answers(endpoint: str, petitions: list[dict]) -> dict:
    """Three-tier answer resolution against the mirror, fail-closed.

    T0: exact join on cdm:work_id_document via the register's C-number —
    deterministic; the C(YYYY)NNNN ref is the Commission's internal id
    and does NOT map arithmetically to a CELEX (Fur Free Europe:
    C(2023)8362 is 52023XC01559, not 52023DC8362).
    T1/T2 (match_answers): title matching inside the fixed answer-
    communication class, gated on uniqueness, injectivity and register/
    mirror date agreement. Returns pid -> (celex, tier, delta, row).
    """
    answered = [p for p in petitions if p.get("status") == "ANSWERED"]
    resolved: dict = {}
    for pet in answered:
        for ref in pet.get("refs") or []:
            if not _ANSWER_REF_RE.match(ref or ""):
                continue
            rows = sparql(endpoint, _ANSWER_REF_QUERY.format(
                graph=MIRROR_GRAPH, ref=ref))
            if len(rows) == 1:
                resolved[pet["pid"]] = (rows[0]["celex"], "ref-exact", 0,
                                        rows[0])
                break

    remaining = [p for p in answered if p["pid"] not in resolved]
    if remaining:
        cand_rows = sparql(
            endpoint, _ANSWER_CANDIDATES_QUERY.format(graph=MIRROR_GRAPH))
        candidates = {r["celex"]: r for r in cand_rows
                      if ANSWER_CLASS_RE.match(r.get("title") or "")}
        for pid, (celex, tier, delta) in match_answers(
                remaining, candidates).items():
            resolved[pid] = (celex, tier, delta, candidates[celex])
    return resolved


def link_petitions(endpoint: str, driver) -> dict:  # pylint: disable=too-many-locals
    """Resolve petition CELEX refs in the mirror; edge only when found."""
    with driver.session() as session:
        petitions = session.run(
            "MATCH (p:Petition) "
            "RETURN p.system AS system, p.petition_id AS pid, "
            "       p.title AS title, p.status AS status, "
            "       p.answered_date AS answered_date, "
            "       p.registration_decision_celex AS reg, "
            "       p.answer_refs AS refs"
        ).data()
    wanted: dict[str, list[tuple]] = {}
    for p in petitions:
        if p.get("reg"):
            wanted.setdefault(p["reg"], []).append(
                (p["system"], p["pid"], "REGISTERED_BY", "celex", None))

    answers = resolve_answers(endpoint, petitions)
    by_pid = {p["pid"]: p for p in petitions}
    answer_rows = {}
    for pid, (celex, tier, delta, row) in answers.items():
        p = by_pid[pid]
        wanted.setdefault(celex, []).append(
            (p["system"], pid, "ANSWERED_BY", tier, delta))
        answer_rows[celex] = row
    if not wanted:
        return {"petitions": len(petitions), "resolved": 0, "edges": 0}

    reg_celexes = sorted(set(wanted) - set(answer_rows))
    found = dict(answer_rows)
    if reg_celexes:
        values = " ".join(f'"{c}"^^xsd:string' for c in reg_celexes)
        found.update({r["celex"]: r for r in sparql(
            endpoint, _LOOKUP_QUERY.format(graph=MIRROR_GRAPH,
                                           values=values))})

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
            for system, pid, rel, matched, delta in wanted.get(celex, []):
                session.run(
                    f"MATCH (p:Petition {{system: $system, petition_id: $pid}}) "
                    f"MATCH (a:LegalAct {{celex: $celex}}) "
                    f"MERGE (p)-[r:{rel}]->(a) "
                    f"SET r.provenance = 'eci-register', "
                    f"    r.matched = $matched, "
                    f"    r.date_delta_days = $delta",
                    system=system, pid=pid, celex=celex,
                    matched=matched, delta=delta,
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
