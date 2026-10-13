"""Strawberry GraphQL schema for the WebCrawlerPro public API.

A thin, read-mostly graph over the same service container the REST routers
use (``app.state.services``), plus a mutation to submit crawl jobs. This lets
data teams compose one query for job status + usage + marketplace listings
instead of round-tripping five REST endpoints.

Mounted at ``/graphql`` by :mod:`src.api.main`.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

import strawberry
from fastapi import Request
from fastapi.responses import JSONResponse
from graphql.language import OperationDefinitionNode
from strawberry.fastapi import GraphQLRouter

# Version-compat shim: older strawberry releases exposed `strawberry.Info` at
# the package root; newer ones (>=0.2xx) moved it to `strawberry.types`.
if not hasattr(strawberry, "Info"):  # pragma: no cover - depends on version
    from strawberry.types import Info as _Info

    strawberry.Info = _Info  # type: ignore[attr-defined]

# Strawberry does not register the spec's built-in `ID` scalar implicitly —
# without this the schema has no `ID` type and queries like
# `query ($id: ID!) { job(id: $id) { ... } }` fail with "Unknown type 'ID'".
from strawberry.scalars import ID  # noqa: E402  canonical GraphQL `ID` scalar

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


class MutationLockError(PermissionError):
    """Raised when a client attempts a mutation on the read-only surface.

    Mapped to HTTP 401 by :func:`make_graphql_router` — authentication is not
    the problem here, write access simply is not offered by this deployment.
    """


class AuthToHTTPStatusExtension(strawberry.extensions.SchemaExtension):
    """Re-raise auth failures so the ASGI layer can answer with HTTP 401.

    Strawberry normally swallows resolver exceptions into ``errors[]`` and
    replies 200; anonymous / invalid-key / locked-mutation requests must
    instead look like ordinary 401 responses to API consumers.

    Version-compat notes:
      * Older strawberry exposed a sync ``on_execute``/``on_operation`` hook;
        newer releases (>=0.2xx) drive hooks through
        ``ExtensionContextManager``, which treats ``async def`` hooks as
        coroutine functions and sync hooks as generators. We implement both
        flavours (sync generator + async wrapper) sharing one helper.
      * graphql-core answers *validation* errors (e.g. an unknown mutation
        field) without ever running resolvers — no PermissionError exists in
        that path. Since mutations are locked on this read-only surface, we
        translate "client tried to run a mutation" into the same 401 contract
        up front, in ``on_operation`` (runs before parse/validate/execute in
        every strawberry version).
    """

    @staticmethod
    def _raise_if_permission(errors) -> None:
        for err in errors or ():
            if isinstance(getattr(err, "original_error", None), PermissionError):
                raise err.original_error

    def _check_mutation_lock(self) -> None:
        """Locked mutation surface: any mutation operation (valid field or
        not) must look like a 401, matching the runtime MutationLockError
        behaviour when the field does exist."""
        if not _mutations_locked():
            return
        try:
            ctx = self.execution_context
            doc = getattr(ctx, "graphql_document", None)
            for defn in getattr(doc, "definitions", ()) or ():
                if (isinstance(defn, OperationDefinitionNode)
                        and defn.operation == "mutation"):
                    raise MutationLockError(
                        "mutations are disabled on this read-only"
                        " GraphQL surface")
        except PermissionError:
            raise
        except Exception:  # pragma: no cover - defensive: never mask valid errors
            pass

    def _handle_operation(self) -> None:
        # Runs before parse/validate/execute: catches locked-mutation attempts
        # even when the mutation field itself is unknown (validation error).
        self._check_mutation_lock()

    def on_operation(self):
        # Sync generator hook (supported across all strawberry versions).
        self._handle_operation()
        yield

    async def on_operation_async(self):
        # Async variant for releases that await async hooks only.
        self._handle_operation()
        yield

    def on_validate(self):
        # Sync hook: runs around the validation step; raising here propagates
        # out of execute_sync before errors are turned into a 200 response.
        self._check_mutation_lock()
        yield

    async def on_validate_async(self):
        # Async hook used by newer strawberry releases (>=0.2xx).
        self._check_mutation_lock()
        yield

    def on_execute(self):
        # ``execution_context`` is populated before the hook runs; remember it
        # because the result may be produced after this generator resumes.
        ctx = self.execution_context
        result = yield
        errors = getattr(ctx, "result", None)
        errors = getattr(errors, "errors", None) or getattr(result, "errors", None)
        self._raise_if_permission(errors)

    async def on_execute_async(self):
        ctx = self.execution_context
        result = yield
        errors = getattr(ctx, "result", None)
        errors = getattr(errors, "errors", None) or getattr(result, "errors", None)
        self._raise_if_permission(errors)


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
    def job(self, info: strawberry.Info,
            id: ID) -> Optional[JobType]:  # noqa: A002, E501
        ctx = _ctx(info)
        id = str(id)
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
        if _mutations_locked():
            raise MutationLockError(
                "mutations are disabled on this read-only GraphQL surface")
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


schema = strawberry.Schema(query=Query, mutation=Mutation,
                           extensions=[AuthToHTTPStatusExtension])


def make_graphql_router() -> GraphQLRouter:
    """Build the FastAPI router; per-request context carries the HTTP request."""

    async def get_context(request: Request) -> dict:
        return {"request": request}

    router = GraphQLRouter(schema, context_getter=get_context)

    # Strawberry turns resolver exceptions into 200 + errors[]; we want real
    # HTTP semantics for auth failures (401), so translate them here.
    # Version-compat: older strawberry-graphql exposed `post_template` on the
    # router; newer versions (>=0.2xx) route POSTs through `get_root_value`/
    # `run` internals and no longer have that attribute. Wrap whichever entry
    # point exists so the monkey-patch works across versions.
    original_post = getattr(router, "post_template", None)

    if original_post is not None:

        async def post_with_auth_status(*args, **kwargs):
            try:
                return await original_post(*args, **kwargs)
            except PermissionError as exc:
                message = str(exc) or "authentication required"
                code = ("invalid_api_key" if "api key" in message.lower()
                        else "unauthorized")
                return JSONResponse(
                    status_code=401,
                    content={"errors": [{"message": message}],
                             "data": None},
                    headers={"WWW-Authenticate": "Bearer",
                             "X-Error-Code": code})

        router.post_template = post_with_auth_status  # type: ignore[assignment]
    else:
        # Newer strawberry: wrap the ASGI app callable instead, catching
        # PermissionError raised out of resolvers during request handling.
        original_app = router.app if hasattr(router, "app") else None

        if original_app is not None:  # pragma: no cover - defensive

            async def app_with_auth_status(scope, receive, send):
                try:
                    await original_app(scope, receive, send)
                except PermissionError as exc:
                    message = str(exc) or "authentication required"
                    code = ("invalid_api_key" if "api key" in message.lower()
                            else "unauthorized")
                    response = JSONResponse(
                        status_code=401,
                        content={"errors": [{"message": message}],
                                 "data": None},
                        headers={"WWW-Authenticate": "Bearer",
                                 "X-Error-Code": code})
                    await response(scope, receive, send)

            router.app = app_with_auth_status  # type: ignore[attr-defined]

    return router
