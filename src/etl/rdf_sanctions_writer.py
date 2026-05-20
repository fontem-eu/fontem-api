"""Sanctions → Virtuoso writer.

Converts the dicts emitted by ``parse_sanctions_xml`` into Turtle
N-Triples conforming to the Phase 0 sanctions ontology, validates
each batch against ``shapes/sanctions.shacl.ttl``, and pushes the
result into the named graph
``http://data.fontem.eu/graph/sanctions`` via Virtuoso's SPARQL
graph-crud endpoint.

Reasons it lives in its own module rather than rolled into
``load_eu_sanctions``:

* The XML parser is reusable (the same parser feeds both this
  writer and any future fixture-based testing). Keeping the
  XML→dict step pure makes this writer trivial to unit-test
  with a hand-written input dict.
* SHACL validation is a hard gate. We pull the shapes from the
  fontem-ontology repo at import time and refuse to run if it
  isn't on disk — so a misconfigured CI image fails loudly
  rather than silently writing un-validated triples.

Public API (used by ``load_eu_sanctions``):

    writer = RdfSanctionsWriter(
        sparql_endpoint="http://virtuoso.gmr.svc.cluster.local:8890/sparql",
        graph_iri="http://data.fontem.eu/graph/sanctions",
        dba_user="dba", dba_password="...",
    )
    n = writer.write(entities)   # SHACL-validates + POSTs
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD

logger = logging.getLogger(__name__)

FONTEM = Namespace("http://data.fontem.eu/ontology#")
SANCTION_BASE = "http://data.fontem.eu/id/Sanction/"
DEFAULT_GRAPH_IRI = "http://data.fontem.eu/graph/sanctions"

# Shapes file location is configurable so CI can point at a local
# clone of fontem-ontology rather than depending on a sibling
# checkout. ``SHAPES_PATH`` lookup order:
#   1. SANCTIONS_SHACL_PATH env var (CI sets this)
#   2. ``shapes/sanctions.shacl.ttl`` next to a sibling
#      fontem-ontology checkout under /config/repos
#   3. Bundled copy under this repo's data/ dir (fallback for the
#      production image, populated by the CI build step)
_DEFAULT_SHAPE_LOCATIONS = [
    Path("/config/repos/fontem-ontology/shapes/sanctions.shacl.ttl"),
    Path(__file__).resolve().parent.parent.parent / "data" / "sanctions.shacl.ttl",
]


def _locate_shapes() -> Path:
    if env := os.environ.get("SANCTIONS_SHACL_PATH"):
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"SANCTIONS_SHACL_PATH={env} but file does not exist"
        )
    for cand in _DEFAULT_SHAPE_LOCATIONS:
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "Could not locate sanctions.shacl.ttl. Set SANCTIONS_SHACL_PATH "
        "or check out fontem-ontology under /config/repos."
    )


@dataclass
class WriteResult:
    """Outcome of one ``write`` call."""

    written: int
    skipped_persons: int
    triples_pushed: int


class RdfSanctionsWriter:
    """SHACL-validating writer that pushes sanctions to Virtuoso.

    GDPR posture: persons are explicitly excluded — see
    ``ontology/sanctions.ttl``. ``entity_type='person'`` records
    are counted in the result and discarded silently. The
    SanctionedEntity class only models organisations.
    """

    # One kwarg per Virtuoso/SPARQL knob the caller may need to override
    # (endpoint, named graph, auth user/password, timeout, SHACL shapes path).
    # All keyword-only with defaults; bundling into a config object adds a
    # layer with no readers.
    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        sparql_endpoint: str,
        graph_iri: str = DEFAULT_GRAPH_IRI,
        dba_user: str = "dba",
        dba_password: str = "",
        timeout: float = 30.0,
        shapes_path: Path | None = None,
    ) -> None:
        self.sparql_endpoint = sparql_endpoint.rstrip("/")
        self.graph_iri = graph_iri
        self.dba_user = dba_user
        self.dba_password = dba_password
        self.timeout = timeout
        self._shapes_path = shapes_path or _locate_shapes()

    # ── public API ────────────────────────────────────────

    def write(self, entities: Iterable[dict]) -> WriteResult:
        """Validate + push a batch of sanction dicts.

        Person-typed records are dropped (GDPR posture). The
        remaining records are serialised to Turtle, run through
        pyshacl against the bundled shape, then POSTed to the
        sanctions named graph via the SPARQL graph-crud endpoint.

        Raises ``SanctionsValidationError`` if any record fails
        shape validation. Either the whole batch is written or
        none of it — half-written batches would corrupt the
        named graph.
        """
        graph = Graph()
        graph.bind("fontem", FONTEM)
        graph.bind("rdfs", RDFS)
        graph.bind("skos", SKOS)
        graph.bind("xsd", XSD)

        written = 0
        skipped_persons = 0
        for ent in entities:
            if (ent.get("entity_type") or "").lower() == "person":
                skipped_persons += 1
                continue
            self._add_entity(graph, ent)
            written += 1

        if written == 0:
            logger.info(
                "rdf_sanctions: nothing to write (skipped %d persons)",
                skipped_persons,
            )
            return WriteResult(0, skipped_persons, 0)

        self._validate(graph)

        triples = self._push(graph)
        logger.info(
            "rdf_sanctions: wrote %d entities (%d triples) to <%s>; "
            "skipped %d persons",
            written, triples, self.graph_iri, skipped_persons,
        )
        return WriteResult(written, skipped_persons, triples)

    # ── implementation ────────────────────────────────────

    def _add_entity(self, g: Graph, ent: dict) -> None:
        iri = URIRef(SANCTION_BASE + ent["entity_id"])
        g.add((iri, RDF.type, FONTEM.SanctionedEntity))
        g.add((iri, FONTEM.euReference, Literal(ent["eu_reference"])))

        if ent.get("name"):
            g.add((iri, RDFS.label, Literal(ent["name"], lang="en")))
        for alias in ent.get("aliases") or ():
            if alias:
                g.add((iri, SKOS.altLabel, Literal(alias)))

        if dt := (ent.get("designation_date") or "").strip()[:10]:
            g.add((iri, FONTEM.designationDate,
                   Literal(dt, datatype=XSD.date)))

        if regime := (ent.get("sanction_regime") or "").strip():
            g.add((iri, FONTEM.sanctionRegime, Literal(regime)))

        if basis := (ent.get("legal_basis") or "").strip():
            g.add((iri, FONTEM.legalBasis, Literal(basis)))

        if reason := (ent.get("listing_reason") or "").strip():
            g.add((iri, FONTEM.listingReason, Literal(reason)))

    def _validate(self, g: Graph) -> None:
        # Imported lazily so the Turtle-only path doesn't require
        # pyshacl in environments that just want to construct
        # graphs (tests, dry-run mode). pyshacl is an optional dep —
        # the lint runner doesn't have it on its import path, so
        # import-error is structural here.
        from pyshacl import validate as _validate  # pylint: disable=import-outside-toplevel,import-error

        shapes_g = Graph().parse(str(self._shapes_path), format="turtle")
        conforms, _, report = _validate(
            data_graph=g,
            shacl_graph=shapes_g,
            inference="rdfs",
            advanced=False,
        )
        if not conforms:
            raise SanctionsValidationError(report)

    def _push(self, g: Graph) -> int:
        """PUT the Turtle to /sparql-graph-crud-auth.

        We use PUT semantics on the named graph: each loader run
        replaces the graph contents wholesale. This is correct
        for a daily snapshot — partial diffs would leak deleted
        sanctions back into the graph.

        Virtuoso's CRUD endpoint requires HTTP Digest Auth (not
        Basic). Probed empirically: Basic returns 401, Digest
        returns 201/200.
        """
        body = g.serialize(format="turtle")
        url = self.sparql_endpoint.replace(
            "/sparql", "/sparql-graph-crud-auth"
        )
        params = {"graph": self.graph_iri}
        auth = httpx.DigestAuth(self.dba_user, self.dba_password)

        with httpx.Client(timeout=self.timeout, auth=auth) as client:
            r = client.put(
                url,
                params=params,
                content=body,
                headers={"Content-Type": "text/turtle"},
            )
            r.raise_for_status()
        return len(g)


class SanctionsValidationError(RuntimeError):
    """Raised when SHACL validation fails for the batch."""

    def __init__(self, report: str) -> None:
        super().__init__(
            "sanctions batch failed SHACL validation:\n" + report
        )
        self.report = report
