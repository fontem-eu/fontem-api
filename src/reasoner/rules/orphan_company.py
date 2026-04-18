"""
Rule: orphan-company — flag Company nodes with zero relationships.

**What it looks for**
    Company nodes where COUNT { (c)--() } = 0. These companies arrived
    via one data source (typically GLEIF) but never got linked to a
    contract, listing, sanction, beneficial-owner relationship, or
    any other node in the graph.

**Why it matters**
    - Completeness signal: after each ingest we can count orphans to
      see whether link-discovery is keeping up with base-entity load.
    - Matching candidates: an orphan is often an un-matched form of
      another entity already in the graph (different name spelling,
      different country code). The SAME_AS queue is the cousin rule
      that handles linking; orphan-company just surfaces the candidates.

**Confidence**
    1.0 — degree zero is directly observed, no ambiguity.

**Action**
    Review-only (auto_apply_threshold = NEVER_AUTO_APPLY). Orphaning
    is rarely a bug with a known fix — it's a signal for humans or
    downstream dedup rules to investigate. This rule does NOT mutate
    the graph.

**Payload** (per finding)
    - country: Company.country (alpha-3)
    - name:    Company.name
    - source:  whichever `source_*` property is non-null, for triage
"""
from __future__ import annotations

from typing import Iterable

from ..rule import NEVER_AUTO_APPLY, Finding, RuleContext


# Large-graph scans must not fetch every row — we page with SKIP/LIMIT
# and yield as we go so persistence can batch-write.
_SCAN_CYPHER = """
MATCH (c:Company)
WHERE COUNT { (c)--() } = 0
RETURN c.gmr_id AS gmr_id, c.name AS name, c.country AS country,
       c.source AS source
SKIP $skip LIMIT $limit
"""

_SCAN_TARGETED_CYPHER = """
UNWIND $ids AS gmr_id
MATCH (c:Company {gmr_id: gmr_id})
WHERE COUNT { (c)--() } = 0
RETURN c.gmr_id AS gmr_id, c.name AS name, c.country AS country,
       c.source AS source
"""

_PAGE_SIZE = 5_000


class OrphanCompanyRule:
    id = "orphan-company"
    description = (
        "Company nodes with no relationships at all. "
        "Typically arrived via a single source and never got linked."
    )
    severity = "warning"
    auto_apply_threshold = NEVER_AUTO_APPLY
    rule_categories = ["completeness"]

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        with ctx.neo4j.session() as session:
            if ctx.target_ids:
                # Targeted mode: only inspect the provided ids.
                for row in session.run(_SCAN_TARGETED_CYPHER, ids=ctx.target_ids):
                    yield self._finding_from_row(row)
                return

            # Full sweep: paginate so we don't load the whole result set.
            skip = 0
            while True:
                rows = list(session.run(
                    _SCAN_CYPHER, skip=skip, limit=_PAGE_SIZE,
                ))
                if not rows:
                    return
                for row in rows:
                    yield self._finding_from_row(row)
                skip += _PAGE_SIZE

    def _finding_from_row(self, row) -> Finding:
        gmr_id = row["gmr_id"]
        return Finding(
            rule_id=self.id,
            severity=self.severity,
            confidence=1.0,
            target_ids=[gmr_id],
            message=f"Orphan company: {row['name'] or gmr_id}",
            payload={
                "country": row["country"],
                "name": row["name"],
                "source": row["source"],
            },
        )


RULE = OrphanCompanyRule()
