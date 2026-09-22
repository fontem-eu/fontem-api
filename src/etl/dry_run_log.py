"""An in-memory stand-in for ``fontem_events.EventLog``.

``--dry-run`` runs the whole loader — discovery, parsing, matching, the
cleaning stage, payload building — and lands nothing. Payloads are
still validated against their schema exactly as the producer would,
so a dry run also proves every event it would have written is
well-formed. It needs no ``EVENTS_DATABASE_URL``.

Only counts are kept unbounded; a capped sample of events stays in
memory for inspection (a full rescan is millions of events).
"""
from __future__ import annotations

import contextlib
from collections import Counter
from typing import Any, Iterator

from fontem_event_schemas import validate

DEFAULT_SAMPLE_SIZE = 1000


class DryRunEventLog:
    """Same surface as ``EventLog``: ``batch()`` + ``close()``."""

    def __init__(self, sample_size: int = DEFAULT_SAMPLE_SIZE) -> None:
        self.sample_size = sample_size
        self.counts: Counter = Counter()
        self.batches = 0
        self.sample: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def batch(self, batch_id, producer: str) -> Iterator["DryRunBatch"]:
        """Events emitted in the block are kept only if it exits cleanly —
        the same all-or-nothing rule the real transaction enforces."""
        staged = DryRunBatch(self, batch_id, producer)
        yield staged
        self.batches += 1
        for event in staged.events:
            self.counts[event["event_type"]] += 1
            if len(self.sample) < self.sample_size:
                self.sample.append(event)

    def close(self) -> None:
        """Nothing to release."""

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class DryRunBatch:
    """Per-batch emit helper mirroring ``EventBatch``."""

    def __init__(self, log: DryRunEventLog, batch_id, producer: str) -> None:
        self._log = log
        self._batch_id = batch_id
        self._producer = producer
        self.events: list[dict[str, Any]] = []

    @property
    def count(self) -> int:
        return len(self.events)

    def upsert(self, event_type: str, *, iri: str, domain: str,
               payload: dict[str, Any], schema_version: int = 1) -> int:
        return self._emit(event_type, iri=iri, domain=domain, op="upsert",
                          payload=payload, schema_version=schema_version)

    def delete(self, event_type: str, *, iri: str, domain: str,
               schema_version: int = 1) -> int:
        return self._emit(event_type, iri=iri, domain=domain, op="delete",
                          payload={"iri": iri}, schema_version=schema_version)

    def _emit(self, event_type: str, *, iri: str, domain: str, op: str,  # pylint: disable=too-many-arguments
              payload: dict[str, Any], schema_version: int) -> int:
        if op == "upsert":
            validate(event_type, schema_version, payload)
        self.events.append({
            "event_type": event_type, "iri": iri, "domain": domain, "op": op,
            "payload": payload, "schema_version": schema_version,
            "batch_id": str(self._batch_id), "producer": self._producer,
        })
        return len(self.events)
