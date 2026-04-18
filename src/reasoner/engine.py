"""The Engine runs rules and dispatches their findings.

For each rule:
  for finding in rule.evaluate(ctx):
      if finding.confidence >= rule.auto_apply_threshold AND rule has apply():
          rule.apply(ctx, finding)
          mark_applied(finding) + audit
      else:
          persist finding

The engine knows NOTHING about orchestration — it doesn't schedule,
it doesn't watch queues, it just runs what you hand it. Orchestration
is whatever invokes this (CronJob in V1, event-driven worker in V2).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from .persistence import Persistence
from .rule import Finding, Rule, RuleContext

logger = logging.getLogger(__name__)


@dataclass
class EngineResult:
    rule_id: str
    findings_seen: int = 0
    findings_persisted: int = 0
    findings_auto_applied: int = 0
    errors: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    def run_rule(self, rule: Rule, ctx: RuleContext) -> EngineResult:
        result = EngineResult(rule_id=rule.id)
        has_apply = hasattr(rule, "apply") and callable(getattr(rule, "apply", None))
        to_persist: list[Finding] = []

        try:
            findings: Iterable[Finding] = rule.evaluate(ctx)
            for finding in findings:
                result.findings_seen += 1
                should_apply = (
                    has_apply
                    and finding.confidence >= rule.auto_apply_threshold
                )
                if should_apply:
                    if ctx.dry_run:
                        logger.info(
                            "[dry-run] would auto-apply %s for targets %s",
                            rule.id, finding.target_ids,
                        )
                    else:
                        try:
                            rule.apply(ctx, finding)  # type: ignore[attr-defined]
                            self._persistence.record_audit(
                                rule_id=rule.id,
                                finding_key=finding.finding_key(),
                                run_id=ctx.run_id,
                                action="auto-applied",
                                summary=finding.message,
                                payload={
                                    "target_ids": finding.target_ids,
                                    "confidence": finding.confidence,
                                },
                            )
                            # Still persist the finding so it shows in the
                            # review UI with status='applied'.
                            self._persistence.upsert_finding(finding)
                            self._persistence.mark_applied(
                                rule.id, finding.finding_key(),
                            )
                            result.findings_auto_applied += 1
                        except Exception as exc:  # pylint: disable=broad-except
                            logger.exception(
                                "apply() failed for %s: %s", rule.id, exc,
                            )
                            result.errors.append(f"apply: {exc}")
                            # Fall back to persistence so a human can see it.
                            to_persist.append(finding)
                else:
                    to_persist.append(finding)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("evaluate() failed for %s: %s", rule.id, exc)
            result.errors.append(f"evaluate: {exc}")

        if to_persist and not ctx.dry_run:
            n = self._persistence.upsert_many(to_persist)
            result.findings_persisted += n
        elif to_persist:
            logger.info(
                "[dry-run] would persist %d findings for %s",
                len(to_persist), rule.id,
            )
        return result
