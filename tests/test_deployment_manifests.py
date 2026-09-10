"""Manifest-level traps that no Python test would otherwise catch.

These are chart templates, so a mistake here does not fail the build or
the test suite -- it fails at sync time, in a cluster, as a PreSync hook
that blocks the whole deployment. fontem-api sat undeployable in testing
that way, with ArgoCD reporting only "one or more synchronization tasks
completed unsuccessfully".
"""
from __future__ import annotations

import pathlib
import re

_TEMPLATES = pathlib.Path("deployment/templates")


def _container_arg_blocks() -> list[tuple[pathlib.Path, str]]:
    """Every template that puts a shell script in container args."""
    return [
        (p, p.read_text(encoding="utf-8"))
        for p in _TEMPLATES.rglob("*.yaml")
        if "args:" in p.read_text(encoding="utf-8")
    ]


def test_no_bare_dollar_quote_in_container_args():
    """Kubernetes expands container args itself: $(VAR) is substituted
    and `$$` is the escape for a literal `$`.

    So a Postgres dollar-quoted block written `DO $$ ... END $$;` reaches
    psql as `DO $ ... END $;` and dies with `syntax error at or near
    "$"`. The shell heredoc around it is single-quoted, so the shell is
    not the culprit and none of the usual quoting fixes help -- which is
    what makes this cost an afternoon rather than a minute.

    A TAGGED dollar-quote (`$mig$`) contains no `$$` sequence and passes
    through untouched. Use one.
    """
    offenders = []
    for path, text in _container_arg_blocks():
        for i, line in enumerate(text.splitlines(), 1):
            # Comments are not passed to the container, and the comment
            # explaining this very trap has to be able to name it.
            if line.strip().startswith("#"):
                continue
            if "$$" in line:
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert not offenders, (
        "bare $$ in container args -- Kubernetes collapses it to a single "
        "$ before the container sees it. Use a tagged dollar-quote "
        f"($mig$): {offenders}"
    )


def test_migration_hooks_do_not_retry():
    """A migration that failed once should be read by a human, not
    retried against a database it may have half-changed."""
    for path, text in _container_arg_blocks():
        if "hook: PreSync" not in text:
            continue
        assert re.search(r"backoffLimit:\s*0", text), (
            f"{path} is a PreSync migration hook without backoffLimit: 0"
        )


def test_job_hooks_disable_the_mesh_sidecar():
    """The linkerd sidecar never exits, so an injected Job never reaches
    Complete and ArgoCD waits on the hook forever."""
    for path, text in _container_arg_blocks():
        if "kind: Job" not in text:
            continue
        assert "linkerd.io/inject: disabled" in text, (
            f"{path} defines a Job without disabling mesh injection; "
            "ArgoCD would wait on it forever"
        )
