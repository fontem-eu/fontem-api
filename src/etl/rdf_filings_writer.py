"""Filings → Virtuoso writer.

Both the EDGAR (``load_us_financials``) and ESEF
(``load_eu_listings``) loaders share this writer: they hand it
records of the shape::

    {
        "gmr_id": "<company-uuid>",
        "year":   2023,
        "revenue": 12345.0, "net_income": 678.0, ...   # any subset
        "filing_date": "2024-02-21",                   # ESEF only
    }

and a ``source`` literal (``"edgar"`` or ``"esef"``). The writer
translates each record into a SHACL-validated batch of
``fontem:Filing`` triples and PUTs them to a per-source named
graph.

Per-source graphs (rather than a single ``…/graph/financials``)
are deliberate: the two loaders run on different schedules, and a
PUT-style replacement on a shared graph would have one loader
wipe the other's data. With separate graphs each loader owns its
side; the data-quality SPARQL queries union them when reporting
the cross-source totals.

  EDGAR → http://data.fontem.eu/graph/financials/edgar
  ESEF  → http://data.fontem.eu/graph/financials/esef

The Filing IRI is a deterministic UUID5 of (gmr_id, year, source)
so re-runs for the same company-year-source land on the same IRI
and overwrite cleanly. (gmr_id, year) alone — the Neo4j primary
key — would mean EDGAR and ESEF data for the same company-year
collide, which the Neo4j schema accepted as a drift bug; we fix
it here at the same time we move stores.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

logger = logging.getLogger(__name__)

FONTEM = Namespace("http://data.fontem.eu/ontology#")
FILING_BASE = "http://data.fontem.eu/id/Filing/"
COMPANY_BASE = "http://data.fontem.eu/id/Company/"

GRAPH_FOR_SOURCE = {
    "edgar": "http://data.fontem.eu/graph/financials/edgar",
    "esef":  "http://data.fontem.eu/graph/financials/esef",
}

# Same UUID5 namespace the rest of the loaders use (gmr_id.py).
# Keeps Filing IRIs reproducible across reruns.
_GMR_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Field-name → fontem property URI. The record dicts emitted by
# both loaders use snake_case Python keys; we map to the
# camelCase property URIs from the corporate ontology.
_FIELD_MAP: dict[str, str] = {
    "revenue":                   str(FONTEM.revenue),
    "gross_profit":              str(FONTEM.grossProfit),
    "operating_income":          str(FONTEM.operatingIncome),
    "net_income":                str(FONTEM.netIncome),
    "eps":                       str(FONTEM.eps),
    "total_assets":              str(FONTEM.totalAssets),
    "total_liabilities":         str(FONTEM.totalLiabilities),
    "equity":                    str(FONTEM.equity),
    "cash_and_equivalents":      str(FONTEM.cash),
    "cash":                      str(FONTEM.cash),
    "capex":                     str(FONTEM.capex),
    "operating_cashflow":        str(FONTEM.operatingCashflow),
    "free_cashflow":             str(FONTEM.freeCashflow),
    "current_assets":            str(FONTEM.currentAssets),
    "current_liabilities":       str(FONTEM.currentLiabilities),
    "shares_outstanding":        str(FONTEM.sharesOutstanding),
    "long_term_debt":            str(FONTEM.longTermDebt),
    "interest_expense":          str(FONTEM.interestExpense),
    "income_tax_expense":        str(FONTEM.incomeTaxExpense),
    "depreciation_amortization": str(FONTEM.depreciationAmortization),
    "inventory":                 str(FONTEM.inventory),
}

_DEFAULT_SHAPE_LOCATIONS = [
    Path("/config/repos/fontem-ontology/shapes/filing.shacl.ttl"),
    Path(__file__).resolve().parent.parent.parent / "data" / "filing.shacl.ttl",
]


def _locate_shapes() -> Path:
    if env := os.environ.get("FILING_SHACL_PATH"):
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"FILING_SHACL_PATH={env} but file does not exist"
        )
    for cand in _DEFAULT_SHAPE_LOCATIONS:
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "Could not locate filing.shacl.ttl. Set FILING_SHACL_PATH or "
        "check out fontem-ontology under /config/repos."
    )


@dataclass
class WriteResult:
    """Outcome of a single write call."""
    written: int
    triples_pushed: int


class FilingsValidationError(RuntimeError):
    """Raised when SHACL validation fails for the batch."""

    def __init__(self, report: str) -> None:
        super().__init__(
            "filings batch failed SHACL validation:\n" + report
        )
        self.report = report


class RdfFilingsWriter:
    """SHACL-validating writer that PUTs Filings to Virtuoso.

    A writer instance is bound to one source (``edgar`` or
    ``esef``) at construction so the named graph is implicit and
    misuse — pushing EDGAR records into the ESEF graph — is hard
    to do accidentally.
    """

    def __init__(
        self,
        *,
        source: str,
        sparql_endpoint: str,
        dba_user: str = "dba",
        dba_password: str = "",
        timeout: float = 1800.0,  # 30m — EDGAR full-snapshot PUT is large.
        shapes_path: Path | None = None,
    ) -> None:
        if source not in GRAPH_FOR_SOURCE:
            raise ValueError(
                f"unknown filing source {source!r}; "
                f"expected one of {sorted(GRAPH_FOR_SOURCE)}"
            )
        self.source = source
        self.graph_iri = GRAPH_FOR_SOURCE[source]
        self.sparql_endpoint = sparql_endpoint.rstrip("/")
        self.dba_user = dba_user
        self.dba_password = dba_password
        self.timeout = timeout
        self._shapes_path = shapes_path or _locate_shapes()

    # ── public API ────────────────────────────────────────

    def write(self, records: Iterable[dict]) -> WriteResult:
        """Validate + PUT a batch of filing records.

        Each record must carry ``gmr_id`` (str) and ``year``
        (int). Numeric fields are picked up by lookup against
        ``_FIELD_MAP``; unknown keys are ignored (forward-
        compatible — adding a new XBRL concept on the loader
        side without a property URI doesn't break the writer).
        """
        graph = Graph()
        graph.bind("fontem", FONTEM)
        graph.bind("xsd", XSD)

        written = 0
        for rec in records:
            self._add_filing(graph, rec)
            written += 1

        if written == 0:
            return WriteResult(0, 0)

        self._validate(graph)
        triples = self._push(graph)
        logger.info(
            "rdf_filings(%s): wrote %d filings (%d triples) to <%s>",
            self.source, written, triples, self.graph_iri,
        )
        return WriteResult(written, triples)

    # ── implementation ────────────────────────────────────

    def _filing_iri(self, gmr_id: str, year: int) -> URIRef:
        seed = f"filing:{gmr_id}:{year}:{self.source}"
        return URIRef(FILING_BASE + str(uuid.uuid5(_GMR_NS, seed)))

    def _add_filing(self, g: Graph, rec: dict) -> None:
        gmr_id = rec["gmr_id"]
        year = int(rec["year"])
        iri = self._filing_iri(gmr_id, year)
        company_iri = URIRef(COMPANY_BASE + gmr_id)

        g.add((iri, RDF.type, FONTEM.Filing))
        g.add((iri, FONTEM.filedBy, company_iri))
        g.add((iri, FONTEM.fiscalYear,
               Literal(str(year), datatype=XSD.gYear)))
        g.add((iri, FONTEM.filingSource, Literal(self.source)))

        if filing_date := (rec.get("filing_date") or "").strip()[:10]:
            g.add((iri, FONTEM.filingDate,
                   Literal(filing_date, datatype=XSD.date)))

        for key, prop in _FIELD_MAP.items():
            if (val := rec.get(key)) is None:
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            g.add((
                iri,
                URIRef(prop),
                Literal(repr(num), datatype=XSD.decimal),
            ))

    def _validate(self, g: Graph) -> None:
        # Lazy import — graph-only callers (tests, dry-runs)
        # don't need pyshacl on the import path.
        from pyshacl import validate as _validate

        shapes_g = Graph().parse(str(self._shapes_path), format="turtle")
        conforms, _, report = _validate(
            data_graph=g,
            shacl_graph=shapes_g,
            inference="rdfs",
            advanced=False,
        )
        if not conforms:
            raise FilingsValidationError(report)

    def _push(self, g: Graph) -> int:
        """PUT the batch to Virtuoso's graph-crud endpoint.

        PUT replaces the named graph wholesale — correct for a
        full-snapshot loader. Per-source graphs mean EDGAR and
        ESEF can replace independently.

        Auth is HTTP Digest (Virtuoso rejects Basic on this
        endpoint).
        """
        body = g.serialize(format="turtle")
        url = self.sparql_endpoint.replace(
            "/sparql", "/sparql-graph-crud-auth"
        )
        params = {"graph": self.graph_iri}
        auth = httpx.DigestAuth(self.dba_user, self.dba_password)
        with httpx.Client(timeout=self.timeout, auth=auth) as client:
            r = client.put(
                url, params=params, content=body,
                headers={"Content-Type": "text/turtle"},
            )
            r.raise_for_status()
        return len(g)
