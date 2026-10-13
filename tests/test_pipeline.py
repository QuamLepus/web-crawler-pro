"""Pipeline tests: cleaning, dedupe, exporters, language/PII fallbacks."""

import fakeredis
import pytest

import redis as real_redis


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    server = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(real_redis, "from_url", lambda url, **kw: server)
    yield server


def test_normalize_item():
    from src.pipeline.cleaning import normalize_item

    item = {"url": "https://X.com/a/", "title": " Hello   world ",
            "text": "foo\tbar", "status_code": "200"}
    out = normalize_item(item)
    assert out["url"] == "https://x.com/a"
    assert out["title"] == "Hello world"
    assert out["status_code"] == 200
    assert "fetched_at" in out


def test_minhash_exact_dedupe_via_redis():
    from src.pipeline.dedupe import MinHashDeduper

    text = "the quick brown fox jumps over the lazy dog " * 10
    a = {"text": text}
    b = {"text": text}
    assert MinHashDeduper.is_duplicate(a) is False   # first sighting
    assert MinHashDeduper.is_duplicate(b) is True    # same signature


def test_simhash_near_duplicate():
    from src.pipeline.dedupe import SimHashDeduper

    d = SimHashDeduper(max_distance=3)
    base = "web crawling data platform for enterprise scale ingestion pipelines " * 20
    assert d.is_duplicate(base) is False
    # small perturbation -> same fingerprint (near-duplicate)
    assert d.is_duplicate(base.replace("enterprise", "ENTERPRISE")) is True
    # totally different content -> not a duplicate
    assert d.is_duplicate(
        "completely different content about cooking recipes and gourmet food " * 20
    ) is False


def test_pii_redact_fallback():
    from src.pipeline.language_pii import PIIRedactor

    r = PIIRedactor()
    out = r.redact("Contact john@example.com or call +1 555-123-4567 today")
    assert "john@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_language_detect_fallback():
    from src.pipeline.language_pii import detect_language

    assert detect_language("hello world this is an english sentence and more")[0] == "en"
    assert detect_language("আমি বাংলায় কথা বলি")[0] == "bn"


def test_json_csv_exporters(tmp_path):
    from src.pipeline.storage.csv_exporter import CSVExporter
    from src.pipeline.storage.json_exporter import JSONExporter

    items = [{"url": "https://a.com", "title": "A"},
             {"url": "https://b.com", "title": "B"}]
    jp = JSONExporter(str(tmp_path)).save(items[0])
    assert open(jp).read().strip().startswith("{")
    cp = CSVExporter(str(tmp_path)).save(items)
    lines = open(cp, encoding="utf-8-sig").read().splitlines()
    assert lines[0].startswith("url,title") and len(lines) == 3
