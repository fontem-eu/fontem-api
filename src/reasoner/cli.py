"""Reasoner CLI — the entrypoint orchestration invokes.

Two modes:

    python -m src.reasoner.cli sweep [--rules id1,id2] [--dry-run]
        Run the given rules (or all of them) against the whole graph.
        Default trigger for the V1 CronJob.

    python -m src.reasoner.cli evaluate --target-ids a,b,c [--rules ...]
        Run the given rules scoped to specific entity ids. Used by the
        future event-driven orchestration to handle "entity X was
        touched, re-evaluate".

The CLI doesn't schedule or loop — one invocation, one run. That's
orchestration's job.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid

from ..data.graph.neo4j_client import Neo4jClient
from .engine import Engine
from .persistence import Persistence
from .registry import Registry
from .rule import RuleContext


logger = logging.getLogger("reasoner")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.reasoner.cli")
    sub = p.add_subparsers(dest="mode", required=True)

    sweep = sub.add_parser(
        "sweep",
        help="Run rules across the whole graph.",
    )
    sweep.add_argument(
        "--rules",
        type=str,
        default="",
        help="Comma-separated rule ids. Empty = all registered rules.",
    )
    sweep.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen but don't touch Postgres or Neo4j.",
    )

    evaluate = sub.add_parser(
        "evaluate",
        help="Run rules scoped to specific target ids.",
    )
    evaluate.add_argument(
        "--target-ids",
        type=str,
        required=True,
        help="Comma-separated list of entity ids to evaluate.",
    )
    evaluate.add_argument("--rules", type=str, default="")
    evaluate.add_argument("--dry-run", action="store_true")

    sub.add_parser("list-rules", help="List registered rule ids.")
    return p


def _select_rules(registry: Registry, rules_arg: str):
    if not rules_arg:
        return list(registry.all())
    ids = [r.strip() for r in rules_arg.split(",") if r.strip()]
    return registry.by_ids(ids)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _parser().parse_args(argv)

    registry = Registry()

    if args.mode == "list-rules":
        for rule in registry.all():
            print(f"{rule.id}\t{rule.severity}\t{rule.description}")
        return 0

    selected = _select_rules(registry, args.rules)
    if not selected:
        logger.info("No rules to run (registry is empty). Exit 0.")
        return 0

    run_id = str(uuid.uuid4())
    ctx = RuleContext(
        neo4j=Neo4jClient(),
        run_id=run_id,
        target_ids=(
            [t.strip() for t in args.target_ids.split(",") if t.strip()]
            if args.mode == "evaluate" else None
        ),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    persistence = Persistence() if not ctx.dry_run else _NullPersistence()
    engine = Engine(persistence)

    logger.info(
        "reasoner run_id=%s mode=%s rules=%s dry_run=%s",
        run_id, args.mode, [r.id for r in selected], ctx.dry_run,
    )

    total_seen = total_persisted = total_applied = 0
    any_errors = False
    for rule in selected:
        result = engine.run_rule(rule, ctx)
        total_seen += result.findings_seen
        total_persisted += result.findings_persisted
        total_applied += result.findings_auto_applied
        if result.errors:
            any_errors = True
            for err in result.errors:
                logger.error("rule=%s error=%s", rule.id, err)
        logger.info(
            "rule=%s seen=%d persisted=%d auto_applied=%d errors=%d",
            rule.id,
            result.findings_seen,
            result.findings_persisted,
            result.findings_auto_applied,
            len(result.errors),
        )

    logger.info(
        "DONE run_id=%s total_seen=%d persisted=%d auto_applied=%d",
        run_id, total_seen, total_persisted, total_applied,
    )
    return 1 if any_errors else 0


class _NullPersistence:
    """No-op persistence used when --dry-run is set."""

    def upsert_finding(self, finding):
        return None

    def upsert_many(self, findings):
        return 0

    def record_audit(self, *_args, **_kwargs):
        return None

    def mark_applied(self, *_args, **_kwargs):
        return None


if __name__ == "__main__":
    sys.exit(main())
