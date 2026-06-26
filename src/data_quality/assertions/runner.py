"""Execute the assertion catalog against a live environment.

The runner is connection-agnostic: it takes a Cypher runner and a SQL
runner (callables that take a query and return the single result row as
a dict). The CLI wires these to the real Neo4j driver + events Postgres;
tests wire them to in-memory fakes. This keeps every assertion unit-
testable without a database.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from src.data_quality.assertions.catalog import ASSERTIONS, BLOCK, Assertion

RowRunner = Callable[[str], Mapping[str, Any]]

# Result statuses.
PASS = "pass"
FAIL = "fail"      # a BLOCK assertion that did not hold → fails the Job
WARN = "warn"      # a WARN assertion that did not hold → reported, non-fatal
ERROR = "error"    # the query itself blew up (bad cypher/sql, no connection)


@dataclass(frozen=True)
class AssertionResult:
    id: str
    family: str
    title: str
    severity: str
    status: str
    observed: str

    @property
    def ok(self) -> bool:
        return self.status == PASS


def evaluate_assertion(
    assertion: Assertion,
    cypher: RowRunner,
    sql: RowRunner,
    consistency: "RowRunner | None" = None,
) -> AssertionResult:
    """Run one assertion and classify the outcome."""
    runner = {"cypher": cypher, "sql": sql, "consistency": consistency}.get(
        assertion.engine)
    if runner is None:
        # An assertion whose engine has no runner wired (e.g. the cross-store
        # consistency engine when Virtuoso is unconfigured) is surfaced, not
        # crashed: BLOCK errors, WARN warns.
        status = ERROR if assertion.severity == BLOCK else WARN
        return AssertionResult(
            assertion.id, assertion.family, assertion.title,
            assertion.severity, status, f"no runner for engine {assertion.engine!r}",
        )
    try:
        row = runner(assertion.query) or {}
        held, observed = assertion.evaluate(row)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # A broken query/connection is itself a quality failure we want
        # surfaced, not a crash. BLOCK engines error; WARN engines warn.
        status = ERROR if assertion.severity == BLOCK else WARN
        return AssertionResult(
            assertion.id, assertion.family, assertion.title,
            assertion.severity, status, f"{type(exc).__name__}: {str(exc)[:160]}",
        )
    if held:
        status = PASS
    else:
        status = FAIL if assertion.severity == BLOCK else WARN
    return AssertionResult(
        assertion.id, assertion.family, assertion.title,
        assertion.severity, status, observed,
    )


def run_catalog(
    cypher: RowRunner,
    sql: RowRunner,
    assertions: "list[Assertion] | None" = None,
    consistency: "RowRunner | None" = None,
) -> "list[AssertionResult]":
    """Run every assertion (or a supplied subset) and return results."""
    return [evaluate_assertion(a, cypher, sql, consistency)
            for a in (assertions or ASSERTIONS)]


def summarise(results: "list[AssertionResult]") -> dict[str, int]:
    counts = {PASS: 0, FAIL: 0, WARN: 0, ERROR: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def exit_code(results: "list[AssertionResult]") -> int:
    """Non-zero iff any BLOCK assertion failed or errored — fails the Job."""
    return 1 if any(r.status in (FAIL, ERROR) for r in results) else 0


_GLYPH = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", ERROR: "ERR "}


def format_report(results: "list[AssertionResult]", env_label: str = "") -> str:
    """Human-readable grouped report for the Job log."""
    lines: list[str] = []
    header = "Data-quality assertions"
    if env_label:
        header += f" — {env_label}"
    lines.append(header)
    lines.append("=" * max(len(header), 60))
    family_order: list[str] = []
    for r in results:
        if r.family not in family_order:
            family_order.append(r.family)
    for fam in family_order:
        fam_results = [r for r in results if r.family == fam]
        sev = fam_results[0].severity.upper()
        lines.append("")
        lines.append(f"[{fam}]  ({sev})")
        for r in fam_results:
            lines.append(f"  {_GLYPH[r.status]}  {r.id:<42} {r.observed}")
    c = summarise(results)
    lines.append("")
    lines.append("-" * 60)
    lines.append(
        f"{len(results)} assertions: "
        f"{c[PASS]} pass, {c[FAIL]} fail, {c[WARN]} warn, {c[ERROR]} error"
    )
    verdict = "FAILED" if exit_code(results) else "OK"
    lines.append(f"Gate: {verdict} (block-tier failures fail the run; warn-tier do not)")
    return "\n".join(lines)
