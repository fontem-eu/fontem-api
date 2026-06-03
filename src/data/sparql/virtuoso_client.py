"""Tiny synchronous SPARQL client over httpx.

The data-quality dashboard runs SPARQL SELECT queries against
Virtuoso (sanctions stats, top-regimes, etc.) and stays
deliberately small: no persistent client, no retries, no
streaming. Each call opens a short-lived httpx.Client because
the request rate is low — once-a-minute dashboard refresh, not
hot-path traffic.

Default timeout is 60s rather than 10s because the data-quality
inventory query ("count every triple in every data graph") does a
full triple-store scan on Virtuoso and reliably blew past 10s on
prod, taking the dashboard down with a ReadTimeout-rooted 500.
The dashboard panel that calls this is cached by the browser; the
extra ceiling head-room only kicks in on the first cold load per
session.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SparqlTimeout(Exception):
    """Raised when Virtuoso doesn't reply within the configured timeout.

    Callers that surface results in a dashboard should catch this and
    render a partial/graceful state rather than 500-ing the request.
    """


@dataclass
class VirtuosoClient:
    """Read-only SPARQL client.

    Only ``query`` is exposed; updates land via the loader's own
    SHACL-validated graph-crud path, not through this client.
    """

    sparql_endpoint: str
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "VirtuosoClient | None":
        """Build from VIRTUOSO_SPARQL_URL. Returns None if unset.

        Returning None lets the API server boot in environments
        that haven't enabled Virtuoso yet — the data quality
        source falls back to Neo4j-only behaviour rather than
        failing at startup.

        ``VIRTUOSO_SPARQL_TIMEOUT`` (seconds, float) overrides the
        60s default. The inventory query in the data-quality
        dashboard does a full triple-store scan and reliably hit
        the previous 10s default in prod.
        """
        if endpoint := os.environ.get("VIRTUOSO_SPARQL_URL"):
            timeout = float(os.environ.get("VIRTUOSO_SPARQL_TIMEOUT") or 60.0)
            return cls(sparql_endpoint=endpoint, timeout=timeout)
        return None

    def query(self, q: str) -> list[dict[str, Any]]:
        """Run a SPARQL SELECT, return the raw bindings list.

        Each binding row is a dict mapping variable name → the
        unwrapped Literal/IRI string. Datatyped numerics are
        coerced to int/float; everything else stays as strings.

        Raises ``SparqlTimeout`` when the request times out, so
        callers can render a graceful "took too long, try again"
        instead of a 500. All other transport / HTTP errors stay
        as raised exceptions — the data-quality source already
        guards against ``None`` clients, and a hard error from a
        configured Virtuoso means something is genuinely broken.
        """
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.get(
                    self.sparql_endpoint,
                    params={"query": q},
                    headers={"Accept": "application/sparql-results+json"},
                )
                r.raise_for_status()
                results = r.json().get("results", {}).get("bindings", [])
        except httpx.ReadTimeout as exc:
            raise SparqlTimeout(
                f"SPARQL query exceeded {self.timeout}s",
            ) from exc
        return [self._unwrap(b) for b in results]

    @staticmethod
    def _unwrap(binding: dict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for var, val in binding.items():
            v = val.get("value")
            dt = val.get("datatype", "")
            if dt.endswith("integer") or dt.endswith("int"):
                out[var] = int(v)
            elif dt.endswith("decimal") or dt.endswith("double") or dt.endswith("float"):
                out[var] = float(v)
            else:
                out[var] = v
        return out
