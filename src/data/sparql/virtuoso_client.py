"""Tiny synchronous SPARQL client over httpx.

The data-quality dashboard runs SPARQL SELECT queries against
Virtuoso (sanctions stats, top-regimes, etc.) and stays
deliberately small: no persistent client, no retries, no
streaming. Each call opens a short-lived httpx.Client because
the request rate is low — once-a-minute dashboard refresh, not
hot-path traffic.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class VirtuosoClient:
    """Read-only SPARQL client.

    Only ``query`` is exposed; updates land via the loader's own
    SHACL-validated graph-crud path, not through this client.
    """

    sparql_endpoint: str
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "VirtuosoClient | None":
        """Build from VIRTUOSO_SPARQL_URL. Returns None if unset.

        Returning None lets the API server boot in environments
        that haven't enabled Virtuoso yet — the data quality
        source falls back to Neo4j-only behaviour rather than
        failing at startup.
        """
        if endpoint := os.environ.get("VIRTUOSO_SPARQL_URL"):
            return cls(sparql_endpoint=endpoint)
        return None

    def query(self, q: str) -> list[dict[str, Any]]:
        """Run a SPARQL SELECT, return the raw bindings list.

        Each binding row is a dict mapping variable name → the
        unwrapped Literal/IRI string. Datatyped numerics are
        coerced to int/float; everything else stays as strings.
        """
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(
                self.sparql_endpoint,
                params={"query": q},
                headers={"Accept": "application/sparql-results+json"},
            )
            r.raise_for_status()
            results = r.json().get("results", {}).get("bindings", [])
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
