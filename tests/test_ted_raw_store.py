"""TedRawStore: gzip round-trip + graceful degradation."""
import gzip
from src.data.ted_raw_store import TedRawStore, _safe_key


class _FakeMinio:
    def __init__(self):
        self.objects = {}

    def put_object(self, bucket, key, stream, length, content_type=None):
        self.objects[(bucket, key)] = stream.read()

    def get_object(self, bucket, key):
        import io
        if (bucket, key) not in self.objects:
            raise KeyError(key)
        return _Resp(self.objects[(bucket, key)])

    def stat_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise KeyError(key)
        return object()


class _Resp:
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def close(self): pass
    def release_conn(self): pass


def _store():
    return TedRawStore(_FakeMinio(), "ted-raw")


def test_put_get_roundtrip():
    s = _store()
    xml = b"<Notice><cbc:ID>24782-2025</cbc:ID></Notice>"
    assert s.put("24782-2025", xml) is True
    assert s.get("24782-2025") == xml


def test_put_stores_gzipped():
    s = _store()
    xml = b"x" * 1000
    s.put("k", xml)
    stored = s._client.objects[("ted-raw", "k.xml.gz")]
    assert gzip.decompress(stored) == xml
    assert len(stored) < len(xml)  # compressed


def test_get_absent_returns_none():
    assert _store().get("nope") is None


def test_exists():
    s = _store()
    s.put("a", b"<x/>")
    assert s.exists("a") is True
    assert s.exists("b") is False


def test_put_empty_is_noop():
    s = _store()
    assert s.put("k", b"") is False
    assert s.put("", b"<x/>") is False


def test_safe_key_slashes():
    assert _safe_key("a/b") == "a_b.xml.gz"


def test_from_env_unconfigured_returns_none(monkeypatch):
    for k in ("TED_RAW_ENDPOINT", "TED_RAW_ACCESS_KEY", "TED_RAW_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert TedRawStore.from_env() is None
