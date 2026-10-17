"""Tenant-scoped saved Data Studio queries."""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.api.routers.jobs import CtxDep

router = APIRouter(prefix="/studio", tags=["data-studio"])
KEY = "wcp:v1:studio:saved:{saved_id}"
TENANT_INDEX = "wcp:v1:studio:saved:tenant:{tenant}"
TTL = 365 * 24 * 60 * 60


class SavedQueryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    q: str = Field(min_length=1, max_length=2048)
    limit: int = Field(default=25, ge=1, le=100)
    sources: list[str] = Field(default_factory=list, max_length=10)
    group_by: Literal["host", "source"] = "host"
    metric: Literal["count", "avg_score"] = "count"

    @field_validator("name", "q")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("sources")
    @classmethod
    def normalize_sources(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(" ".join(v.split()).lower() for v in values if v.strip()))
        if any(len(value) > 80 for value in normalized):
            raise ValueError("source filter must be at most 80 characters")
        return normalized


def _decode(raw) -> dict:
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def _load(request: Request, saved_id: str, tenant: str) -> dict:
    if not re.fullmatch(r"saved_[a-f0-9]{20}", saved_id):
        raise HTTPException(404, "saved query not found")
    raw = request.app.state.services.redis.get(KEY.format(saved_id=saved_id))
    if not raw:
        raise HTTPException(404, "saved query not found")
    record = _decode(raw)
    if record.get("tenant") != tenant:
        raise HTTPException(404, "saved query not found")
    return record


@router.post("/saved-queries", status_code=201, summary="Save a Data Studio query")
async def save_query(body: SavedQueryCreate, request: Request, ctx: CtxDep) -> dict:
    tenant = str(ctx["tenant"])
    saved_id = "saved_" + uuid.uuid4().hex[:20]
    record = {"saved_id": saved_id, "tenant": tenant, "name": body.name,
              "q": body.q, "limit": body.limit, "sources": body.sources,
              "group_by": body.group_by, "metric": body.metric,
              "created_at": datetime.now(timezone.utc).isoformat()}
    redis = request.app.state.services.redis
    pipe = redis.pipeline()
    pipe.set(KEY.format(saved_id=saved_id), json.dumps(record, separators=(",", ":")), ex=TTL)
    pipe.zadd(TENANT_INDEX.format(tenant=tenant), {saved_id: time.time()})
    pipe.execute()
    return record


@router.get("/saved-queries", summary="List saved Data Studio queries")
async def list_queries(request: Request, ctx: CtxDep,
                       page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict:
    tenant = str(ctx["tenant"])
    redis = request.app.state.services.redis
    index = TENANT_INDEX.format(tenant=tenant)
    ids = redis.zrevrange(index, (page - 1) * page_size, page * page_size - 1)
    items = []
    for raw_id in ids:
        saved_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        try:
            items.append(_load(request, saved_id, tenant))
        except HTTPException:
            continue
    return {"items": items, "total": int(redis.zcard(index) or 0),
            "page": page, "page_size": page_size}


@router.delete("/saved-queries/{saved_id}", status_code=204,
               summary="Delete a saved Data Studio query")
async def delete_query(saved_id: str, request: Request, ctx: CtxDep) -> None:
    tenant = str(ctx["tenant"])
    _load(request, saved_id, tenant)
    redis = request.app.state.services.redis
    redis.delete(KEY.format(saved_id=saved_id))
    redis.zrem(TENANT_INDEX.format(tenant=tenant), saved_id)
