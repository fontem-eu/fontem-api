"""One-shot bridge: lift bulk reference data out of Neo4j into Virtuoso.

Target audience: the Phase 3 cutover. Each ETL still writes to Neo4j
today. This script stages the parity migration by mapping live Neo4j
state into Turtle and pushing it into per-domain named graphs in
Virtuoso. Once read paths flip (gmr-api, gmr-community-api), the
loaders themselves get rewritten to skip Neo4j entirely and Neo4j
can come down.

Run order (smallest first):

    python -m scripts.migrate_neo4j_to_virtuoso cpv
    python -m scripts.migrate_neo4j_to_virtuoso nuts
    python -m scripts.migrate_neo4j_to_virtuoso listing
    python -m scripts.migrate_neo4j_to_virtuoso cohesion
    python -m scripts.migrate_neo4j_to_virtuoso authority
    python -m scripts.migrate_neo4j_to_virtuoso lobbyist
    python -m scripts.migrate_neo4j_to_virtuoso company-lei      # GLEIF only
    python -m scripts.migrate_neo4j_to_virtuoso contract --since 2026-03-01

Each class mapper:
  * Cypher fetch — yields rows, paged for memory predictability
  * Turtle render — pure function (rows -> Turtle string)
  * Target graph — per-domain named graph URI

The mapper registry lives at the bottom of this file.

PUT semantics: each run *replaces* the target named graph. That makes
re-runs idempotent and lets us re-bridge a class after fixes without
hand-cleaning. PUT goes via Virtuoso's /sparql-graph-crud-auth
endpoint (same as the sanctions writer).
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable, Iterator

import httpx
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


# ── Shared Turtle helpers ─────────────────────────────────────────

PREAMBLE = """\
@prefix fontem:    <http://data.fontem.eu/ontology#> .
@prefix fontem-id: <http://data.fontem.eu/id/> .
@prefix rdf:       <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:      <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos:      <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd:       <http://www.w3.org/2001/XMLSchema#> .
@prefix prov:      <http://www.w3.org/ns/prov#> .
@prefix wdt:       <http://www.wikidata.org/prop/direct/> .
"""


def esc(s: str) -> str:
    """Escape a string literal for Turtle."""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
    )


def lit(s: str | None, *, lang: str | None = None) -> str | None:
    """Render a string value as a Turtle literal. Returns None if blank."""
    if s is None or str(s).strip() == "":
        return None
    out = '"' + esc(str(s)) + '"'
    if lang:
        out += "@" + lang
    return out


def date_lit(s: str | None) -> str | None:
    """Render a YYYY-MM-DD string as xsd:date. None on bad/missing input."""
    if not s:
        return None
    try:
        datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
    return f'"{str(s)[:10]}"^^xsd:date'


def int_lit(n: int | None) -> str | None:
    if n is None:
        return None
    return f'"{int(n)}"^^xsd:integer'


def decimal_lit(n: float | int | None) -> str | None:
    if n is None:
        return None
    return f'"{n}"^^xsd:decimal'


def emit(iri: str, *triples: tuple[str, str | None]) -> str:
    """Render a subject + (predicate, object) pairs into a Turtle block.
    Drops triples whose object is None.
    """
    lines = [f"<{iri}>"]
    kept = [(p, o) for (p, o) in triples if o is not None]
    if not kept:
        return ""
    for i, (p, o) in enumerate(kept):
        sep = " ;" if i < len(kept) - 1 else " ."
        lines.append(f"    {p} {o}{sep}")
    return "\n".join(lines)


# ── Class mappers ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Mapper:
    name: str
    target_graph: str
    cypher: str
    render: Callable[[dict], str]
    page_size: int = 5000


# CPV ── procurement vocabulary
def render_cpv(row: dict) -> str:
    code = row["code"]
    iri = f"http://data.fontem.eu/id/CPV/{code}"
    return emit(
        iri,
        ("a", "fontem:CPV"),
        ("fontem:code", lit(code)),
        ("rdfs:label", lit(row.get("description"), lang="en")),
        ("fontem:cpvDivision", lit(row.get("division"))),
    )


CPV = Mapper(
    name="cpv",
    target_graph="http://data.fontem.eu/graph/cpv",
    cypher="MATCH (n:CPV) RETURN n.code AS code, n.description AS description, n.division AS division",
    render=render_cpv,
)


# NUTS ── geographic regions
def render_nuts(row: dict) -> str:
    code = row["code"]
    iri = f"http://data.fontem.eu/id/NUTSRegion/{code}"
    return emit(
        iri,
        ("a", "fontem:NUTSRegion"),
        ("fontem:code", lit(code)),
        ("rdfs:label", lit(row.get("name"), lang="en")),
        ("fontem:nutsLevel", int_lit(row.get("level"))),
        ("fontem:countryAlpha3", lit(row.get("country_alpha3"))),
    )


NUTS = Mapper(
    name="nuts",
    target_graph="http://data.fontem.eu/graph/nuts",
    cypher="MATCH (n:NUTSRegion) RETURN n.code AS code, n.name AS name, n.level AS level, n.country_alpha3 AS country_alpha3",
    render=render_nuts,
)


# Listing ── stock-exchange tickers
def render_listing(row: dict) -> str:
    ticker = row["ticker"]
    exchange = row.get("exchange") or "UNKNOWN"
    iri = f"http://data.fontem.eu/id/Listing/{exchange}-{ticker}"
    return emit(
        iri,
        ("a", "fontem:Listing"),
        ("fontem:ticker", lit(ticker)),
        ("fontem:exchange", lit(exchange)),
        ("fontem:currency", lit(row.get("currency"))),
        ("fontem:active", "true" if row.get("active") else "false"),
    )


LISTING = Mapper(
    name="listing",
    target_graph="http://data.fontem.eu/graph/listing",
    cypher="MATCH (n:Listing) RETURN n.ticker AS ticker, n.exchange AS exchange, n.currency AS currency, n.active AS active",
    render=render_listing,
)


# CohesionProject ── EU funded projects
def render_cohesion(row: dict) -> str:
    pid = row["project_id"]
    iri = f"http://data.fontem.eu/id/CohesionProject/{pid}"
    return emit(
        iri,
        ("a", "fontem:CohesionProject"),
        ("fontem:projectId", lit(pid)),
        ("rdfs:label", lit(row.get("title"), lang="en")),
        ("fontem:description", lit(row.get("description"), lang="en")),
        ("fontem:fund", lit(row.get("fund"))),
        ("fontem:programme", lit(row.get("programme"))),
        ("fontem:totalBudget", decimal_lit(row.get("total_budget"))),
        ("fontem:euContribution", decimal_lit(row.get("eu_contribution"))),
        ("fontem:startDate", date_lit(row.get("start_date"))),
        ("fontem:endDate", date_lit(row.get("end_date"))),
        ("fontem:nutsCode", lit(row.get("nuts_code"))),
        ("wdt:P17", lit(row.get("country"))),
        ("fontem:wikibaseQid", lit(row.get("wikibase_qid"))),
    )


COHESION = Mapper(
    name="cohesion",
    target_graph="http://data.fontem.eu/graph/cohesion",
    cypher=(
        "MATCH (n:CohesionProject) RETURN n.project_id AS project_id, "
        "n.title AS title, n.description AS description, "
        "n.fund AS fund, n.programme AS programme, "
        "n.total_budget AS total_budget, n.eu_contribution AS eu_contribution, "
        "n.start_date AS start_date, n.end_date AS end_date, "
        "n.nuts_code AS nuts_code, n.country AS country, "
        "n.wikibase_qid AS wikibase_qid"
    ),
    render=render_cohesion,
)


# Authority ── procurement contracting authorities
def render_authority(row: dict) -> str:
    aid = row["authority_id"]
    iri = f"http://data.fontem.eu/id/Authority/{aid}"
    return emit(
        iri,
        ("a", "fontem:Authority"),
        ("fontem:authorityId", lit(aid)),
        ("rdfs:label", lit(row.get("name"), lang="en")),
        ("wdt:P17", lit(row.get("country"))),
    )


AUTHORITY = Mapper(
    name="authority",
    target_graph="http://data.fontem.eu/graph/authority",
    cypher="MATCH (n:Authority) RETURN n.authority_id AS authority_id, n.name AS name, n.country AS country",
    render=render_authority,
)


# Lobbyist ── EU transparency register
def render_lobbyist(row: dict) -> str:
    tr_id = row["tr_id"]
    iri = f"http://data.fontem.eu/id/Lobbyist/{tr_id}"
    return emit(
        iri,
        ("a", "fontem:Lobbyist"),
        ("fontem:trId", lit(tr_id)),
        ("rdfs:label", lit(row.get("name"), lang="en")),
        ("fontem:acronym", lit(row.get("acronym"))),
        ("wdt:P17", lit(row.get("country"))),
        ("fontem:city", lit(row.get("city"))),
        ("fontem:website", lit(row.get("website"))),
        ("fontem:category", lit(row.get("category"))),
        ("fontem:entityForm", lit(row.get("entity_form"))),
        ("fontem:goals", lit(row.get("goals"), lang="en")),
        ("fontem:membersFte", decimal_lit(row.get("members_fte"))),
        ("fontem:costMin", decimal_lit(row.get("cost_min"))),
        ("fontem:costMax", decimal_lit(row.get("cost_max"))),
        ("fontem:epPasses", int_lit(row.get("ep_passes"))),
        ("fontem:registrationDate", date_lit(row.get("registration_date"))),
        ("fontem:lastUpdated", date_lit(row.get("last_updated"))),
    )


LOBBYIST = Mapper(
    name="lobbyist",
    target_graph="http://data.fontem.eu/graph/lobbyist",
    cypher=(
        "MATCH (n:Lobbyist) RETURN n.tr_id AS tr_id, n.name AS name, "
        "n.acronym AS acronym, n.country AS country, n.city AS city, "
        "n.website AS website, n.category AS category, "
        "n.entity_form AS entity_form, n.goals AS goals, "
        "n.members_fte AS members_fte, n.cost_min AS cost_min, "
        "n.cost_max AS cost_max, n.ep_passes AS ep_passes, "
        "n.registration_date AS registration_date, "
        "n.last_updated AS last_updated"
    ),
    render=render_lobbyist,
)


# Company (LEI-bearing only — GLEIF-sourced) ──
# Field set matches the GLEIF loader's MERGE: gmr_id, lei, name,
# country, postal_code, legal_form, active. The earlier draft
# referenced registered_* / status which the loader doesn't
# write — those came back null and got dropped silently.
def render_company_lei(row: dict) -> str:
    gmr_id = row["gmr_id"]
    iri = f"http://data.fontem.eu/id/Company/{gmr_id}"
    active = row.get("active")
    return emit(
        iri,
        ("a", "fontem:Company"),
        ("fontem:gmrId", lit(gmr_id)),
        ("rdfs:label", lit(row.get("name"), lang="en")),
        ("fontem:lei", lit(row.get("lei"))),
        ("wdt:P17", lit(row.get("country"))),
        ("fontem:legalForm", lit(row.get("legal_form"))),
        ("fontem:postalCode", lit(row.get("postal_code"))),
        ("fontem:active", "true" if active else ("false" if active is False else None)),
    )


COMPANY_LEI = Mapper(
    name="company-lei",
    target_graph="http://data.fontem.eu/graph/company",
    cypher=(
        "MATCH (n:Company) WHERE n.lei IS NOT NULL "
        "RETURN n.gmr_id AS gmr_id, n.name AS name, n.lei AS lei, "
        "n.country AS country, n.legal_form AS legal_form, "
        "n.postal_code AS postal_code, n.active AS active"
    ),
    render=render_company_lei,
    page_size=10000,
)


# Contract (with --since cutoff)
def render_contract(row: dict) -> str:
    cid = row["ted_notice_id"]
    iri = f"http://data.fontem.eu/id/Contract/{cid}"
    return emit(
        iri,
        ("a", "fontem:Contract"),
        ("fontem:tedNoticeId", lit(cid)),
        ("rdfs:label", lit(row.get("title"), lang="en")),
        ("fontem:description", lit(row.get("description"), lang="en")),
        ("fontem:procedureType", lit(row.get("procedure_type"))),
        ("fontem:noticeType", lit(row.get("notice_type"))),
        ("fontem:cpvMain", lit(row.get("cpv_main"))),
        ("fontem:valueOriginal", decimal_lit(row.get("value_original"))),
        ("fontem:valueCurrency", lit(row.get("value_currency"))),
        ("fontem:valueUndisclosed", "true" if row.get("value_undisclosed") else "false"),
        ("fontem:publicationDate", date_lit(row.get("publication_date"))),
        ("fontem:awardDate", date_lit(row.get("award_date"))),
        ("wdt:P17", lit(row.get("country"))),
        ("fontem:tedUrl", lit(row.get("ted_url"))),
    )


CONTRACT = Mapper(
    name="contract",
    target_graph="http://data.fontem.eu/graph/contract",
    cypher=(
        "MATCH (n:Contract) WHERE n.publication_date >= $since "
        "RETURN n.ted_notice_id AS ted_notice_id, n.title AS title, "
        "n.description AS description, n.procedure_type AS procedure_type, "
        "n.notice_type AS notice_type, n.cpv_main AS cpv_main, "
        "n.value_original AS value_original, n.value_currency AS value_currency, "
        "n.value_undisclosed AS value_undisclosed, "
        "n.publication_date AS publication_date, n.award_date AS award_date, "
        "n.country AS country, n.ted_url AS ted_url"
    ),
    render=render_contract,
    page_size=2000,
)


# ── Relationship mappers ──────────────────────────────────────────
#
# Each rel mapper renders rows of `(subject_iri, object_iri)` plus
# (optional) edge properties into Turtle. Subject and object IRIs
# are built from the mapper's iri_template; the predicate is fixed
# per mapper.

# Authority -AWARDED-> Contract  (post-cutoff only)
def render_rel_authority_awarded(row: dict) -> str:
    a = row.get("authority_id"); c = row.get("ted_notice_id")
    if not a or not c:
        return ""
    return f'<http://data.fontem.eu/id/Authority/{a}> fontem:awarded <http://data.fontem.eu/id/Contract/{c}> .'


REL_AUTHORITY_AWARDED = Mapper(
    name="rel-authority-awarded",
    target_graph="http://data.fontem.eu/graph/contract-edges",
    cypher=(
        "MATCH (a:Authority)-[:AWARDED]->(c:Contract) "
        "WHERE c.publication_date >= $since "
        "RETURN a.authority_id AS authority_id, c.ted_notice_id AS ted_notice_id"
    ),
    render=render_rel_authority_awarded,
    page_size=10000,
)


# Contract -AWARDED_TO-> Company  (post-cutoff only)
def render_rel_contract_awarded_to(row: dict) -> str:
    c = row.get("ted_notice_id"); co = row.get("gmr_id")
    if not c or not co:
        return ""
    return f'<http://data.fontem.eu/id/Contract/{c}> fontem:awardedTo <http://data.fontem.eu/id/Company/{co}> .'


REL_CONTRACT_AWARDED_TO = Mapper(
    name="rel-contract-awarded-to",
    target_graph="http://data.fontem.eu/graph/contract-edges",
    cypher=(
        "MATCH (c:Contract)-[:AWARDED_TO]->(co:Company) "
        "WHERE c.publication_date >= $since "
        "RETURN c.ted_notice_id AS ted_notice_id, co.gmr_id AS gmr_id"
    ),
    render=render_rel_contract_awarded_to,
    page_size=10000,
)


# Contract -CATEGORIZED_AS-> CPV  (post-cutoff only)
def render_rel_contract_cpv(row: dict) -> str:
    c = row.get("ted_notice_id"); code = row.get("code")
    if not c or not code:
        return ""
    return f'<http://data.fontem.eu/id/Contract/{c}> fontem:cpv <http://data.fontem.eu/id/CPV/{code}> .'


REL_CONTRACT_CPV = Mapper(
    name="rel-contract-cpv",
    target_graph="http://data.fontem.eu/graph/contract-edges",
    cypher=(
        "MATCH (c:Contract)-[:CATEGORIZED_AS]->(cpv:CPV) "
        "WHERE c.publication_date >= $since "
        "RETURN c.ted_notice_id AS ted_notice_id, cpv.code AS code"
    ),
    render=render_rel_contract_cpv,
    page_size=10000,
)


# Company-LEI -LOCATED_IN-> NUTSRegion  (~1.88M rows)
def render_rel_company_located(row: dict) -> str:
    co = row.get("gmr_id"); n = row.get("code")
    if not co or not n:
        return ""
    return f'<http://data.fontem.eu/id/Company/{co}> fontem:locatedIn <http://data.fontem.eu/id/NUTSRegion/{n}> .'


REL_COMPANY_LOCATED = Mapper(
    name="rel-company-located",
    target_graph="http://data.fontem.eu/graph/company-edges",
    cypher=(
        "MATCH (co:Company)-[:LOCATED_IN]->(n:NUTSRegion) "
        "WHERE co.lei IS NOT NULL "
        "RETURN co.gmr_id AS gmr_id, n.code AS code"
    ),
    render=render_rel_company_located,
    page_size=20000,
)


# NUTSRegion -PART_OF-> NUTSRegion  (hierarchy)
def render_rel_nuts_partof(row: dict) -> str:
    child = row.get("child"); parent = row.get("parent")
    if not child or not parent:
        return ""
    return (
        f'<http://data.fontem.eu/id/NUTSRegion/{child}> '
        f'fontem:partOf <http://data.fontem.eu/id/NUTSRegion/{parent}> .'
    )


REL_NUTS_PARTOF = Mapper(
    name="rel-nuts-partof",
    target_graph="http://data.fontem.eu/graph/nuts",
    cypher="MATCH (c:NUTSRegion)-[:PART_OF]->(p:NUTSRegion) RETURN c.code AS child, p.code AS parent",
    render=render_rel_nuts_partof,
    page_size=10000,
)


# Company-LEI -LISTED_AS-> Listing
def render_rel_company_listing(row: dict) -> str:
    co = row.get("gmr_id"); ex = row.get("exchange") or "UNKNOWN"; tk = row.get("ticker")
    if not co or not tk:
        return ""
    return (
        f'<http://data.fontem.eu/id/Company/{co}> '
        f'fontem:listedAs <http://data.fontem.eu/id/Listing/{ex}-{tk}> .'
    )


REL_COMPANY_LISTING = Mapper(
    name="rel-company-listing",
    target_graph="http://data.fontem.eu/graph/company-edges",
    cypher=(
        "MATCH (co:Company)-[:LISTED_AS]->(l:Listing) "
        "WHERE co.lei IS NOT NULL "
        "RETURN co.gmr_id AS gmr_id, l.exchange AS exchange, l.ticker AS ticker"
    ),
    render=render_rel_company_listing,
    page_size=10000,
)


# Company -BENEFICIARY_OF-> CohesionProject (small)
def render_rel_cohesion_beneficiary(row: dict) -> str:
    co = row.get("gmr_id"); pid = row.get("project_id")
    if not co or not pid:
        return ""
    return (
        f'<http://data.fontem.eu/id/Company/{co}> '
        f'fontem:beneficiaryOf <http://data.fontem.eu/id/CohesionProject/{pid}> .'
    )


REL_COHESION_BENEFICIARY = Mapper(
    name="rel-cohesion-beneficiary",
    target_graph="http://data.fontem.eu/graph/cohesion",
    cypher=(
        "MATCH (co:Company)-[:BENEFICIARY_OF]->(p:CohesionProject) "
        "RETURN co.gmr_id AS gmr_id, p.project_id AS project_id"
    ),
    render=render_rel_cohesion_beneficiary,
    page_size=10000,
)


# Lobbyist -INTERESTED_IN-> LobbyInterest (single shared interest stub
# nodes — rendering both the interest body + the edge in one pass)
def render_rel_lobbyist_interest(row: dict) -> str:
    tr_id = row.get("tr_id"); interest = row.get("topic") or row.get("name")
    if not tr_id or not interest:
        return ""
    # LobbyInterest IRI built from the topic name (stable hash). The
    # Neo4j data has at most ~40 distinct interests so collisions
    # are not a worry.
    import re
    slug = re.sub(r'\W+', '-', interest.strip().lower()).strip('-')[:64] or "unknown"
    interest_iri = f"http://data.fontem.eu/id/LobbyInterest/{slug}"
    return (
        f'<{interest_iri}> a fontem:LobbyInterest ;\n'
        f'    rdfs:label {_quoted_inline(interest)}@en .\n'
        f'<http://data.fontem.eu/id/Lobbyist/{tr_id}> '
        f'fontem:interestedIn <{interest_iri}> .'
    )


def _quoted_inline(s: str) -> str:
    return '"' + esc(s) + '"'


REL_LOBBYIST_INTEREST = Mapper(
    name="rel-lobbyist-interest",
    target_graph="http://data.fontem.eu/graph/lobbyist",
    cypher=(
        "MATCH (l:Lobbyist)-[:INTERESTED_IN]->(li:LobbyInterest) "
        "RETURN l.tr_id AS tr_id, coalesce(li.name, li.topic) AS topic"
    ),
    render=render_rel_lobbyist_interest,
    page_size=10000,
)


REGISTRY: dict[str, Mapper] = {
    m.name: m for m in [
        CPV, NUTS, LISTING, COHESION, AUTHORITY, LOBBYIST, COMPANY_LEI, CONTRACT,
        REL_AUTHORITY_AWARDED, REL_CONTRACT_AWARDED_TO, REL_CONTRACT_CPV,
        REL_COMPANY_LOCATED, REL_NUTS_PARTOF, REL_COMPANY_LISTING,
        REL_COHESION_BENEFICIARY, REL_LOBBYIST_INTEREST,
    ]
}


# Mappers that need the --since cutoff to be passed.
NEEDS_SINCE = {CONTRACT.name, REL_AUTHORITY_AWARDED.name,
               REL_CONTRACT_AWARDED_TO.name, REL_CONTRACT_CPV.name}


# ── Driver ────────────────────────────────────────────────────────


def fetch_pages(driver, mapper: Mapper, params: dict) -> Iterator[list[dict]]:
    """Fetch rows in pages so memory stays bounded for the larger
    classes (Company, Contract).
    """
    skip = 0
    paged_query = mapper.cypher + " SKIP $skip LIMIT $limit"
    with driver.session() as session:
        while True:
            rows = session.run(
                paged_query,
                {**params, "skip": skip, "limit": mapper.page_size},
            ).data()
            if not rows:
                return
            yield rows
            if len(rows) < mapper.page_size:
                return
            skip += mapper.page_size


def render_pages(pages: Iterator[list[dict]], mapper: Mapper) -> Iterator[str]:
    """Yield Turtle chunks (each = one page worth of triples)."""
    for page in pages:
        bodies = [mapper.render(row) for row in page]
        bodies = [b for b in bodies if b]
        if not bodies:
            continue
        yield PREAMBLE + "\n" + "\n\n".join(bodies) + "\n"


def push(client: httpx.Client, base_url: str, graph_iri: str, turtle: str, *, replace: bool) -> None:
    """PUT (replace) or POST (append) a Turtle payload to a graph."""
    method = client.put if replace else client.post
    resp = method(
        f"{base_url}/sparql-graph-crud-auth",
        params={"graph": graph_iri},
        content=turtle.encode("utf-8"),
        headers={"Content-Type": "text/turtle"},
        timeout=300.0,
    )
    resp.raise_for_status()


def run(mapper: Mapper, *, neo4j_uri: str, neo4j_user: str, neo4j_password: str,
        virtuoso_url: str, virtuoso_password: str, since: str | None = None) -> int:
    """Bridge one class into Virtuoso. Returns the row count migrated.

    Memory pressure on the bigger classes (company-lei, contract) is
    handled by Virtuoso's automatic checkpoint cadence rather than
    explicit calls — we don't have a clean SPARQL-over-HTTP path to
    issue a SQL `checkpoint` command, and the 2 GiB cgroup that
    triggered the original OOM has been raised to 6 GiB. If the
    bridge ever has to run under tight memory again, route the
    checkpoint via `kubectl exec virtuoso-0 -- isql ... exec="checkpoint;"`
    from a sidecar.
    """
    params: dict = {}
    if since:
        params["since"] = since

    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    total = 0
    try:
        # Virtuoso's /sparql*-auth endpoints use Digest auth, not
        # Basic — Basic returns 401 even with the right password.
        auth = httpx.DigestAuth("dba", virtuoso_password)
        with httpx.Client(auth=auth) as vclient:
            first = True
            for page_n, page in enumerate(fetch_pages(driver, mapper, params)):
                bodies = [mapper.render(r) for r in page]
                bodies = [b for b in bodies if b]
                if not bodies:
                    continue
                turtle = PREAMBLE + "\n" + "\n\n".join(bodies) + "\n"
                push(vclient, virtuoso_url, mapper.target_graph, turtle, replace=first)
                first = False
                total += len(page)
                logger.info("%s: %d rows pushed (cumulative)", mapper.name, total)
    finally:
        driver.close()

    return total


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("class_name", choices=list(REGISTRY.keys()))
    parser.add_argument("--since", help="ISO date (e.g. 2026-03-01) for time-limited classes (contract).")
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    parser.add_argument(
        "--virtuoso-url",
        default=os.environ.get("VIRTUOSO_BASE_URL", "http://virtuoso.gmr.svc.cluster.local:8890"),
    )
    parser.add_argument("--virtuoso-password", default=os.environ.get("VIRTUOSO_DBA_PASSWORD", ""))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    mapper = REGISTRY[args.class_name]
    if mapper.name in NEEDS_SINCE and not args.since:
        sys.exit(f"--since YYYY-MM-DD is required for {mapper.name} (staging compactness).")

    n = run(
        mapper,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_password=args.neo4j_password,
        virtuoso_url=args.virtuoso_url,
        virtuoso_password=args.virtuoso_password,
        since=args.since,
    )
    logger.info("Bridge complete: %s -> %s (%d rows)", mapper.name, mapper.target_graph, n)


if __name__ == "__main__":
    main()
