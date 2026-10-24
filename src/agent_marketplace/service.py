"""Redis-backed tenant-scoped catalog for declarative agent manifests.

This service stores and discovers metadata only. It deliberately has no code
upload, purchase, or execution path.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from redis.exceptions import WatchError


class AgentMarketplaceError(ValueError):
    """Invalid catalog operation or owner-only update failure."""


@dataclass
class AgentCard:
    agent_id: str
    owner_tenant: str
    manifest: dict[str, Any]
    tags: list[str]
    license: str
    status: str
    created_at: str
    updated_at: str


class AgentMarketplaceService:
    PUBLIC_INDEX: ClassVar[str] = "wcp:v1:agent_marketplace:public"
    TENANT_INDEX: ClassVar[str] = "wcp:v1:agent_marketplace:tenant:{tenant}"
    CARD_KEY: ClassVar[str] = "wcp:v1:agent_marketplace:card:{agent_id}"
    STATUSES: ClassVar[set[str]] = {"draft", "published", "suspended", "archived"}
    MAX_MANIFEST_BYTES: ClassVar[int] = 16 * 1024

    def __init__(self, redis_client, *, clock=None) -> None:
        self.r = redis_client
        self._clock = clock or time.time

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def _load(self, agent_id: str) -> AgentCard | None:
        raw = self.r.hget(self.CARD_KEY.format(agent_id=agent_id), "data")
        if not raw:
            return None
        return AgentCard(**json.loads(self._text(raw)))

    def _load_many(self, raw_ids: list[Any]) -> list[AgentCard]:
        ids = [self._text(raw_id) for raw_id in raw_ids]
        if not ids:
            return []
        pipe = self.r.pipeline(transaction=False)
        for agent_id in ids:
            pipe.hget(self.CARD_KEY.format(agent_id=agent_id), "data")
        records = pipe.execute()
        return [AgentCard(**json.loads(self._text(raw)))
                for raw in records if raw]

    @classmethod
    def _validate_manifest(cls, manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise AgentMarketplaceError("manifest must be JSON-serializable") from exc
        if len(encoded.encode("utf-8")) > cls.MAX_MANIFEST_BYTES:
            raise AgentMarketplaceError("manifest exceeds 16384 encoded bytes")
        return manifest

    @staticmethod
    def _normalize_tags(tags: list[str]) -> list[str]:
        normalized = []
        seen = set()
        for raw in tags:
            tag = raw.strip().lower()
            if tag and tag not in seen:
                seen.add(tag)
                normalized.append(tag)
        return normalized

    def create(self, owner: str, *, manifest: dict[str, Any], tags: list[str],
               license: str) -> AgentCard:
        self._validate_manifest(manifest)
        timestamp = datetime.now(UTC).isoformat()
        card = AgentCard(
            agent_id=f"agent_{uuid.uuid4().hex[:20]}", owner_tenant=str(owner),
            manifest=manifest, tags=self._normalize_tags(tags), license=license,
            status="draft", created_at=timestamp, updated_at=timestamp,
        )
        pipe = self.r.pipeline()
        pipe.hset(self.CARD_KEY.format(agent_id=card.agent_id),
                  mapping={"data": json.dumps(asdict(card), separators=(",", ":"))})
        pipe.zadd(self.TENANT_INDEX.format(tenant=owner), {card.agent_id: self._clock()})
        pipe.execute()
        return card

    def get_for_viewer(self, agent_id: str, viewer: str) -> AgentCard | None:
        card = self._load(agent_id)
        if card is None or (card.status != "published" and card.owner_tenant != str(viewer)):
            return None
        return card

    def update(self, agent_id: str, actor: str, *, changes: dict[str, Any]) -> AgentCard:
        allowed = {"manifest", "tags", "license", "status"}
        if not changes or set(changes) - allowed:
            raise AgentMarketplaceError("at least one editable catalog field is required")
        if "manifest" in changes:
            self._validate_manifest(changes["manifest"])
        if "tags" in changes:
            changes["tags"] = self._normalize_tags(changes["tags"])
        if "status" in changes and changes["status"] not in self.STATUSES:
            raise AgentMarketplaceError("invalid agent listing status")

        key = self.CARD_KEY.format(agent_id=agent_id)
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.hget(key, "data")
                    if not raw:
                        raise AgentMarketplaceError("not found")
                    card = AgentCard(**json.loads(self._text(raw)))
                    if card.owner_tenant != str(actor):
                        raise AgentMarketplaceError("not found")
                    for name, value in changes.items():
                        setattr(card, name, value)
                    card.updated_at = datetime.now(UTC).isoformat()
                    pipe.multi()
                    pipe.hset(key, mapping={
                        "data": json.dumps(asdict(card), separators=(",", ":"))})
                    pipe.zadd(self.TENANT_INDEX.format(tenant=card.owner_tenant),
                              {agent_id: self._clock()})
                    if card.status == "published":
                        pipe.zadd(self.PUBLIC_INDEX, {agent_id: self._clock()})
                    else:
                        pipe.zrem(self.PUBLIC_INDEX, agent_id)
                    pipe.execute()
                    return card
                except WatchError:
                    continue

    def list_owned(self, owner: str, *, status: str | None = None,
                   limit: int = 20, offset: int = 0) -> list[AgentCard]:
        ids = self.r.zrevrange(self.TENANT_INDEX.format(tenant=owner), 0, 9999)
        cards = self._load_many(ids)
        if status:
            cards = [card for card in cards if card.status == status]
        return cards[offset:offset + limit]

    def search(self, *, query: str = "", tag: str = "", limit: int = 20,
               offset: int = 0) -> list[AgentCard]:
        # Bound candidate reads even when the public catalog is large.
        ids = self.r.zrevrange(self.PUBLIC_INDEX, 0, 9999)
        needle, normalized_tag = query.strip().casefold(), tag.strip().casefold()
        found = []
        for card in self._load_many(ids):
            if card.status != "published":
                continue
            searchable = f"{card.manifest.get('name', '')} {card.manifest.get('summary', '')}".casefold()
            if needle and needle not in searchable:
                continue
            if normalized_tag and normalized_tag not in card.tags:
                continue
            found.append(card)
        return found[offset:offset + limit]
