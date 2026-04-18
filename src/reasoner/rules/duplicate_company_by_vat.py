"""
Rule: duplicate-company-by-vat — flag Company nodes sharing a VAT.

**What it looks for**
    Two or more distinct Company nodes that share the same non-null
    ``vat_number``. VAT numbers are governmentally issued per legal
    entity per country, so a shared VAT is a strong signal the two
    nodes refer to the same company.

**Why it matters**
    Duplicate entities split signals. A contract awarded to two
    "different" nodes with the same VAT looks like two small
    contracts when it's actually one. SAME_AS linking them collapses
    the signal back together.

**Confidence**
    0.9 — VAT collisions are rare but do happen (data-entry error in
    a source, historical splits, identifier reassignment). Not 1.0.

**Action**
    V1: review-only. `auto_apply_threshold` is set to 1.1 so the
    0.9-confidence findings are persisted for human review.

    The rule DOES implement ``apply()`` — it creates a SAME_AS edge
    between the two nodes with method="reasoner:duplicate-company-by-vat"
    and reviewed=false. To turn it on once we have confidence, drop
    the threshold to ≤ 0.9 in a follow-up PR.

**Payload** (per finding)
    - vat_number: the shared VAT
    - names:      list of Company names (one per target)
    - countries:  list of alpha-3 country codes (one per target)

**Notes**
    - We skip VAT numbers that appear on >10 companies, treating them
      as garbage (a well-known placeholder / mass data entry error).
      Those get a separate rule if we ever want to investigate.
    - We only emit one finding per VAT, not one per pair — the
      finding's target_ids lists ALL companies in the cluster.
"""
from __future__ import annotations

from typing import Iterable

from ..rule import Finding, RuleContext


_SCAN_CYPHER = """
MATCH (c:Company)
WHERE c.vat_number IS NOT NULL AND c.vat_number <> ''
WITH c.vat_number AS vat, collect(c) AS companies
WHERE size(companies) > 1 AND size(companies) <= 10
RETURN
  vat,
  [c IN companies | c.gmr_id] AS ids,
  [c IN companies | c.name] AS names,
  [c IN companies | c.country] AS countries
"""

_SCAN_TARGETED_CYPHER = """
UNWIND $ids AS gmr_id
MATCH (target:Company {gmr_id: gmr_id})
WHERE target.vat_number IS NOT NULL AND target.vat_number <> ''
WITH DISTINCT target.vat_number AS vat
MATCH (c:Company {vat_number: vat})
WITH vat, collect(c) AS companies
WHERE size(companies) > 1 AND size(companies) <= 10
RETURN
  vat,
  [c IN companies | c.gmr_id] AS ids,
  [c IN companies | c.name] AS names,
  [c IN companies | c.country] AS countries
"""

_APPLY_CYPHER = """
UNWIND $pairs AS p
MATCH (a:Company {gmr_id: p.a}), (b:Company {gmr_id: p.b})
MERGE (a)-[r:SAME_AS]-(b)
  ON CREATE SET
    r.method = 'reasoner:duplicate-company-by-vat',
    r.confidence = $confidence,
    r.detected_at = datetime(),
    r.reviewed = false
"""


class DuplicateCompanyByVatRule:
    id = "duplicate-company-by-vat"
    description = (
        "Distinct Company nodes sharing a non-null vat_number. "
        "Candidate SAME_AS pairs."
    )
    severity = "warning"
    # Review-only for V1. Lower to 0.9 to enable auto-apply of SAME_AS edges.
    auto_apply_threshold = 1.1
    rule_categories = ["dedup", "consistency"]

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        with ctx.neo4j.session() as session:
            query = _SCAN_TARGETED_CYPHER if ctx.target_ids else _SCAN_CYPHER
            params = {"ids": ctx.target_ids} if ctx.target_ids else {}
            for row in session.run(query, **params):
                ids = list(row["ids"])
                yield Finding(
                    rule_id=self.id,
                    severity=self.severity,
                    confidence=0.9,
                    target_ids=ids,
                    message=(
                        f"{len(ids)} companies share VAT {row['vat']}: "
                        + ", ".join(n or "<no-name>" for n in row["names"])
                    ),
                    payload={
                        "vat_number": row["vat"],
                        "names": list(row["names"]),
                        "countries": list(row["countries"]),
                    },
                )

    def apply(self, ctx: RuleContext, finding: Finding) -> None:
        """Create SAME_AS edges across the duplicate cluster.

        Creates pairwise edges (not star-shaped) because the graph
        model doesn't privilege any company node as "the canonical
        one" — any downstream rule that needs a single representative
        can pick one via SAME_AS traversal.
        """
        ids = sorted(finding.target_ids)
        pairs = [
            {"a": ids[i], "b": ids[j]}
            for i in range(len(ids))
            for j in range(i + 1, len(ids))
        ]
        if not pairs:
            return
        with ctx.neo4j.session() as session:
            session.run(_APPLY_CYPHER, pairs=pairs, confidence=finding.confidence)


RULE = DuplicateCompanyByVatRule()
