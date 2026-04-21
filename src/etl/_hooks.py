"""Post-ETL consolidator hooks.

Each loader calls notify_consolidator() at the end with the entity-type and
ids it just wrote. The consolidator then runs its rule pipeline against
each id and persists the decisions.

Best-effort by design: a hook failure NEVER breaks an ETL run. If the
consolidator is down or unreachable, the consolidation just doesn't happen
for that batch — a nightly catch-up sweep would handle drift.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

import httpx

CONSOLIDATOR_URL = os.environ.get(
    "CONSOLIDATOR_URL",
    "http://gmr-consolidator.gmr.svc.cluster.local:8000",
)
HOOK_TIMEOUT = float(os.environ.get("CONSOLIDATOR_HOOK_TIMEOUT", "5"))

log = logging.getLogger(__name__)


def notify_consolidator(entity_type: str, ids: Iterable[str]) -> None:
    """Fire a /consolidate/batch call. Swallow any error."""
    ids = [i for i in ids if i]  # drop None / empty
    if not ids:
        return
    if not CONSOLIDATOR_URL:
        return
    try:
        with httpx.Client(timeout=HOOK_TIMEOUT) as client:
            r = client.post(
                f"{CONSOLIDATOR_URL}/consolidate/batch",
                json={"entity_type": entity_type, "ids": ids},
            )
            r.raise_for_status()
            log.info(
                "consolidator: notified %s ids=%d → %s", entity_type, len(ids), r.status_code
            )
    except Exception as exc:  # pragma: no cover
        log.warning("consolidator: notify failed for %s: %s", entity_type, exc)
