"""Tiny synchronous client for the fontem-linguistics service.

Search calls ``POST /embed`` to vectorise the user's query for hybrid
(dense + sparse) matching. The dependency is soft in both directions:

* ``from_env`` returns ``None`` when ``LINGUISTICS_URL`` is unset, so
  the API boots in environments without the service;
* ``embed`` returns ``None`` on any transport/HTTP error and the caller
  falls back to lexical-only — a degraded search beats a 500 on the
  results page.

Timeout is short (3s default) because this sits on the interactive
search path.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LinguisticsClient:
    """Keyword-extraction client (stop-word removal, 24 EU languages)."""

    base_url: str
    timeout: float = 3.0

    @classmethod
    def from_env(cls) -> "LinguisticsClient | None":
        """Build from LINGUISTICS_URL; None when unset."""
        if base_url := os.environ.get("LINGUISTICS_URL"):
            timeout = float(os.environ.get("LINGUISTICS_TIMEOUT") or 3.0)
            return cls(base_url=base_url.rstrip("/"), timeout=timeout)
        return None

    def embed(self, text: str, backend: str = "minilm-local") -> tuple[list[float], str] | None:
        """Vector-embed ``text`` and return ``(vector, encoder_id)``.

        ``None`` on transport/HTTP failure so the caller can decide
        whether to fall back to lexical-only (search) or 5xx (embedding-
        essential paths). Timeout uses the client's configured value —
        which for interactive search should stay short (2-3s); a long
        embed timeout ties up the request thread and makes users wait.
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/embed",
                    json={"text": text[:8000], "backend": backend},
                )
                resp.raise_for_status()
                data = resp.json()
                vec = data.get("vector")
                enc = data.get("encoder_id")
                if not isinstance(vec, list) or not isinstance(enc, str):
                    logger.warning("linguistics /embed returned malformed payload")
                    return None
                return [float(x) for x in vec], enc
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("linguistics /embed unavailable: %s", exc)
            return None
