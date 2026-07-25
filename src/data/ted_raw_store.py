"""Immutable raw-XML store for TED notices (the "store everything" backstop).

The event log (``events.entity_events``) holds a *curated* projection of
each notice — the ~14 fields the builder lists. Everything else the
parser saw, and the raw XML itself, was historically discarded, so
wanting a field we did not originally curate (e.g. the NUTS
place-of-performance) forced a re-fetch from TED.

This module persists the raw notice XML, gzip-compressed, in object
storage keyed by publication-number. With it, any *future* field is a
local re-parse of stored bytes — never a TED round-trip. The parsed
event payload stays the projection-optimised view; this is the
full-fidelity source of record beneath it.

Configuration (all from env; unset ⇒ the store is a graceful no-op so
ingest never blocks on object-storage availability):
  * ``TED_RAW_ENDPOINT``   host:port of the S3/minio endpoint
  * ``TED_RAW_ACCESS_KEY`` / ``TED_RAW_SECRET_KEY``
  * ``TED_RAW_BUCKET``     bucket name (default ``ted-raw``)
  * ``TED_RAW_SECURE``     "1" for TLS (default off; in-cluster minio is plaintext)
"""
from __future__ import annotations

import gzip
import io
from pathlib import Path
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_BUCKET = "ted-raw"


def _safe_key(identifier: str) -> str:
    """Object key for a notice. Publication-number ("24782-2025") or the
    notice UUID — both are filesystem/S3-safe already; guard anyway."""
    return identifier.strip().replace("/", "_") + ".xml.gz"


class TedRawStore:
    """Thin gzip-on-write wrapper over a minio/S3 bucket. Never raises to
    the caller: a storage failure logs and degrades to a no-op so the
    loader keeps ingesting (the parsed event is still the durable record;
    a missing raw blob only costs a future re-fetch)."""

    def __init__(self, client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> "TedRawStore | None":
        endpoint = os.environ.get("TED_RAW_ENDPOINT")
        access = os.environ.get("TED_RAW_ACCESS_KEY")
        secret = os.environ.get("TED_RAW_SECRET_KEY")
        if not (endpoint and access and secret):
            logger.info("TED raw store not configured (TED_RAW_* unset); "
                        "raw XML will not be persisted")
            return None
        try:
            from minio import Minio  # pylint: disable=import-outside-toplevel
        except ImportError:
            logger.warning("minio package unavailable; raw XML not persisted")
            return None
        bucket = os.environ.get("TED_RAW_BUCKET", _DEFAULT_BUCKET)
        secure = os.environ.get("TED_RAW_SECURE", "").lower() in ("1", "true", "yes")
        client = Minio(endpoint, access_key=access, secret_key=secret, secure=secure)
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            logger.exception("TED raw store: bucket check/create failed; disabling")
            return None
        logger.info("TED raw store ready: %s/%s", endpoint, bucket)
        return cls(client, bucket)

    def put(self, identifier: str, xml_bytes: bytes) -> bool:
        """Persist one notice's raw XML (gzip). Idempotent overwrite.
        Returns True on success, False on any failure (already logged)."""
        if not identifier or not xml_bytes:
            return False
        key = _safe_key(identifier)
        try:
            blob = gzip.compress(xml_bytes)
            self._client.put_object(
                self._bucket, key, io.BytesIO(blob), length=len(blob),
                content_type="application/gzip",
            )
            return True
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            logger.exception("TED raw store: put failed for %s", identifier)
            return False

    def get(self, identifier: str) -> bytes | None:
        """Fetch + decompress one notice's raw XML, or None if absent."""
        key = _safe_key(identifier)
        resp = None
        try:
            resp = self._client.get_object(self._bucket, key)
            return gzip.decompress(resp.read())
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            return None
        finally:
            if resp is not None:
                resp.close()
                resp.release_conn()

    def exists(self, identifier: str) -> bool:
        key = _safe_key(identifier)
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            return False


_DEFAULT_PACKAGE_BUCKET = "ted-packages"


class TedPackageStore:
    """Durable cache for TED monthly packages (the >1 GB tar.gz bundles).

    The archive loader downloads one package per calendar month. Caching
    each in object storage means a later re-parse (a new field, a bug
    fix) streams the cached bundle from in-cluster minio instead of
    re-downloading gigabytes from TED's CDN. Package granularity is the
    natural "store everything" unit for the bulk path — the whole
    month's XML is in the bundle.

    File-based (fput/fget) so multi-GB bundles never sit in memory.
    Config mirrors TedRawStore but with its own bucket:
      * ``TED_RAW_ENDPOINT`` / ``TED_RAW_ACCESS_KEY`` / ``TED_RAW_SECRET_KEY``
      * ``TED_PACKAGE_BUCKET`` (default ``ted-packages``)
    """

    def __init__(self, client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> "TedPackageStore | None":
        endpoint = os.environ.get("TED_RAW_ENDPOINT")
        access = os.environ.get("TED_RAW_ACCESS_KEY")
        secret = os.environ.get("TED_RAW_SECRET_KEY")
        if not (endpoint and access and secret):
            logger.info("TED package store not configured (TED_RAW_* unset)")
            return None
        try:
            from minio import Minio  # pylint: disable=import-outside-toplevel
        except ImportError:
            logger.warning("minio package unavailable; packages not cached")
            return None
        bucket = os.environ.get("TED_PACKAGE_BUCKET", _DEFAULT_PACKAGE_BUCKET)
        secure = os.environ.get("TED_RAW_SECURE", "").lower() in ("1", "true", "yes")
        client = Minio(endpoint, access_key=access, secret_key=secret, secure=secure)
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            logger.exception("TED package store: bucket check/create failed; disabling")
            return None
        logger.info("TED package store ready: %s/%s", endpoint, bucket)
        return cls(client, bucket)

    @staticmethod
    def _key(year: int, month: int) -> str:
        return f"ted-{year}-{month:02d}.tar.gz"

    def has(self, year: int, month: int) -> bool:
        try:
            self._client.stat_object(self._bucket, self._key(year, month))
            return True
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            return False

    def fetch_to(self, year: int, month: int, dest: "Path") -> bool:
        """Download the cached package to ``dest``. True on success."""
        try:
            self._client.fget_object(self._bucket, self._key(year, month), str(dest))
            return True
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            logger.exception("TED package store: fetch failed for %d-%02d",
                             year, month)
            return False

    def save(self, year: int, month: int, path: "Path") -> bool:
        """Upload a downloaded package for future re-parses. True on success."""
        try:
            self._client.fput_object(
                self._bucket, self._key(year, month), str(path),
                content_type="application/gzip",
            )
            return True
        except Exception:  # noqa: BLE001  pylint: disable=broad-except
            logger.exception("TED package store: save failed for %d-%02d",
                             year, month)
            return False
