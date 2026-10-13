"""Strawberry GraphQL schema for the WebCrawlerPro public API.

A thin, read-mostly graph over the same service container the REST routers
use (``app.state.services``), plus a mutation to submit crawl jobs. This lets
data teams compose one query for job status + usage + marketplace listings
instead of round-tripping five REST endpoints.

Mounted at ``/graphql`` by :mod:`src.api.main`.
"""

from __future__ import annotations

import time
from typing import List, Optional

import strawberry
from fastapi import Request
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import ID  # canonical GraphQL `ID` scalar

# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


@strawberry.type
class JobType:
    job_id: str
    state: str
    url: str
    max_pages: int
    pages_done: int
    items_collected: int
    created_at: float
    error: Optional[str] = None


@strawberry.type
class UsageType:
    tenant: str
    plan: str
    requests_used: int
    requests_limit: int
    storage_bytes_used: int
    period: str


@strawberry.type
class ListingType:
    dataset_id: str
    seller: str
    title: str
    description: str
    price_usd_cents: int
    tags: List[str]
    avg_rating: float
    review_count: int


@strawberry.type
class PlanType:
    id: str
    name: str
    monthly_usd_cents: int
    request_quota: int


@strawberry.type
class HealthType:
    status: str
    redis_ok: bool
    server_time: float


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _svc(info: strawberry.Info):
    return info.context["request"].app.state.services


def _ctx(info: strawberry.Info) -> dict:
    """Resolve tenant context from X-API-Key (same contract as REST routers)."""
    request: Request = info.context["request"]
    svc = _svc(info)
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        record = svc.api_keys.lookup(api_key)
        if record is not None:
            return {"tenant": record.tenant, "scopes": record.scopes,
                    "role": "member", "principal": "apikey"}
        raise PermissionError("invalid api key")
    ctx = getattr(request.state, "ctx", None)
    if ctx:
        return ctx
    raise PermissionError("authentication required: set X-API-Key header")


def _mutations_locked() -> bool:
    """GraphQL mutations are disabled on the public read-only surface."""
    return os.getenv("WCP_GRAPHQL_MUTATIONS", "0") != "1"


@strawberry.type
class ViewerType:
    tenant: str
    role: str
    principal: str


@strawberry.type
class JobPage:
    total: int
    items: List[JobType]


# --------------------------------------------------------------------------- #
# Queries / Mutations
# --------------------------------------------------------------------------- #


@strawberry.type
class Query:
    @strawberry.field(description="Authenticated identity for this request")
    def viewer(self, info: strawberry.Info) -> ViewerType:
        ctx = _ctx(info)
        return ViewerType(tenant=ctx.get("tenant") or "",
                          role=ctx.get("role") or "member",
                          principal=ctx.get("principal") or "apikey")

    @strawberry.field(description="Liveness + dependency snapshot")
    def health(self, info: strawberry.Info) -> HealthType:
        svc = _svc(info)
        ok = False
        try:
            ok = bool(svc.redis.ping())
        except Exception:
            pass
        return HealthType(status="ok" if ok else "degraded", redis_ok=ok,
                          server_time=time.time())

    @strawberry.field(description="Current tenant's crawl jobs (newest first)")
    def jobs(self, info: strawberry.Info, limit: int = 20,
             state: Optional[str] = None) -> JobPage:
        ctx = _ctx(info)
        svc = _svc(info)
        r = svc.redis
        out: List[JobType] = []
        total = int(r.zcard(f"wcp:v1:tenant_jobs:{ctx['tenant']}"))
        ids = r.zrevrange(f"wcp:v1:tenant_jobs:{ctx['tenant']}", 0, limit - 1)
        for jid in ids:
            jid_s = jid.decode() if isinstance(jid, bytes) else jid
            raw = r.hgetall(f"wcp:v1:job:{jid_s}")
            if not raw:
                continue
            rec = {(k.decode() if isinstance(k, bytes) else k):
                   (v.decode() if isinstance(v, bytes) else v)
                   for k, v in raw.items()}
            if state and rec.get("state") != state:
                continue
            out.append(JobType(
                job_id=rec.get("job_id", jid_s), state=rec.get("state", "queued"),
                url=rec.get("url", ""), max_pages=int(rec.get("max_pages", 0)),
                pages_done=int(rec.get("pages_done", 0)),
                items_collected=int(rec.get("items_collected", 0)),
                created_at=_to_ts(rec.get("created_at")),
                error=rec.get("error") or None,
            ))
        return JobPage(total=total, items=out)

    @strawberry.field(description="Single job by id (tenant-scoped)")
    def job(self, info: strawberry.Info, id: str) -> Optional[JobType]:  # noqa: A002
        ctx = _ctx(info)
        rec = _svc(info).redis.hgetall(f"wcp:v1:job:{id}")
        if not rec:
            return None
        rec = {(k.decode() if isinstance(k, bytes) else k):
               (v.decode() if isinstance(v, bytes) else v) for k, v in rec.items()}
        if rec.get("tenant") != ctx["tenant"]:
            return None
        return JobType(
            job_id=rec.get("job_id", id), state=rec.get("state", "queued"),
            url=rec.get("url", ""), max_pages=int(rec.get("max_pages", 0)),
            pages_done=int(rec.get("pages_done", 0)),
            items_collected=int(rec.get("items_collected", 0)),
            created_at=_to_ts(rec.get("created_at")),
            error=rec.get("error") or None,
        )

    @strawberry.field(description="Metered usage vs plan limits this period")
    def usage(self, info: strawberry.Info) -> UsageType:
        ctx = _ctx(info)
        svc = _svc(info)
        plan = svc.plan_for(ctx["tenant"])
        status = svc.quotas.usage(ctx["tenant"], "requests")
        return UsageType(
            tenant=ctx["tenant"], plan=getattr(plan, "name", str(plan)),
            requests_used=int(getattr(status, "used", 0) or 0),
            requests_limit=int(getattr(status, "limit", 0) or 0),
            storage_bytes_used=int(_storage_used(svc, ctx["tenant"])),
            period=str(getattr(status, "period", "monthly")),
        )

    @strawberry.field(description="Available subscription plans")
    def plans(self, info: strawberry.Info) -> List[PlanType]:
        from src.gateway.billing.plans import PLANS

        return [PlanType(id=p.name, name=p.display_name,
                         monthly_usd_cents=int(p.price_cents_month),
                         request_quota=int(p.quotas.requests_per_month))
                for p in PLANS.values()]

    @strawberry.field(description="Marketplace dataset listings")
    def marketplace_listings(self, info: strawberry.Info, query: str = "",
                             tag: str = "", limit: int = 20) -> List[ListingType]:
        from src.marketplace.service import MarketplaceService

        svc = _svc(info)
        mp = getattr(svc, "marketplace", None) or MarketplaceService(svc.redis)
        out: List[ListingType] = []
        for lst in mp.search_listings(query=query, tag=tag, limit=limit):
            avg, count = mp.rating_summary(lst.dataset_id)
            out.append(ListingType(
                dataset_id=lst.dataset_id, seller=lst.seller_tenant,
                title=lst.title,
                description=lst.description,
                price_usd_cents=int(lst.price_usd_cents),
                tags=list(lst.tags), avg_rating=avg, review_count=count,
            ))
        return out


def _to_ts(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _storage_used(svc, tenant: str) -> int:
    """Best-effort storage meter (reported in bytes; quota metric is GB)."""
    try:
        st = svc.quotas.usage(tenant, "storage_gb")
        return int(float(getattr(st, "used", 0) or 0) * (1024 ** 3))
    except Exception:
        return 0


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Submit a crawl job (same engine as REST)")
    def submit_crawl(self, info: strawberry.Info, url: str,
                     max_pages: int = 10, stealth: bool = False) -> JobType:
        ctx = _ctx(info)
        svc = _svc(info)
        import uuid
        from datetime import datetime, timezone

        job_id = f"job_{uuid.uuid4().hex[:20]}"
        now = datetime.now(timezone.utc)
        rec = {
            "job_id": job_id, "tenant": ctx["tenant"], "state": "queued",
            "url": url, "max_pages": str(max_pages), "pages_done": "0",
            "items_collected": "0", "tokens_generated": "0",
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "error": "",
        }
        r = svc.redis
        pipe = r.pipeline()
        pipe.hset(f"wcp:v1:job:{job_id}", mapping=rec)
        pipe.expire(f"wcp:v1:job:{job_id}", 60 * 60 * 24 * 30)
        pipe.zadd(f"wcp:v1:tenant_jobs:{ctx['tenant']}", {job_id: time.time()})
        pipe.execute()
        try:
            from src.compute.scheduler import JobSpec

            svc.scheduler.submit(JobSpec(
                kind="crawl_job",
                payload={"url": url, "max_pages": max_pages, "stealth": stealth,
                         "tenant": ctx["tenant"], "job_id": job_id},
                tenant=ctx["tenant"]))
        except Exception as exc:
            r.hset(f"wcp:v1:job:{job_id}",
                   mapping={"state": "failed", "error": str(exc)[:500]})
        state_raw = r.hget(f"wcp:v1:job:{job_id}", "state") or b"queued"
        state = state_raw.decode() if isinstance(state_raw, bytes) else str(state_raw)
        err_raw = r.hget(f"wcp:v1:job:{job_id}", "error") or b""
        err = err_raw.decode() if isinstance(err_raw, bytes) else str(err_raw)
        return JobType(job_id=job_id, state=state, url=url,
                       max_pages=max_pages, pages_done=0, items_collected=0,
                       created_at=now.timestamp(), error=err or None)


schema = strawberry.Schema(query=Query, mutation=Mutation)


def make_graphql_router() -> GraphQLRouter:
    """Build the FastAPI router; per-request context carries the HTTP request."""

    async def get_context(request: Request) -> dict:
        return {"request": request}

    return GraphQLRouter(schema, context_getter=get_context)
