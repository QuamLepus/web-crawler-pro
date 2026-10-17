"""Tenant-isolated provenance ledger endpoints.

This ledger stores caller-supplied metadata; it does not fetch URLs or infer
permission. Signing is disabled until an operator configures a secret key.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from src.api.routers.jobs import CtxDep
from src.api.provenance import signed_manifest

router = APIRouter(prefix="/provenance", tags=["provenance"])


class ProvenanceRecord(BaseModel):
    record_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    source_url: str = Field(min_length=8, max_length=4096)
    fetched_at: str = Field(min_length=1, max_length=80)
    http_status: int | None = Field(default=None, ge=100, le=599)
    robots_verdict: str = Field(default="unknown", max_length=64)
    license: str = Field(default="unknown", max_length=128)
    content_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    extraction_method: str = Field(default="unknown", max_length=128)
    transformations: list[str] = Field(default_factory=list, max_length=64)
    extractor_version: str | None = Field(default=None, max_length=128)
    model_id: str | None = Field(default=None, max_length=256)
    prompt_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value


class ManifestRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _record_key(tenant: str, record_id: str) -> str:
    return f"wcp:provenance:{tenant}:{record_id}"


@router.post("/records", status_code=201, summary="Store a provenance passport")
async def put_record(body: ProvenanceRecord, ctx: CtxDep, request: Request) -> dict[str, Any]:
    redis = request.app.state.services.redis
    tenant = str(ctx["tenant"])
    key = _record_key(tenant, body.record_id)
    record = body.model_dump(mode="json")
    record["tenant"] = tenant
    redis.set(key, json.dumps(record, separators=(",", ":")))
    redis.zadd(f"wcp:provenance:records:{tenant}", {body.record_id: time.time()})
    return {"record_id": body.record_id, "stored": True}


@router.get("/records/{record_id}", summary="Retrieve a provenance passport")
async def get_record(record_id: str, ctx: CtxDep, request: Request) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", record_id):
        raise HTTPException(404, "record not found")
    raw = request.app.state.services.redis.get(_record_key(str(ctx["tenant"]), record_id))
    if raw is None:
        raise HTTPException(404, "record not found")
    record = json.loads(_text(raw))
    record.pop("tenant", None)
    return record


@router.post("/manifest", summary="Export a signed manifest for this tenant's records")
async def export_manifest(body: ManifestRequest, ctx: CtxDep, request: Request,
                          limit: int = Query(default=1000, ge=1, le=10000)) -> dict[str, Any]:
    key = os.getenv("WCP_PROVENANCE_SIGNING_KEY", "")
    if not key:
        raise HTTPException(503, "manifest signing is not configured")
    redis = request.app.state.services.redis
    tenant = str(ctx["tenant"])
    ids = redis.zrevrange(f"wcp:provenance:records:{tenant}", 0, limit - 1)
    records = []
    for raw_id in ids:
        record_id = _text(raw_id)
        raw_record = redis.get(_record_key(tenant, record_id))
        if raw_record:
            record = json.loads(_text(raw_record))
            record.pop("tenant", None)
            records.append(record)
    return signed_manifest(records, dataset_id=body.dataset_id, signing_key=key)
