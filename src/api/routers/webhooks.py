"""v1 router: outbound webhooks (subscribe / list / test / deliveries)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep
from src.api.schemas import WebhookCreate

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookTestRequest(BaseModel):
    subscription_id: str
    event: str = "crawl.completed"
    data: dict = Field(default_factory=lambda: {"job_id": "job_test",
                                                "pages": 1, "items": 1})


def _svc(request: Request):
    return request.app.state.services.webhooks


@router.post("", status_code=status.HTTP_201_CREATED, summary="Register webhook")
async def create_hook(body: WebhookCreate, ctx: CtxDep, request: Request) -> dict:
    svc = _svc(request)
    try:
        sub = svc.subscribe(ctx["tenant"], body.url,
                            [e.value for e in body.events])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"id": sub.id, "url": sub.url, "events": sub.events,
            "secret": sub.secret, "description": body.description,
            "note": "HMAC-SHA256 secret — shown once; verify "
                    "'X-WCP-Signature: v1=<hex>, t=<ts>' headers"}


@router.get("", summary="List webhook subscriptions")
async def list_hooks(ctx: CtxDep, request: Request) -> list[dict]:
    svc = _svc(request)
    return [{"id": s.id, "url": s.url, "events": s.events, "active": s.active}
            for s in svc.list_for_tenant(ctx["tenant"])]


@router.delete("/{sub_id}", summary="Delete a subscription")
async def delete_hook(sub_id: str, ctx: CtxDep, request: Request) -> dict:
    if not _svc(request).unsubscribe(ctx["tenant"], sub_id):
        raise HTTPException(404, "not found")
    return {"deleted": sub_id}


@router.post("/test", summary="Send a signed test event")
async def test_hook(body: WebhookTestRequest, ctx: CtxDep,
                    request: Request) -> dict:
    svc = _svc(request)
    subs = {s.id: s for s in svc.list_for_tenant(ctx["tenant"])}
    if body.subscription_id not in subs:
        raise HTTPException(404, "unknown subscription")
    delivery_ids = svc.emit(ctx["tenant"], body.event, body.data,
                            only_sub=body.subscription_id)
    return {"queued_deliveries": delivery_ids}


@router.get("/{sub_id}/deliveries", summary="Recent delivery attempts")
async def deliveries(sub_id: str, ctx: CtxDep, request: Request,
                     limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    svc = _svc(request)
    if svc.get_sub(ctx["tenant"], sub_id) is None:
        raise HTTPException(404, "not found")
    return svc.recent_deliveries(sub_id, limit=limit)
