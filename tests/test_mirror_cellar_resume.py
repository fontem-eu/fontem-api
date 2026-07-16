"""Verified-artifact resume: retries must not re-export from CELLAR."""
from __future__ import annotations

import gzip
import hashlib
import json

from src.etl.legislative.mirror_cellar import verified_artifact


def _write(tmp_path, tag, lines, sha_override=None):
    path = tmp_path / f"cellar-cdm-{tag}.nt.gz"
    sha = hashlib.sha256()
    with gzip.open(path, "wt", encoding="utf-8") as gz:
        for line in lines:
            gz.write(line + "\n")
            sha.update(line.encode() + b"\n")
    manifest = {"window": tag, "triples": len(lines),
                "sha256_uncompressed": sha_override or sha.hexdigest(),
                "artifact": path.name}
    (tmp_path / f"cellar-cdm-{tag}.manifest.json").write_text(
        json.dumps(manifest))
    return manifest


def test_verified_artifact_found(tmp_path):
    _write(tmp_path, "2007-01", ["<a> <b> <c> ."])
    m = verified_artifact(tmp_path, "2007-01")
    assert m is not None
    assert m["triples"] == 1


def test_missing_artifact_returns_none(tmp_path):
    assert verified_artifact(tmp_path, "2007-02") is None


def test_sha_mismatch_forces_reexport(tmp_path):
    _write(tmp_path, "2007-03", ["<a> <b> <c> ."], sha_override="0" * 64)
    assert verified_artifact(tmp_path, "2007-03") is None


def test_truncated_gzip_returns_none(tmp_path):
    _write(tmp_path, "2007-04", ["<a> <b> <c> ."])
    p = tmp_path / "cellar-cdm-2007-04.nt.gz"
    p.write_bytes(p.read_bytes()[:-8])
    assert verified_artifact(tmp_path, "2007-04") is None
