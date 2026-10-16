"""v1 router: Web Change Intelligence (monitors + typed change events).

    POST /v1/watch                     create a monitor (urls, cadence, kinds)
    GET  /v1/watch                     list monitors with their cadence
    GET  /v1/watch/{id}                one monitor
    PATCH /v1/watch/{id}               pause/resume, change cadence or URLs
    DELETE /v1/watch/{id}
    POST /v1/watch/{id}/run            run now and return the events
    GET  /v1/watch/{id}/events         change history for one monitor
    GET  /v1/watch/events              tenant-wide timeline
    GET  /v1/watch/stats               counts by kind (the console summary)
    POST /v1/watch/oneshot             fetch + diff a URL without keeping a monitor
    GET  /v1/watch/kinds               the event vocabulary
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep
from src.watch.diff import EVENT_KINDS
from src.watch.models import PRESET_SCHEDULES, human_schedule, parse_schedule

logger = logging.getLogger("wcp.api.watch")
router = APIRouter(prefix="/watch", tags=["watch"])


class TargetBody(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    label: str = ""
    focus: str = ""


class WatchBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    urls: list[str] = Field(default_factory=list, max_length=500)
    targets: list[TargetBody] = Field(default_factory=list, max_length=500)
    goal: str = Field("", max_length=500)
    kinds: list[str] = Field(default_factory=list)
    schedule: str = "1h"
    min_significance: float = Field(0.35, ge=0.0, le=1.0)
    notify_webhook: bool = True
    adaptive: bool = True

    def resolved_targets(self) -> list[dict[str, Any]]:
        rows = [t.model_dump() for t in self.targets]
        rows += [{"url": u} for u in self.urls]
        seen, out = set(), []
        for row in rows:
            if not row.get("url") or row["url"] in seen:
                continue
            seen.add(row["url"])
            out.append(row)
        return out


class PatchBody(BaseModel):
    name: str | None = None
    goal: str | None = None
    schedule: str | None = None
    state: str | None = Field(None, pattern="^(active|paused)$")
    min_significance: float | None = Field(None, ge=0.0, le=1.0)
    kinds: list[str] | None = None
    urls: list[str] | None = None
    adaptive: bool | None = None


class OneshotBody(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    kinds: list[str] = Field(default_factory=list)
    name: str = ""


def _platform(request: Request):
    return request.app.state.services.platform


@router.get("/kinds", summary="The change vocabulary and cadence options")
async def kinds() -> dict[str, Any]:
    return {
        "kinds": [
            {"id": "price_change", "label": "Price changed",
             "detail": "currency amounts differ, with magnitude and direction"},
            {"id": "new_product", "label": "New product appeared",
             "detail": "product rows (incl. schema.org Product) added"},
            {"id": "removed_product", "label": "Product disappeared",
             "detail": "product rows no longer present"},
            {"id": "policy_change", "label": "Policy wording changed",
             "detail": "privacy/terms/refund/SLA sentences differ"},
            {"id": "announcement", "label": "Announcement",
             "detail": "hero/headline text like 'we are launching'"},
            {"id": "jobs_change", "label": "Hiring activity changed",
             "detail": "careers/job-posting mentions moved"},
            {"id": "content_update", "label": "Substantial content change",
             "detail": "many lines added or removed"},
            {"id": "cosmetic", "label": "Cosmetic only",
             "detail": "nav/footer/whitespace — stored, never alerted"},
        ],
        "schedules": sorted(PRESET_SCHEDULES),
        "default_kinds": list(EVENT_KINDS),
    }


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a monitor")
async def create_watch(body: WatchBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    targets = body.resolved_targets()
    if not targets:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "at least one URL or target is required")
    service = _platform(request).watches
    watch = service.create(ctx["tenant"], body.name, targets,
                           goal=body.goal, kinds=body.kinds or None,
                           schedule=body.schedule,
                           min_significance=body.min_significance,
                           notify_webhook=body.notify_webhook,
                           adaptive=body.adaptive)
    _audit(request, ctx, "watch.create", watch_id=watch.watch_id,
           targets=len(targets))
    return watch.to_dict()


@router.get("", summary="List monitors")
async def list_watches(request: Request, ctx: CtxDep, state: str = "",
                       limit: Annotated[int, Query(ge=1, le=500)] = 100
                       ) -> dict[str, Any]:
    service = _platform(request).watches
    watches = service.list(ctx["tenant"], state=state or None, limit=limit)
    return {"items": [w.to_dict() for w in watches],
            "stats": service.stats(ctx["tenant"])}


@router.get("/events", summary="Tenant-wide change timeline")
async def all_events(request: Request, ctx: CtxDep,
                     limit: Annotated[int, Query(ge=1, le=200)] = 50,
                     min_significance: float = 0.0,
                     kinds: Annotated[list[str] | None, Query()] = None
                     ) -> dict[str, Any]:
    service = _platform(request).watches
    events = service.events(ctx["tenant"], limit=limit,
                            min_significance=min_significance, kinds=kinds)
    return {"items": [e.to_dict() for e in events],
            "stats": service.stats(ctx["tenant"])}


@router.get("/stats", summary="Monitoring summary for the console")
async def stats(request: Request, ctx: CtxDep) -> dict[str, Any]:
    service = _platform(request).watches
    tenant = ctx["tenant"]
    return {**service.stats(tenant),
            "recent": [e.to_dict() for e in service.events(tenant, limit=10)]}


@router.post("/oneshot", summary="Fetch + diff one URL, keep no monitor")
async def oneshot(body: OneshotBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    result = _platform(request).watch_once(body.url, kinds=body.kinds or None,
                                           tenant=ctx["tenant"], name=body.name)
    _audit(request, ctx, "watch.run", url=body.url, ad_hoc=True,
           events=len(result.get("events", [])))
    return result


@router.get("/{watch_id}", summary="One monitor")
async def get_watch(watch_id: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    service = _platform(request).watches
    watch = service.get(ctx["tenant"], watch_id)
    if watch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watch not found")
    recent = service.events(ctx["tenant"], watch_id=watch_id, limit=20)
    return {**watch.to_dict(), "recent_events": [e.to_dict() for e in recent],
            "next_run_in": human_schedule(
                int(max(0, watch.next_run_at - __import__("time").time())))}


@router.patch("/{watch_id}", summary="Update a monitor")
async def patch_watch(watch_id: str, body: PatchBody, request: Request, ctx: CtxDep
                      ) -> dict[str, Any]:
    service = _platform(request).watches
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    watch = service.update(ctx["tenant"], watch_id, **fields)
    if watch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watch not found")
    _audit(request, ctx, "watch.update", watch_id=watch_id, **fields)
    return watch.to_dict()


@router.delete("/{watch_id}", summary="Delete a monitor and its schedule")
async def delete_watch(watch_id: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    service = _platform(request).watches
    if service.get(ctx["tenant"], watch_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watch not found")
    service.delete(ctx["tenant"], watch_id)
    _audit(request, ctx, "watch.delete", watch_id=watch_id)
    return {"deleted": watch_id}


@router.post("/{watch_id}/run", summary="Run a monitor now")
async def run_watch(watch_id: str, request: Request, ctx: CtxDep,
                    force: bool = False) -> dict[str, Any]:
    service = _platform(request).watches
    try:
        result = service.run(ctx["tenant"], watch_id, force=force)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watch not found")
    _audit(request, ctx, "watch.run", watch_id=watch_id,
           events=len(result.get("events", [])))
    return result


@router.get("/{watch_id}/events", summary="Change history for one monitor")
async def watch_events(watch_id: str, request: Request, ctx: CtxDep,
                       limit: Annotated[int, Query(ge=1, le=200)] = 50,
                       min_significance: float = 0.0) -> dict[str, Any]:
    service = _platform(request).watches
    if service.get(ctx["tenant"], watch_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "watch not found")
    events = service.events(ctx["tenant"], watch_id=watch_id, limit=limit,
                            min_significance=min_significance)
    return {"watch_id": watch_id, "items": [e.to_dict() for e in events],
            "schedule": human_schedule(parse_schedule(None))}


def _audit(request: Request, ctx: dict, action: str, **fields: Any) -> None:
    try:
        _platform(request).audit.log(
            action, tenant=ctx.get("tenant", ""),
            actor=str(ctx.get("user") or ctx.get("principal", "")), **fields)
    except Exception as exc:  # noqa: BLE001
        logger.debug("audit write failed: %s", exc)
