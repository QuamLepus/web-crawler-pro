"""Tests for the Trust & Provenance Engine (``src/provenance``).

The point of this layer is that it *proves* things, so these tests are about
proof: the chain breaks when a record is edited, a manifest signature fails
when a field moves, inclusion proofs verify against the Merkle root, and a
snapshot replays the exact bytes that produced a record.

If a test here can pass while the guarantee is broken, the feature is
decoration.
"""

from __future__ import annotations

import json
import time

import fakeredis
import pytest

from src.provenance import (
    ChainStep,
    ProvenanceLedger,
    ProvenanceRecord,
    SnapshotStore,
    build_manifest,
    content_hash,
    inclusion_proof,
    manifest_markdown,
    merkle_root,
    record_for_page,
    signed_manifest,
    verify_inclusion,
    verify_manifest,
)
from src.provenance.ledger import _url_id  # noqa: PLC2701 - internal, but tested


@pytest.fixture()
def ledger():
    return ProvenanceLedger(fakeredis.FakeStrictRedis(decode_responses=True))


@pytest.fixture()
def store(tmp_path):
    return SnapshotStore(tmp_path / "snapshots")


def _record(url="https://example.com/a", body="hello world", **kwargs):
    return record_for_page(tenant="t1", url=url, body=body, status=200,
                           license="cc-by", robots_verdict="allowed", **kwargs)


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def test_records_are_chained_and_verify(ledger):
    first = ledger.record(_record(url="https://a.test/1"))
    second = ledger.record(_record(url="https://a.test/2"))

    assert first.seq == 1
    assert first.prev_hash == ""          # genesis
    assert second.prev_hash == first.record_hash
    assert second.seq == 2

    result = ledger.verify("t1")
    assert result["ok"] is True
    assert result["records"] == 2
    assert result["head"] == second.record_hash


def test_editing_a_record_breaks_verification(ledger):
    first = ledger.record(_record(url="https://a.test/1"))
    ledger.record(_record(url="https://a.test/2"))

    raw = ledger.r.get(ledger._k("t1", "rec", first.record_id))
    payload = json.loads(raw)
    payload["license"] = "proprietary"          # a quiet licence rewrite
    ledger.r.set(ledger._k("t1", "rec", first.record_id), json.dumps(payload))

    result = ledger.verify("t1")
    assert result["ok"] is False
    assert result["problem_count"] >= 1
    assert result["problems"][0]["reason"] in ("hash_mismatch", "chain_break")


def test_tenants_are_isolated(ledger):
    ledger.record(record_for_page(tenant="tenant-a", url="https://a.test/", body="a"))
    assert ledger.recent("tenant-b", limit=10) == []
    assert ledger.for_dataset("tenant-b", "whatever") == []
    assert ledger.stats("tenant-b").records == 0


def test_lookup_by_url_and_dataset(ledger):
    ledger.record(_record(url="https://shop.test/p/1", dataset="prices",
                          dataset_version="v1"))
    ledger.record(_record(url="https://shop.test/p/1", dataset="prices",
                          dataset_version="v2"))
    assert len(ledger.for_url("t1", "https://shop.test/p/1")) == 2
    assert len(ledger.for_dataset("t1", "prices")) == 2
    assert len(ledger.for_dataset("t1", "prices", version="v2")) == 1
    stats = ledger.stats("t1")
    assert stats.records == 2
    assert stats.urls == 1
    assert stats.datasets == 1
    assert stats.redistributable == 2


def test_origin_of_content_groups_identical_bytes(ledger):
    body = "<html>same</html>"
    ledger.record(_record(url="https://a.test/x", body=body))
    ledger.record(_record(url="https://b.test/y", body=body))
    digest = content_hash(body)
    assert len(ledger.origin_of_content("t1", digest)) == 2


def test_vocabulary_is_normalised():
    record = ProvenanceRecord(source_url="https://x.test", license="weird",
                             robots_verdict="nonsense", collection_method="telepathy")
    assert record.license == "unknown"
    assert record.robots_verdict == "unknown"
    assert record.collection_method == "http"
    assert record.redistributable is False


def test_stale_and_confidence_bounds():
    record = ProvenanceRecord(source_url="https://x.test", fetched_at=time.time(),
                              confidence=5.0)
    assert record.confidence == 1.0
    assert record.stale is False
    record.fetched_at = time.time() - 200 * 86400
    assert record.stale is True


# --------------------------------------------------------------------------- #
# manifests
# --------------------------------------------------------------------------- #
def test_merkle_root_and_inclusion_proof_round_trip():
    leaves = [f"sha256:{i:064x}" for i in range(7)]
    root = merkle_root(leaves)
    for index in (0, 3, 6):
        proof = inclusion_proof(leaves, index)
        assert verify_inclusion(leaves[index], proof, root) is True
    # A leaf that is not in the tree must fail.
    assert verify_inclusion("sha256:" + "f" * 64,
                            inclusion_proof(leaves, 0), root) is False


def test_manifest_signature_detects_any_edit():
    records = [record_for_page(tenant="t1", url="https://a.test/1", body="a",
                               license="cc-by"),
               record_for_page(tenant="t1", url="https://b.test/2", body="b",
                               license="unknown")]
    for index, record in enumerate(records, start=1):
        record.seq = index
        record.record_hash = f"sha256:{index:064x}"

    manifest = signed_manifest(
        build_manifest(dataset="prices", version="v1", records=records,
                       tenant="t1"), "secret")
    ok, reason = verify_manifest(manifest, "secret")
    assert ok, reason
    assert manifest["licenses"]["counts"] == {"cc-by": 1, "unknown": 1}
    assert manifest["licenses"]["redistributable_records"] == 1
    assert manifest["source_count"] == 2

    tampered = json.loads(json.dumps(manifest))
    tampered["licenses"]["unknown_records"] = 0
    assert verify_manifest(tampered, "secret")[0] is False
    assert verify_manifest(manifest, "other-secret")[0] is False
    assert verify_manifest({"dataset": "x"}, "secret")[0] is False


def test_manifest_markdown_is_readable():
    records = [record_for_page(tenant="t1", url="https://a.test/1", body="a",
                               license="cc-by")]
    records[0].record_hash = "sha256:" + "a" * 64
    manifest = signed_manifest(build_manifest(dataset="d", version="v2",
                                              records=records), "s")
    text = manifest_markdown(manifest)
    assert "Data passport" in text and "Redistributable" in text
    assert manifest["merkle_root"] in text


# --------------------------------------------------------------------------- #
# snapshots / time machine
# --------------------------------------------------------------------------- #
def test_snapshot_store_is_content_addressed(store):
    first = store.put("<html>one</html>", url="https://a.test", status=200)
    second = store.put("<html>one</html>", url="https://b.test", status=200)
    assert first.snapshot_id == second.snapshot_id      # same bytes, same file
    assert store.get(first.snapshot_id) == b"<html>one</html>"
    assert store.meta(first.snapshot_id)["url_hint"] == "https://a.test"
    assert store.stats()["snapshots"] == 1


def test_replay_returns_the_inputs_of_a_record(store):
    body = "<html><title>T</title><p>price $19</p></html>"
    snap = store.put(body, url="https://shop.test/p", status=200,
                     content_type="text/html")
    record = record_for_page(
        tenant="t1", url="https://shop.test/p", body=body, status=200,
        snapshot_id=snap.snapshot_id, extractor="commerce-product",
        extractor_version="2", llm_model_id="test-model", prompt_hash="abc123",
        chain=[ChainStep(step="fetched", tool="crawler"),
               ChainStep(step="extracted", tool="recipe", version="2")])
    replay = store.replay(record)
    assert replay["available"] is True
    assert replay["content"] == body
    assert replay["extractor"] == "commerce-product"
    assert replay["llm_model_id"] == "test-model"
    assert [s["step"] for s in replay["chain"]] == ["fetched", "extracted"]


def test_large_snapshots_are_skipped_but_hashed(store):
    store.max_bytes = 32
    snap = store.put("x" * 100, url="https://big.test", status=200)
    assert snap.size == 100
    assert store.has(snap.snapshot_id) is False
