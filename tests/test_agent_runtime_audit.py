"""Tenant isolation, retention, and integrity tests for runtime audit metadata."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise

import fakeredis
import pytest

from src.agent_marketplace.audit_ledger import AuditLedgerError, RuntimeAuditLedger
from src.agent_marketplace.runtime_contract import RuntimeAuditEvent, RuntimeBackend


def _event(tenant_id="tenant-a", suffix="1"):
    return RuntimeAuditEvent(
        event_id=f"event-{suffix}", occurred_at=datetime.now(UTC),
        event_type="admission_denied", run_id=f"run-{suffix}",
        tenant_id=tenant_id, agent_id="agent-a", version="1.0.0",
        backend=RuntimeBackend.WASM_WASI, artifact_sha256="a" * 64,
        policy_revision="policy-1", reason_code="artifact_not_approved")


def _ledger(max_events=100):
    return RuntimeAuditLedger(
        fakeredis.FakeStrictRedis(decode_responses=True),
        max_events_per_tenant=max_events)


def test_audit_ledger_is_tenant_partitioned_and_rejects_mismatched_tenant():
    ledger = _ledger()
    record = ledger.append("tenant-a", _event())
    ledger.append("tenant-b", _event(tenant_id="tenant-b"))

    assert record.sequence == 1
    assert ledger.verify("tenant-a")
    assert ledger.verify("tenant-b")
    assert [item.event.tenant_id for item in ledger.read("tenant-a")] == ["tenant-a"]
    with pytest.raises(AuditLedgerError, match="tenant must match"):
        ledger.append("tenant-a", _event(tenant_id="tenant-b", suffix="cross"))


def test_audit_ledger_keeps_bounded_window_and_verifies_chain_head():
    ledger = _ledger(max_events=2)
    for index in range(4):
        ledger.append("tenant-a", _event(suffix=str(index)))

    retained = ledger.read("tenant-a", limit=2)
    assert [record.sequence for record in retained] == [3, 4]
    assert retained[1].previous_hash == retained[0].event_hash
    assert ledger.verify("tenant-a")
    with pytest.raises(ValueError, match="limit"):
        ledger.read("tenant-a", limit=3)


def test_audit_ledger_detects_record_tampering_and_head_mismatch():
    redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
    ledger = RuntimeAuditLedger(redis_client)
    ledger.append("tenant-a", _event())
    records_key, head_key = ledger._keys("tenant-a")

    original = json.loads(redis_client.lindex(records_key, 0))
    original["event"]["reason_code"] = "changed"
    redis_client.lset(records_key, 0, json.dumps(original))
    assert not ledger.verify("tenant-a")

    redis_client.lset(records_key, 0, ledger.read("tenant-a")[0].to_json()
                      if ledger.verify("tenant-a") else "{}")
    assert not ledger.verify("tenant-a")
    redis_client.hset(head_key, "event_hash", "b" * 64)
    assert not ledger.verify("tenant-a")


def test_concurrent_appends_have_a_single_ordered_chain():
    ledger = _ledger()
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(
            lambda index: ledger.append("tenant-a", _event(suffix=str(index))),
            range(32)))

    assert sorted(record.sequence for record in records) == list(range(1, 33))
    assert ledger.verify("tenant-a")
    stored = ledger.read("tenant-a", limit=32)
    assert all(left.sequence + 1 == right.sequence
               and right.previous_hash == left.event_hash
               for left, right in pairwise(stored))
