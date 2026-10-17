"""Tenant-isolated lightweight entity graph stored in Redis.

Entity matching is intentionally conservative: Unicode-normalized, case-folded
exact matches across a caller-supplied name/alias set. It does not claim fuzzy
or AI entity resolution; conflicting aliases are rejected rather than merged.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep

router = APIRouter(prefix="/graph", tags=["knowledge graph"])


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _keys(tenant: str) -> tuple[str, str]:
    return f"wcp:graph:{tenant}:entities", f"wcp:graph:{tenant}:aliases"


def _entity_id(name: str) -> str:
    return "ent_" + hashlib.sha256(normalize_name(name).encode()).hexdigest()[:24]


class EntityInput(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(default="Entity", min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    properties: dict[str, Any] = Field(default_factory=dict)


class RelationInput(BaseModel):
    source_id: str = Field(pattern=r"^ent_[a-f0-9]{24}$")
    target_id: str = Field(pattern=r"^ent_[a-f0-9]{24}$")
    relation: str = Field(min_length=1, max_length=80)
    properties: dict[str, Any] = Field(default_factory=dict)


@router.get("", summary="List a bounded tenant knowledge graph")
async def list_graph(ctx: CtxDep, request: Request,
                     limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    entity_key, _ = _keys(tenant)
    ids = redis.zrevrange(f"{entity_key}:ids", 0, limit - 1)
    if not ids:  # compatibility for entities written before the ordered index
        ids = list(redis.scan_iter(match=f"{entity_key}:ent_*"))[:limit]
        ids = [raw.decode() if isinstance(raw, bytes) else raw for raw in ids]
        ids = [value.rsplit(":", 1)[-1] for value in ids]
    nodes = []
    edges_by_key = {}
    for raw_id in ids:
        entity_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        raw = redis.get(f"{entity_key}:{entity_id}")
        if not raw:
            continue
        record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        nodes.append(record)
        rel_key = f"wcp:graph:{tenant}:relations:{entity_id}"
        for raw_relation in redis.smembers(rel_key):
            relation = json.loads(raw_relation.decode() if isinstance(raw_relation, bytes) else raw_relation)
            edges_by_key[(relation["source_id"], relation["target_id"], relation["relation"])] = relation
    node_ids = {node["entity_id"] for node in nodes}
    edges = [edge for edge in edges_by_key.values()
             if edge["source_id"] in node_ids and edge["target_id"] in node_ids]
    return {"nodes": nodes, "edges": edges, "limit": limit,
            "truncated": redis.zcard(f"{entity_key}:ids") > limit}


@router.post("/entities", summary="Create or resolve a graph entity")
async def upsert_entity(body: EntityInput, ctx: CtxDep, request: Request) -> dict[str, Any]:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    entity_key, alias_key = _keys(tenant)
    names = [body.name, *body.aliases]
    normalized = {normalize_name(name) for name in names if name.strip()}
    if len(normalized) != len([name for name in names if name.strip()]):
        normalized = set(normalized)
    ids = {redis.hget(alias_key, name) for name in normalized}
    ids.discard(None)
    ids = {value.decode() if isinstance(value, bytes) else value for value in ids}
    if len(ids) > 1:
        raise HTTPException(409, "provided name/aliases already resolve to different entities")
    entity_id = next(iter(ids), _entity_id(body.name))
    record_key = f"{entity_key}:{entity_id}"
    prior_raw = redis.get(record_key)
    prior = json.loads(prior_raw.decode() if isinstance(prior_raw, bytes) else prior_raw) if prior_raw else {}
    record = {"entity_id": entity_id, "name": body.name,
              "entity_type": body.entity_type,
              "aliases": sorted(set(prior.get("aliases", [])) | set(body.aliases)),
              "properties": {**prior.get("properties", {}), **body.properties}}
    try:
        pipe = redis.pipeline()
        pipe.set(record_key, json.dumps(record, separators=(",", ":"), ensure_ascii=False))
        pipe.zadd(f"{entity_key}:ids", {entity_id: time.time()})
        for alias in normalized:
            existing = redis.hget(alias_key, alias)
            if existing and (existing.decode() if isinstance(existing, bytes) else existing) != entity_id:
                raise HTTPException(409, "alias already belongs to another entity")
            pipe.hset(alias_key, alias, entity_id)
        pipe.execute()
    except HTTPException:
        raise
    return record


@router.get("/entities/{entity_id}", summary="Get entity and its relationships")
async def get_entity(entity_id: str, ctx: CtxDep, request: Request) -> dict[str, Any]:
    if not re.fullmatch(r"ent_[a-f0-9]{24}", entity_id):
        raise HTTPException(404, "entity not found")
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    entity_key, _ = _keys(tenant)
    raw = redis.get(f"{entity_key}:{entity_id}")
    if not raw:
        raise HTTPException(404, "entity not found")
    record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    relation_key = f"wcp:graph:{tenant}:relations:{entity_id}"
    relations = []
    for item in redis.smembers(relation_key):
        item = item.decode() if isinstance(item, bytes) else item
        relations.append(json.loads(item))
    record["relations"] = relations
    return record


@router.post("/relations", status_code=201, summary="Connect two existing entities")
async def add_relation(body: RelationInput, ctx: CtxDep, request: Request) -> dict[str, Any]:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    entity_key, _ = _keys(tenant)
    for entity_id in (body.source_id, body.target_id):
        if not redis.exists(f"{entity_key}:{entity_id}"):
            raise HTTPException(404, "entity not found")
    relation = {"source_id": body.source_id, "target_id": body.target_id,
                "relation": body.relation, "properties": body.properties}
    serialized = json.dumps(relation, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    pipe = redis.pipeline()
    pipe.sadd(f"wcp:graph:{tenant}:relations:{body.source_id}", serialized)
    pipe.sadd(f"wcp:graph:{tenant}:relations:{body.target_id}", serialized)
    pipe.execute()
    return relation
