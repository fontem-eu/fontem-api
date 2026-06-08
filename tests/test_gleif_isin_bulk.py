"""Unit tests for the GLEIF ISIN-LEI bulk loader.

The OpenFIGI loader's tests stub ``load_isin_mapping`` to return ``{}``
so the network never gets hit; these tests cover the bulk module's
own behavior end-to-end with an in-memory zip, no network."""
# pylint: disable=missing-function-docstring,protected-access
import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.etl import _gleif_isin_bulk


def _make_zip(rows, header=("LEI", "ISIN"), name="lei-isin-test.csv"):
    """Build an in-memory zip with one CSV entry. Returns the bytes."""
    csv_buf = io.StringIO()
    csv_buf.write(",".join(header) + "\n")
    for lei, isin in rows:
        csv_buf.write(f"{lei},{isin}\n")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, csv_buf.getvalue())
    return zbuf.getvalue()


def _write_zip(tmp_path: Path, rows, **kw) -> Path:
    out = tmp_path / "isin-lei-20260608T071510.zip"
    out.write_bytes(_make_zip(rows, **kw))
    return out


# ── stream_pairs ──────────────────────────────────────────────────


def test_stream_pairs_yields_rows(tmp_path):
    zp = _write_zip(tmp_path, [("L1", "I1"), ("L1", "I2"), ("L2", "I3")])
    assert list(_gleif_isin_bulk.stream_pairs(zp)) == [
        ("L1", "I1"), ("L1", "I2"), ("L2", "I3"),
    ]


def test_stream_pairs_rejects_bad_header(tmp_path):
    """Header drift must fail loudly. Silent mis-mapping would corrupt
    every downstream Listing for the run."""
    zp = _write_zip(tmp_path, [("L1", "I1")], header=("ISIN", "LEI"))
    with pytest.raises(RuntimeError, match="unexpected GLEIF CSV header"):
        list(_gleif_isin_bulk.stream_pairs(zp))


def test_stream_pairs_rejects_zip_with_no_csv(tmp_path):
    out = tmp_path / "empty.zip"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("readme.txt", "no csv here")
    with pytest.raises(RuntimeError, match="expected exactly one .csv"):
        list(_gleif_isin_bulk.stream_pairs(out))


def test_stream_pairs_rejects_zip_with_multiple_csvs(tmp_path):
    """Two CSVs is a contract violation we want to know about — the
    naïve first-entry pick would silently drop the second file."""
    out = tmp_path / "multi.zip"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("a.csv", "LEI,ISIN\n")
        zf.writestr("b.csv", "LEI,ISIN\n")
    with pytest.raises(RuntimeError, match="expected exactly one .csv"):
        list(_gleif_isin_bulk.stream_pairs(out))


def test_stream_pairs_rejects_short_row(tmp_path):
    """A row with fewer than 2 columns is malformed. Silent drop here
    would lose real LEI→ISIN mappings — better to fail loudly so we
    notice and fix the upstream."""
    zp = tmp_path / "isin-lei-20260608T071510.zip"
    csv_text = "LEI,ISIN\nL1,I1\nshortrow\n"
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lei-isin-test.csv", csv_text)
    zp.write_bytes(zbuf.getvalue())
    with pytest.raises(RuntimeError, match="malformed GLEIF row"):
        list(_gleif_isin_bulk.stream_pairs(zp))


# ── load_isin_mapping ─────────────────────────────────────────────


def test_load_isin_mapping_filters_to_target_leis(tmp_path, monkeypatch):
    """Filter contract: only target LEIs land in the dict; non-target
    LEIs in the file are streamed past."""
    zp = _write_zip(
        tmp_path, [
            ("L1", "I1"), ("L1", "I2"), ("L2", "I3"), ("L3", "I4"),
        ],
    )
    monkeypatch.setattr(
        _gleif_isin_bulk, "fetch_latest_zip",
        lambda cache_dir=None, http_client=None: zp,
    )
    out = _gleif_isin_bulk.load_isin_mapping({"L1", "L2"})
    assert out == {"L1": ["I1", "I2"], "L2": ["I3"]}


def test_load_isin_mapping_target_none_keeps_all(tmp_path, monkeypatch):
    zp = _write_zip(
        tmp_path, [("L1", "I1"), ("L2", "I2"), ("L3", "I3")],
    )
    monkeypatch.setattr(
        _gleif_isin_bulk, "fetch_latest_zip",
        lambda cache_dir=None, http_client=None: zp,
    )
    out = _gleif_isin_bulk.load_isin_mapping(None)
    assert out == {"L1": ["I1"], "L2": ["I2"], "L3": ["I3"]}


def test_load_isin_mapping_target_empty_short_circuits(monkeypatch):
    """Empty cohort must NOT touch the network or filesystem. Spy on
    fetch_latest_zip to assert it was never called."""
    spy = MagicMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(_gleif_isin_bulk, "fetch_latest_zip", spy)
    assert not _gleif_isin_bulk.load_isin_mapping(set())
    spy.assert_not_called()


# ── fetch_latest_zip ──────────────────────────────────────────────


def test_fetch_latest_zip_same_day_cache_hit_skips_network(tmp_path, monkeypatch):
    """The offline-cache fallback: if a same-day file is on disk we
    must return it without calling /latest. Spy on _resolve_latest."""
    from datetime import datetime, timezone  # pylint: disable=import-outside-toplevel
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    cached = tmp_path / f"isin-lei-{today}T071510.zip"
    cached.write_bytes(_make_zip([("L1", "I1")]))

    spy = MagicMock(side_effect=AssertionError("must not call /latest"))
    monkeypatch.setattr(_gleif_isin_bulk, "_resolve_latest", spy)

    out = _gleif_isin_bulk.fetch_latest_zip(cache_dir=str(tmp_path))
    assert out == cached
    spy.assert_not_called()


def test_fetch_latest_zip_resolves_then_downloads_on_miss(tmp_path, monkeypatch):
    """Cache miss path: resolve /latest, then download. Confirms both
    helpers are called exactly once with the resolved filename."""
    filename = "isin-lei-20260608T071510.zip"
    download_url = "https://example.invalid/path/to/" + filename
    resolve_spy = MagicMock(return_value=(filename, download_url))
    monkeypatch.setattr(_gleif_isin_bulk, "_resolve_latest", resolve_spy)

    def _fake_download(url, out, http_client):
        del url, http_client
        out.write_bytes(_make_zip([("L1", "I1")]))
        return out
    download_spy = MagicMock(side_effect=_fake_download)
    monkeypatch.setattr(_gleif_isin_bulk, "_download", download_spy)

    out = _gleif_isin_bulk.fetch_latest_zip(cache_dir=str(tmp_path))
    assert out.name == filename
    resolve_spy.assert_called_once()
    download_spy.assert_called_once()


# ── _resolve_latest fileName validation ───────────────────────────


def test_resolve_latest_rejects_path_traversal_filename():
    """Path-traversal in the GLEIF-controlled fileName attribute must
    not be accepted. The cache-dir join would otherwise escape under
    the attacker's filename."""
    client = MagicMock()
    resp = MagicMock()
    resp.json.return_value = {
        "data": {"attributes": {
            "fileName": "../../etc/passwd",
            "downloadLink": "https://example.invalid/x",
        }},
    }
    resp.raise_for_status.return_value = None
    client.get.return_value = resp
    with pytest.raises(RuntimeError, match="untrusted GLEIF fileName"):
        _gleif_isin_bulk._resolve_latest(client)


def test_resolve_latest_rejects_absolute_path_filename():
    client = MagicMock()
    resp = MagicMock()
    resp.json.return_value = {
        "data": {"attributes": {
            "fileName": "/etc/passwd",
            "downloadLink": "https://example.invalid/x",
        }},
    }
    resp.raise_for_status.return_value = None
    client.get.return_value = resp
    with pytest.raises(RuntimeError, match="untrusted GLEIF fileName"):
        _gleif_isin_bulk._resolve_latest(client)


def test_resolve_latest_accepts_canonical_filename():
    client = MagicMock()
    resp = MagicMock()
    resp.json.return_value = {
        "data": {"attributes": {
            "fileName": "isin-lei-20260608T071510.zip",
            "downloadLink": "https://example.invalid/x",
        }},
    }
    resp.raise_for_status.return_value = None
    client.get.return_value = resp
    name, url = _gleif_isin_bulk._resolve_latest(client)
    assert name == "isin-lei-20260608T071510.zip"
    assert url == "https://example.invalid/x"


# ── _prune_old_caches ─────────────────────────────────────────────


def test_prune_old_caches_keeps_newest_only(tmp_path):
    """After a download we keep the 2 newest cache files; older ones
    get unlinked. Mtime-ordered, best-effort, never aborts."""
    import os, time  # pylint: disable=import-outside-toplevel,multiple-imports
    files = []
    for i in range(5):
        f = tmp_path / f"isin-lei-2026060{i}T071510.zip"
        f.write_bytes(b"x")
        # Force distinct mtimes
        os.utime(f, (time.time() - (5 - i), time.time() - (5 - i)))
        files.append(f)
    pruned = _gleif_isin_bulk._prune_old_caches(tmp_path, keep=2)
    assert pruned == 3
    remaining = sorted(p.name for p in tmp_path.glob("isin-lei-*.zip"))
    # Newest two by mtime are the last two we touched
    assert remaining == [files[-2].name, files[-1].name]
