"""Contract for a reasoner rule and the Finding it produces.

A Rule inspects some slice of the graph and, for anything that
violates a business constraint, emits Findings. Findings have a
confidence; if confidence >= rule.auto_apply_threshold AND the rule
provides an ``apply()`` method, the engine will call apply() to
mutate the graph. Otherwise the finding is persisted to Postgres for
human review.

Example:

    class OrphanCompanyRule:
        id = "orphan-company"
        description = "Companies with no relationships"
        severity = "warning"
        auto_apply_threshold = 2.0   # effectively: never auto-apply
        rule_categories = ["completeness"]

        def evaluate(self, ctx):
            with ctx.neo4j.session() as session:
                for row in session.run(CYPHER).data():
                    yield Finding(
                        rule_id=self.id,
                        severity=self.severity,
                        confidence=1.0,
                        target_ids=[row["gmr_id"]],
                        message=f"Orphan company {row['name']}",
                        payload={"country": row["country"]},
                    )
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol, runtime_checkable


# Sentinel confidence threshold that means "never auto-apply".
# Used by rules that are review-only.
NEVER_AUTO_APPLY = 2.0


@dataclass(frozen=True)
class Finding:
    """A single thing a rule noticed about the graph."""

    rule_id: str
    severity: str                # "info" | "warning" | "error"
    confidence: float            # 0.0–1.0
    target_ids: list[str]        # the entities this finding is about
    message: str                 # human-readable one-liner
    payload: dict[str, Any] = field(default_factory=dict)

    def finding_key(self) -> str:
        """Stable hash used for dedup in Postgres.

        Two Finding instances for the same rule + same set of targets
        collapse to a single row so re-running a sweep is idempotent.
        Target ordering doesn't matter.
        """
        canonical = json.dumps(
            {"rule_id": self.rule_id, "targets": sorted(self.target_ids)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class RuleContext:
    """Per-run state passed into each rule's evaluate() / apply().

    Rules must NOT keep references to ctx past the evaluate() call —
    the engine may close resources between invocations.
    """

    neo4j: Any           # Neo4jClient (avoid import cycle)
    run_id: str
    target_ids: Optional[list[str]] = None
    dry_run: bool = False


@runtime_checkable
class Rule(Protocol):
    """Protocol every rule must satisfy.

    Fields (read-only after construction):
      id:                     unique slug (used as primary key for
                              findings)
      description:            plain-English summary
      severity:               default severity for findings; each
                              Finding can override via its own field
      auto_apply_threshold:   findings with confidence >= this AND a
                              rule that provides apply() will be
                              auto-applied. Set to NEVER_AUTO_APPLY
                              (or > 1.0) to disable auto-apply entirely
      rule_categories:        free-form tags used in documentation +
                              filtering (e.g. "completeness",
                              "consistency", "dedup")

    Methods:
      evaluate(ctx) -> Iterable[Finding]:
        Required. Walks the graph (or the ctx.target_ids subset) and
        yields findings.

      apply(ctx, finding) -> None:
        Optional. Only called on findings whose confidence meets the
        rule's threshold. Must be idempotent — if called twice, same
        outcome.
    """

    id: str
    description: str
    severity: str
    auto_apply_threshold: float
    rule_categories: list[str]

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]: ...

    # apply() is optional; rules without it are review-only by construction.
