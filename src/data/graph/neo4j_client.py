"""Thin Neo4j connection wrapper used by GraphDataSource."""
from __future__ import annotations

import logging
import os

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Manages a single Neo4j driver for the lifetime of the process."""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._uri = uri or os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
        self._user = user or os.environ.get("NEO4J_USER", "neo4j")
        self._password = password or os.environ.get(
            "NEO4J_PASSWORD", ""
        )
        self._driver = GraphDatabase.driver(
            self._uri, auth=(self._user, self._password),
        )
        logger.info("Neo4jClient connected to %s", self._uri)

    def session(self, **config):
        """Return a new session (use as context manager).

        ``config`` is forwarded verbatim to the driver, so callers can ask
        for e.g. ``default_access_mode=READ_ACCESS`` — which the server
        enforces, rejecting any write attempted inside the transaction.
        """
        return self._driver.session(**config)

    def close(self):
        """Close the driver."""
        self._driver.close()
