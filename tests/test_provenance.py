import hashlib
import json

import pytest

from src.api.provenance import content_sha256, data_passport, signed_manifest


def test_passport_hash_and_defaults_are_explicit():
    record = data_passport(source_url="https://example.com", content="hello",
                           transformations=["cleaned"])
    assert record["content_sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert record["license"] == "unknown"
    assert record["robots_verdict"] == "unknown"
    assert record["transformations"] == ["cleaned"]


def test_confidence_validation():
    with pytest.raises(ValueError):
        data_passport(source_url="https://example.com", content=b"x", confidence=1.1)


def test_signed_manifest_is_verifiable():
    manifest = signed_manifest([{"content_sha256": content_sha256("x")}],
                              dataset_id="demo", signing_key="secret")
    signature = manifest.pop("signature")["value"]
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hmac
    expected = hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
