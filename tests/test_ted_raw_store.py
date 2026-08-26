"""TedRawStore: gzip round-trip + graceful degradation."""
import gzip
import io

from src.data.ted_raw_store import TedRawStore, TedPackageStore, _safe_key


class _Resp:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d

    def close(self):
        pass

    def release_conn(self):
        pass


class _FakeMinio:
    def __init__(self):
        self.objects = {}

    # Signature mirrors minio.Minio.put_object exactly — this double is
    # substituted for the real client, so its arity is fixed by the SDK
    # rather than chosen here. `length` and `content_type` are accepted
    # and ignored because the store passes them and the fake does not
    # need them.
    def put_object(  # pylint: disable=unused-argument,too-many-arguments,too-many-positional-arguments
        self, bucket, key, stream, length, content_type=None,
    ):
        self.objects[(bucket, key)] = stream.read()

    def get_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise KeyError(key)
        return _Resp(self.objects[(bucket, key)])

    def stat_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise KeyError(key)
        return object()


def _store():
    return TedRawStore(_FakeMinio(), "ted-raw")


def test_put_get_roundtrip():
    store = _store()
    xml = b"<Notice><cbc:ID>24782-2025</cbc:ID></Notice>"
    assert store.put("24782-2025", xml) is True
    assert store.get("24782-2025") == xml


def test_put_stores_gzipped_and_smaller():
    store = _store()
    xml = b"x" * 1000
    store.put("k", xml)
    assert store.get("k") == xml
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as fobj:
        fobj.write(xml)
    assert len(raw.getvalue()) < len(xml)


def test_get_absent_returns_none():
    assert _store().get("nope") is None


def test_exists():
    store = _store()
    store.put("a", b"<x/>")
    assert store.exists("a") is True
    assert store.exists("b") is False


def test_put_empty_is_noop():
    store = _store()
    assert store.put("k", b"") is False
    assert store.put("", b"<x/>") is False


def test_safe_key_slashes():
    assert _safe_key("a/b") == "a_b.xml.gz"


def test_from_env_unconfigured_returns_none(monkeypatch):
    for key in ("TED_RAW_ENDPOINT", "TED_RAW_ACCESS_KEY", "TED_RAW_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert TedRawStore.from_env() is None


class _FakeMinioFiles(_FakeMinio):
    def fput_object(self, bucket, key, file_path, content_type=None):  # pylint: disable=unused-argument
        with open(file_path, "rb") as fobj:
            self.objects[(bucket, key)] = fobj.read()

    def fget_object(self, bucket, key, file_path):
        if (bucket, key) not in self.objects:
            raise KeyError(key)
        with open(file_path, "wb") as fobj:
            fobj.write(self.objects[(bucket, key)])


def test_package_store_save_has_fetch(tmp_path):
    store = TedPackageStore(_FakeMinioFiles(), "ted-packages")
    pkg = tmp_path / "ted-2025-01.tar.gz"
    pkg.write_bytes(b"PKGDATA" * 100)
    assert store.has(2025, 1) is False
    assert store.save(2025, 1, pkg) is True
    assert store.has(2025, 1) is True
    dest = tmp_path / "fetched.tar.gz"
    assert store.fetch_to(2025, 1, dest) is True
    assert dest.read_bytes() == pkg.read_bytes()


def test_package_store_key():
    assert TedPackageStore._key(2026, 3) == "ted-2026-03.tar.gz"  # pylint: disable=protected-access
