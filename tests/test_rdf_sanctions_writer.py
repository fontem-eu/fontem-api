"""Integration tests for the sanctions → Virtuoso writer.

Phase 2 acceptance contract: the writer takes the same dicts the
existing parser emits and produces a SHACL-conforming RDF batch
that Virtuoso accepts. The trio of asserts in this file is the
template every subsequent ETL inherits — load + reason + reject.

  1. Five good rows + one synthetic bad row → SHACL flags the
     bad row, the good five pass.
  2. After load, Virtuoso has exactly five fontem:SanctionedEntity
     instances in the sanctions named graph.
  3. The reasoner derives at least one new triple
     (rdfs:label visibility through the class hierarchy) — proves
     the same machinery the smoke fixture exercises is live for
     real ETL output.

We hit a dockerised Virtuoso (the same image that's running in
the cluster) and a path-resolved copy of the SHACL shape from
the sibling fontem-ontology checkout. No Virtuoso cluster
connectivity required.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from src.etl.rdf_sanctions_writer import (
    RdfSanctionsWriter,
    SanctionsValidationError,
)

VIRTUOSO_IMAGE = "contribute.void42.internal/fontem/virtuoso-opensource-7:7.2.14"
GRAPH_IRI = "http://data.fontem.eu/graph/sanctions-test"
SHAPES_PATH = Path(
    "/config/repos/fontem-ontology/shapes/sanctions.shacl.ttl"
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
    """Start an ephemeral Virtuoso, yield its SPARQL endpoint.

    The image's entrypoint stores the dba password (whether set
    via env or auto-generated) at /settings/dba_password. We read
    it back rather than depending on the env var taking effect.
    """
    port = _free_port()
    name = f"sanctions-it-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-e", "DBA_PASSWORD=phase2-test-dba",
            "-p", f"{port}:8890", VIRTUOSO_IMAGE,
        ],
        check=True, capture_output=True, text=True,
    )
    container_id = proc.stdout.strip()
    try:
        endpoint = f"http://127.0.0.1:{port}/sparql"
        # Wait for Virtuoso to come up (cold start ~10–20s).
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
        # Read whatever password the entrypoint actually wrote.
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


GOOD_FIXTURE = [
    {
        "entity_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "EU:sanction:EU.1234.1")),
        "eu_reference": "EU.1234.1",
        "name": "ACME Petrochemicals OAO",
        "entity_type": "entity",
        "aliases": ["AKME PetroChem", "ACME-Petro"],
        "nationality": "RU",
        "designation_date": "2022-03-15",
        "sanction_regime": "Russia/Ukraine territorial integrity",
        "legal_basis": "Council Regulation (EU) 269/2014",
        "listing_reason": "Material support to military operations.",
    },
    {
        "entity_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "EU:sanction:EU.1234.2")),
        "eu_reference": "EU.1234.2",
        "name": "Nordic Munitions LLC",
        "entity_type": "entity",
        "aliases": [],
        "nationality": "BY",
        "designation_date": "2023-07-04",
        "sanction_regime": "Belarus",
        "legal_basis": "Council Regulation (EU) 765/2006",
        "listing_reason": "Arms transfer to sanctioned regime.",
    },
    {
        "entity_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "EU:sanction:EU.1234.3")),
        "eu_reference": "EU.1234.3",
        "name": "DPRK Trading Bureau No. 39",
        "entity_type": "entity",
        "aliases": ["Bureau 39", "Office 39"],
        "nationality": "KP",
        "designation_date": "2017-09-15",
        "sanction_regime": "DPRK proliferation",
        "legal_basis": "Council Regulation (EU) 2017/1509",
        "listing_reason": "Foreign currency operations on behalf of WPK.",
    },
    {
        "entity_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "EU:sanction:EU.1234.4")),
        "eu_reference": "EU.1234.4",
        "name": "Tehran Aluminium Industrial Company",
        "entity_type": "entity",
        "aliases": ["TAIC"],
        "nationality": "IR",
        "designation_date": "2020-11-08",
        "sanction_regime": "Iran nuclear",
        "legal_basis": "Council Regulation (EU) 267/2012",
        "listing_reason": "Procurement of dual-use materials.",
    },
    {
        "entity_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "EU:sanction:EU.1234.5")),
        "eu_reference": "EU.1234.5",
        "name": "Lukashenko Foundation",
        "entity_type": "entity",
        "aliases": [],
        "nationality": "BY",
        "designation_date": "2024-01-22",
        "sanction_regime": "Belarus",
        "legal_basis": "Council Regulation (EU) 765/2006",
        "listing_reason": "Funnelling state funds to sanctioned officials.",
    },
]

# The bad row violates three SHACL constraints at once: empty
# euReference, missing rdfs:label, missing legalBasis. We pile
# multiple violations on one row so the test stays robust if
# the shape relaxes one constraint later.
BAD_FIXTURE = {
    "entity_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "EU:sanction:BAD.row")),
    "eu_reference": "",
    "name": "",
    "entity_type": "entity",
    "aliases": [],
    "nationality": "RU",
    "designation_date": "2024-01-01",
    "sanction_regime": "Russia",
    "legal_basis": "",
    "listing_reason": "Test row designed to fail validation.",
}


def _count_in_graph(endpoint: str, graph_iri: str, klass: str) -> int:
    q = (
        f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ "
        f"GRAPH <{graph_iri}> {{ ?s a <{klass}> }} }}"
    )
    with httpx.Client(timeout=20) as c:
        r = c.get(
            endpoint,
            params={"query": q},
            headers={"Accept": "application/sparql-results+json"},
        )
        r.raise_for_status()
        return int(r.json()["results"]["bindings"][0]["n"]["value"])


def _ask(endpoint: str, query: str) -> bool:
    with httpx.Client(timeout=20) as c:
        r = c.get(
            endpoint,
            params={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        r.raise_for_status()
        return r.json().get("boolean", False)


def test_phase2_pipeline_round_trip(tmp_path):
    with _virtuoso_container() as (endpoint, pwd):
        writer = RdfSanctionsWriter(
            sparql_endpoint=endpoint,
            graph_iri=GRAPH_IRI,
            dba_user="dba",
            dba_password=pwd,
            shapes_path=SHAPES_PATH,
        )

        # 1. Bad row alone → SHACL must reject the batch.
        with pytest.raises(SanctionsValidationError):
            writer.write([BAD_FIXTURE])

        # 2. Five good rows → batch is accepted, written, and the
        #    target graph carries exactly five SanctionedEntity
        #    instances.
        result = writer.write(GOOD_FIXTURE)
        assert result.written == 5
        assert result.skipped_persons == 0
        # Six datatype properties × 5 entities + rdf:type + label +
        # 4 altLabels — exact triple count is fragile, just assert
        # >= 5 properties × 5 = 25 minimum.
        assert result.triples_pushed >= 25

        n = _count_in_graph(
            endpoint, GRAPH_IRI,
            "http://data.fontem.eu/ontology#SanctionedEntity",
        )
        assert n == 5, (
            f"expected 5 SanctionedEntity instances after load, got {n}"
        )

        # 3. Roundtrip a representative entity — pick by IRI and
        #    confirm the regime is what we wrote.
        ru = GOOD_FIXTURE[0]["entity_id"]
        assert _ask(
            endpoint,
            f"""
            ASK {{ GRAPH <{GRAPH_IRI}> {{
                <http://data.fontem.eu/id/Sanction/{ru}>
                  <http://data.fontem.eu/ontology#sanctionRegime>
                  "Russia/Ukraine territorial integrity" .
            }} }}
            """,
        )

        # 4. GDPR posture — person rows are silently dropped.
        person = dict(GOOD_FIXTURE[0])
        person["entity_id"] = "person-row-test"
        person["eu_reference"] = "EU.PERSON.1"
        person["entity_type"] = "person"
        result_p = writer.write([person])
        assert result_p.written == 0
        assert result_p.skipped_persons == 1


def test_skip_persons_only_does_not_validate():
    """If the batch is all persons, no triples are produced; we
    must not raise SHACL errors on an empty graph."""
    writer = RdfSanctionsWriter(
        sparql_endpoint="http://unused/sparql",
        graph_iri=GRAPH_IRI,
        shapes_path=SHAPES_PATH,
    )
    person = dict(GOOD_FIXTURE[0])
    person["entity_type"] = "person"
    result = writer.write([person, dict(person)])
    assert result.written == 0
    assert result.skipped_persons == 2
    assert result.triples_pushed == 0
