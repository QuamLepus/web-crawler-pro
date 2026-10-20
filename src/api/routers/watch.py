"""Scheduled URL watches with crawl-backed snapshots and tenant-scoped diffs."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.api.routers.jobs import CtxDep
from src.intelligence.watch import (DUE, TENANT_WATCHES, WATCH, create_watch,
                                    get_watch, list_events)

router = APIRouter(prefix="/watch", tags=["web monitoring"])


class WatchCreate(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    interval_minutes: int = Field(default=60, ge=15, le=10080)

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("url must be an absolute HTTP(S) URL without credentials")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError("local and private hostnames are not allowed")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError("non-public IP addresses are not allowed")
        return value


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


@router.post("", status_code=201, summary="Create a scheduled page watch")
async def create(body: WatchCreate, ctx: CtxDep, request: Request) -> dict:
    tenant = str(ctx["tenant"])
    watch_id = "watch_" + uuid.uuid4().hex[:20]
    return create_watch(request.app.state.services.redis, tenant=tenant,
                        watch_id=watch_id, url=body.url,
                        interval_minutes=body.interval_minutes)


@router.get("", summary="List tenant page watches")
async def list_watches(ctx: CtxDep, request: Request) -> dict:
    redis = request.app.state.services.redis
    tenant = str(ctx["tenant"])
    ids = redis.smembers(TENANT_WATCHES.format(tenant=tenant))
    items = []
    for raw_id in ids:
        watch_id = _text(raw_id)
        watch = get_watch(redis, watch_id, tenant)
        if watch:
            watch.pop("last_text", None)
            items.append(watch)
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {"items": items, "total": len(items)}


@router.get("/{watch_id}/changes", summary="List detected changes for a watch")
async def changes(watch_id: str, ctx: CtxDep, request: Request, limit: int = 50) -> dict:
    if not re.fullmatch(r"watch_[a-f0-9]{20}", watch_id):
        raise HTTPException(404, "watch not found")
    redis = request.app.state.services.redis
    watch = get_watch(redis, watch_id, str(ctx["tenant"]))
    if not watch:
        raise HTTPException(404, "watch not found")
    return {"watch_id": watch_id, "items": list_events(redis, watch_id,
                                                       limit=max(1, min(limit, 100))),
            "last_checked_at": watch.get("last_checked_at")}


@router.post("/{watch_id}/run", status_code=202, summary="Run a watch now")
async def run_now(watch_id: str, ctx: CtxDep, request: Request) -> dict:
    if not re.fullmatch(r"watch_[a-f0-9]{20}", watch_id):
        raise HTTPException(404, "watch not found")
    redis = request.app.state.services.redis
    tenant = str(ctx["tenant"])
    watch = get_watch(redis, watch_id, tenant)
    if not watch or not watch.get("active"):
        raise HTTPException(404, "watch not found")
    host = urlsplit(watch["url"]).netloc.lower()
    lock_key = f"wcp:watch:hostlock:{hashlib.sha256(host.encode()).hexdigest()}"
    if not redis.set(lock_key, watch_id, nx=True, ex=60):
        raise HTTPException(429, "a watch crawl for this host was queued recently")
    from src.compute.scheduler import JobPriority, JobSpec
    job_id = "watchrun_" + uuid.uuid4().hex[:20]
    spec = JobSpec(kind="crawl_job", tenant=tenant, job_id=job_id,
                   priority=JobPriority.NORMAL,
                   payload={"url": watch["url"], "max_pages": 1,
                            "obey_robots": True, "stealth": False,
                            "tenant": tenant, "job_id": "",
                            "watch_id": watch_id, "options": {"watch_id": watch_id}})
    try:
        request.app.state.services.scheduler.submit(spec)
    except Exception as exc:
        redis.delete(lock_key)
        raise HTTPException(503, "watch crawl could not be scheduled") from exc
    return {"watch_id": watch_id, "job_id": job_id, "status": "queued"}


@router.delete("/{watch_id}", status_code=204, summary="Stop a page watch")
async def delete_watch(watch_id: str, ctx: CtxDep, request: Request) -> None:
    if not re.fullmatch(r"watch_[a-f0-9]{20}", watch_id):
        raise HTTPException(404, "watch not found")
    redis = request.app.state.services.redis
    tenant = str(ctx["tenant"])
    watch = get_watch(redis, watch_id, tenant)
    if not watch:
        raise HTTPException(404, "watch not found")
    watch["active"] = False
    redis.set(WATCH.format(watch_id=watch_id), json.dumps(watch, separators=(",", ":")))
    redis.zrem(DUE, watch_id)
    return None
