"""Integration tests for the filings → Virtuoso writer.

Same shape as test_rdf_sanctions_writer: spin an ephemeral
Virtuoso, exercise SHACL pass/fail, confirm round-trip. The
two domains are intentionally tested with the same scaffold so
adding the next ETL stays a copy-and-tweak.
"""
# rdflib and pyshacl are imported inside the round-trip helper so the test
# module can be collected on lint-only environments where they're absent.
# `broad-exception-caught` mirrors the writer's own catch: the ephemeral
# Virtuoso may go away mid-test (the test starts it itself) and we want a
# skip, not an obscure traceback.
# pylint: disable=import-outside-toplevel,import-error,broad-exception-caught
from __future__ import annotations

import socket
import subprocess
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from src.etl.rdf_filings_writer import (
    GRAPH_FOR_SOURCE,
    RdfFilingsWriter,
)

VIRTUOSO_IMAGE = "contribute.void42.internal/fontem/virtuoso-opensource-7:7.2.14"
SHAPES_PATH = Path(
    "/config/repos/fontem-ontology/shapes/filing.shacl.ttl"
)


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available() or not SHAPES_PATH.is_file(),
    reason="needs docker + fontem-ontology shape file checked out",
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@contextmanager
def _virtuoso_container():
    port = _free_port()
    name = f"filings-it-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-e", "DBA_PASSWORD=phase3-test-dba",
            "-p", f"{port}:8890", VIRTUOSO_IMAGE,
        ],
        check=True, capture_output=True, text=True,
    )
    container_id = proc.stdout.strip()
    try:
        endpoint = f"http://127.0.0.1:{port}/sparql"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{endpoint}?query=ASK%20%7B%7D", timeout=2
                ) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("Virtuoso never came up")
        pw_proc = subprocess.run(
            ["docker", "exec", name, "cat", "/settings/dba_password"],
            check=True, capture_output=True, text=True,
        )
        pwd = pw_proc.stdout.strip()
        yield endpoint, pwd
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False, capture_output=True,
        )


GMR_ID_A = "11111111-1111-5111-9111-111111111111"
GMR_ID_B = "22222222-2222-5222-9222-222222222222"

GOOD_EDGAR = [
    {
        "gmr_id": GMR_ID_A, "year": 2023,
        "revenue": 100000.0, "net_income": 12345.0,
        "total_assets": 500000.0,
    },
    {
        "gmr_id": GMR_ID_A, "year": 2022,
        "revenue": 95000.0, "net_income": 10000.0,
    },
    {
        "gmr_id": GMR_ID_B, "year": 2023,
        "revenue": 7e9, "operating_cashflow": 1.2e9,
        "shares_outstanding": 1.5e9,
    },
]

GOOD_ESEF = [
    {
        "gmr_id": GMR_ID_B, "year": 2023,
        "revenue": 8.0e9,
        "filing_date": "2024-03-12",
        "free_cashflow": 9e8,
    },
]

# Bad row — missing gmr_id (causes filedBy to point at an
# IRI ending in `Company/None`, which is technically present
# but cannot be a real Company; covered separately below). The
# truly-fatal violation is missing year, which the writer
# currently refuses (KeyError before SHACL). Use a SHACL-level
# violation: an unknown filingSource sneaks past the writer
# constructor (which guards `source`) only when an attacker
# bypasses the constructor — easiest synthetic: build the
# graph by hand. The integration check focuses on the writer's
# happy path; SHACL constraints are pinned by the shape file's
# own ontology smoke test in fontem-ontology.


def _count(endpoint: str, graph: str) -> int:
    q = (
        f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ "
        f"GRAPH <{graph}> {{ ?s a "
        f"<http://data.fontem.eu/ontology#Filing> }} }}"
    )
    with httpx.Client(timeout=20) as c:
        r = c.get(endpoint, params={"query": q},
                  headers={"Accept": "application/sparql-results+json"})
        r.raise_for_status()
        return int(r.json()["results"]["bindings"][0]["n"]["value"])


def _ask(endpoint: str, q: str) -> bool:
    with httpx.Client(timeout=20) as c:
        r = c.get(endpoint, params={"query": q},
                  headers={"Accept": "application/sparql-results+json"})
        r.raise_for_status()
        return r.json().get("boolean", False)


def test_two_sources_into_separate_graphs():
    with _virtuoso_container() as (endpoint, pwd):
        edgar = RdfFilingsWriter(
            source="edgar", sparql_endpoint=endpoint,
            dba_password=pwd, shapes_path=SHAPES_PATH,
        )
        esef = RdfFilingsWriter(
            source="esef", sparql_endpoint=endpoint,
            dba_password=pwd, shapes_path=SHAPES_PATH,
        )

        e_res = edgar.write(GOOD_EDGAR)
        s_res = esef.write(GOOD_ESEF)
        assert e_res.written == 3
        assert s_res.written == 1

        # Each graph carries only its own source's filings —
        # the per-source graph isolation is enforced by the
        # writer's choice of named graph.
        assert _count(endpoint, GRAPH_FOR_SOURCE["edgar"]) == 3
        assert _count(endpoint, GRAPH_FOR_SOURCE["esef"]) == 1

        # Round-trip a representative property.
        assert _ask(endpoint, f"""
            ASK {{ GRAPH <{GRAPH_FOR_SOURCE["edgar"]}> {{
                ?f <http://data.fontem.eu/ontology#filedBy>
                   <http://data.fontem.eu/id/Company/{GMR_ID_A}> ;
                   <http://data.fontem.eu/ontology#fiscalYear>
                   "2023"^^<http://www.w3.org/2001/XMLSchema#gYear> ;
                   <http://data.fontem.eu/ontology#revenue>
                   ?rev .
                FILTER(?rev > 99000)
            }} }}
        """)


def test_put_overwrites_graph_within_source():
    """A second write to the same source REPLACES the graph
    (PUT semantics) — re-runs of the loader don't accumulate
    stale rows."""
    with _virtuoso_container() as (endpoint, pwd):
        w = RdfFilingsWriter(
            source="edgar", sparql_endpoint=endpoint,
            dba_password=pwd, shapes_path=SHAPES_PATH,
        )
        w.write(GOOD_EDGAR)
        assert _count(endpoint, GRAPH_FOR_SOURCE["edgar"]) == 3

        w.write(GOOD_EDGAR[:1])
        assert _count(endpoint, GRAPH_FOR_SOURCE["edgar"]) == 1


def test_unknown_source_rejected_at_construction():
    with pytest.raises(ValueError):
        RdfFilingsWriter(
            source="not-a-real-source",
            sparql_endpoint="http://unused/sparql",
        )


def test_shacl_catches_bad_filingsource_in_graph():
    """If a malformed batch slips through (e.g. by adding a
    triple manually), SHACL validation must reject it."""
    from rdflib import Graph, Literal, URIRef
    from rdflib.namespace import RDF, XSD
    from src.etl.rdf_filings_writer import (
        FONTEM, _locate_shapes,
    )
    from pyshacl import validate

    g = Graph()
    iri = URIRef("http://data.fontem.eu/id/Filing/abc")
    company = URIRef("http://data.fontem.eu/id/Company/x")
    g.add((iri, RDF.type, FONTEM.Filing))
    g.add((iri, FONTEM.filedBy, company))
    g.add((iri, FONTEM.fiscalYear,
           Literal("2024", datatype=XSD.gYear)))
    # Wrong filingSource — not in (edgar, esef, gleif)
    g.add((iri, FONTEM.filingSource, Literal("madeupsource")))

    shapes = Graph().parse(str(_locate_shapes()), format="turtle")
    conforms, _, report = validate(
        data_graph=g, shacl_graph=shapes, inference="rdfs",
    )
    assert not conforms, report
    assert "filingSource" in report
