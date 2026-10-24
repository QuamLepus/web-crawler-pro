"""Tenant-partitioned, bounded hash-linked storage for runtime audit metadata.

This ledger detects accidental/partial record changes within its retained
window. Redis administrators can rewrite both records and heads; it is not an
immutable or externally anchored audit archive.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from redis.exceptions import WatchError

from src.agent_marketplace.runtime_contract import RuntimeAuditEvent


class AuditLedgerError(ValueError):
    """Rejected audit append or invalid ledger contents."""


@dataclass(frozen=True)
class AuditRecord:
    sequence: int
    previous_hash: str
    event_hash: str
    event: RuntimeAuditEvent

    def to_json(self) -> str:
        return json.dumps({
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
            "event": self.event.model_dump(mode="json"),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_event(event: RuntimeAuditEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def _record_hash(sequence: int, previous_hash: str,
                 event_data: dict[str, Any]) -> str:
    payload = json.dumps({
        "sequence": sequence,
        "previous_hash": previous_hash,
        "event": event_data,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
       allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class RuntimeAuditLedger:
    """Append bounded audit metadata per tenant with optimistic concurrency."""

    KEY_PREFIX = "wcp:v1:agent_runtime:audit"
    ZERO_HASH = "0" * 64

    def __init__(self, redis_client, *, max_events_per_tenant: int = 5000) -> None:
        if max_events_per_tenant < 1:
            raise ValueError("max_events_per_tenant must be positive")
        self.r = redis_client
        self.max_events = max_events_per_tenant

    @classmethod
    def _keys(cls, tenant_id: str) -> tuple[str, str]:
        # The tenant key is a digest, so raw tenant identifiers never enter Redis keys.
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        return (f"{cls.KEY_PREFIX}:{tenant_hash}:records",
                f"{cls.KEY_PREFIX}:{tenant_hash}:head")

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @classmethod
    def _text_mapping(cls, value: dict[Any, Any]) -> dict[str, str]:
        return {cls._text(key): cls._text(item) for key, item in value.items()}

    def append(self, tenant_id: str, event: RuntimeAuditEvent) -> AuditRecord:
        tenant_id = str(tenant_id)
        if not tenant_id or event.tenant_id != tenant_id:
            raise AuditLedgerError("event tenant must match ledger tenant")
        records_key, head_key = self._keys(tenant_id)
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(head_key)
                    head = self._text_mapping(pipe.hgetall(head_key))
                    sequence = int(head.get("sequence", 0)) + 1
                    previous_hash = head.get("event_hash", self.ZERO_HASH)
                    event_data = _canonical_event(event)
                    record = AuditRecord(
                        sequence=sequence,
                        previous_hash=previous_hash,
                        event_hash=_record_hash(sequence, previous_hash, event_data),
                        event=event,
                    )
                    pipe.multi()
                    pipe.rpush(records_key, record.to_json())
                    pipe.ltrim(records_key, -self.max_events, -1)
                    pipe.hset(head_key, mapping={
                        "sequence": sequence, "event_hash": record.event_hash})
                    pipe.execute()
                    return record
                except WatchError:
                    continue

    def _decode_records(self, raw_records: list[Any]) -> list[AuditRecord]:
        records = []
        for raw in raw_records:
            try:
                value = json.loads(self._text(raw))
                records.append(AuditRecord(
                    sequence=int(value["sequence"]),
                    previous_hash=value["previous_hash"],
                    event_hash=value["event_hash"],
                    event=RuntimeAuditEvent.model_validate(value["event"]),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise AuditLedgerError("invalid audit record encoding") from exc
        return records

    def read(self, tenant_id: str, *, limit: int | None = None) -> list[AuditRecord]:
        limit = min(100, self.max_events) if limit is None else limit
        if not 1 <= limit <= self.max_events:
            raise ValueError("limit must be between 1 and max_events_per_tenant")
        records_key, _ = self._keys(str(tenant_id))
        return self._decode_records(self.r.lrange(records_key, -limit, -1))

    def verify(self, tenant_id: str) -> bool:
        """Verify the retained chain and its head; an empty ledger is valid."""
        records_key, head_key = self._keys(str(tenant_id))
        try:
            with self.r.pipeline(transaction=True) as pipe:
                pipe.lrange(records_key, -self.max_events, -1)
                pipe.hgetall(head_key)
                raw_records, raw_head = pipe.execute()
            records = self._decode_records(raw_records)
        except AuditLedgerError:
            return False
        head = self._text_mapping(raw_head)
        if not records:
            return not head
        previous = records[0].previous_hash
        prior_sequence = records[0].sequence - 1
        for record in records:
            event_data = _canonical_event(record.event)
            if (record.sequence != prior_sequence + 1
                    or record.event.tenant_id != str(tenant_id)
                    or record.previous_hash != previous
                    or record.event_hash != _record_hash(
                        record.sequence, record.previous_hash, event_data)):
                return False
            previous = record.event_hash
            prior_sequence = record.sequence
        try:
            return (int(head.get("sequence", -1)) == records[-1].sequence
                    and self._text(head.get("event_hash", "")) == records[-1].event_hash)
        except (TypeError, ValueError):
            return False
