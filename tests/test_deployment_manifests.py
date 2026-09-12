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


def test_the_search_migration_carries_every_column_the_sink_writes():
    """The sink INSERTs name_lex_i18n and parts; a hook that does not add
    them turns every batch into a dead letter on a fresh database."""
    hook = (_TEMPLATES / "search-migrate-job.yaml").read_text(encoding="utf-8")
    for column in ("name_lex_i18n tsvector", "parts jsonb"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in hook, \
            f"search-migrate does not add {column}"


#: Registries a Pod may pull from: our own, and the Nexus pull-through
#: mirrors. Everything else runs only while its layers happen to be cached
#: on the node -- which is how minio went ImagePullBackOff in fontem-shared
#: (2026-09-12) once Docker Hub stopped serving it anonymously, taking the
#: shared environment's health, and every prod promotion, with it.
_MIRRORS = (
    "contribute.void42.internal/", "dockerhub.void42.internal/",
    "mcr.void42.internal/", "lscr.void42.internal/", "ghcr.void42.internal/",
    "cgr.void42.internal/", "quay.void42.internal/", "l5d.void42.internal/",
)


def _image_ref(line: str) -> "str | None":
    """The concrete image in an `image:` / `repository:` line, if any.
    Templated values resolve to a values entry this same scan covers."""
    stripped = line.strip()
    for key in ("image:", "repository:"):
        if stripped.startswith(key):
            ref = stripped[len(key):].strip().strip("\"'")
            return ref if ref and "{{" not in ref else None
    return None


def _declared_images() -> list[tuple[pathlib.Path, str]]:
    """Every concrete image reference in every chart."""
    files = sorted(f for chart in pathlib.Path(".").glob("deployment*/")
                   for f in chart.rglob("*.yaml"))
    return [(f, ref) for f in files
            for ref in [_image_ref(line) for line in f.read_text(encoding="utf-8").splitlines()]
            if ref]


def test_every_chart_image_comes_from_an_internal_mirror():
    """A public-registry pin is a time bomb: it works until the node loses
    the layers. Add the image to a Nexus proxy (or mirror it into
    contribute.void42.internal) rather than widening this list."""
    images = _declared_images()
    assert images, "found no image references to check"
    offenders = [(str(f), ref) for f, ref in images if not ref.startswith(_MIRRORS)]
    assert not offenders, f"images not pulled through a mirror: {offenders}"
