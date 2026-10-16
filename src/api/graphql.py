"""Strawberry GraphQL schema for the WebCrawlerPro public API.

A thin, read-mostly graph over the same service container the REST routers
use (``app.state.services``), plus a mutation to submit crawl jobs. This lets
data teams compose one query for job status + usage + marketplace listings
instead of round-tripping five REST endpoints.

Mounted at ``/graphql`` by :mod:`src.api.main`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

import strawberry
from fastapi import Request
from fastapi.responses import JSONResponse
from strawberry.fastapi import GraphQLRouter

# Strawberry does not register the spec's built-in `ID` scalar implicitly —
# without this the schema has no `ID` type and queries like
# `query ($id: ID!) { job(id: $id) { ... } }` fail with "Unknown type 'ID'".
from strawberry.scalars import ID  # noqa: E402  canonical GraphQL `ID` scalar

logger = logging.getLogger("wcp.api.graphql")

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
    """

    def on_execute(self):
        # ``execution_context`` is populated before the hook runs; remember it
        # because the result may be produced after this generator resumes.
        ctx = self.execution_context
        result = yield
        errors = getattr(ctx, "result", None)
        errors = getattr(errors, "errors", None) or getattr(result, "errors", None)
        for err in errors or ():
            if isinstance(getattr(err, "original_error", None), PermissionError):
                raise err.original_error


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


def _carries_mutation(request_data: object) -> bool:
    """True when *request_data* holds at least one mutation operation.

    ``request_data`` is whatever Strawberry parsed out of the HTTP body: a
    :class:`~strawberry.http.types.GraphQLRequestData`, or a list of them for
    batched requests.
    """
    items = request_data if isinstance(request_data, (list, tuple)) \
        else [request_data]
    for item in items:
        query = getattr(item, "query", None)
        if not query or not isinstance(query, str):
            continue
        try:
            from graphql import parse
        except ImportError:  # pragma: no cover - graphql-core ships with strawberry
            return False
        try:
            document = parse(query)
        except Exception as exc:  # noqa: BLE001 - syntax error
            # Unparseable documents are strawberry's problem: it answers with a
            # 400 and the GraphQL-shaped syntax error. We only need to know
            # whether this was a mutation, and it demonstrably is not one we
            # can recognise, so let the request through.
            logger.debug("could not parse GraphQL query to classify it: %s",
                         exc)
            continue
        for definition in getattr(document, "definitions", ()) or ():
            operation = getattr(definition, "operation", None)
            name = getattr(operation, "value", operation)
            if name and str(name).lower() == "mutation":
                return True
    return False


def _unauthorized(exc: PermissionError) -> JSONResponse:
    """Shape a ``PermissionError`` as a GraphQL-flavoured 401 response."""
    message = str(exc) or "authentication required"
    code = ("invalid_api_key" if "api key" in message.lower()
            else "unauthorized")
    return JSONResponse(
        status_code=401,
        content={"errors": [{"message": message}], "data": None},
        headers={"WWW-Authenticate": "Bearer", "X-Error-Code": code})


def make_graphql_router() -> GraphQLRouter:
    """Build the FastAPI router; per-request context carries the HTTP request.

    Strawberry turns resolver exceptions into ``200 + errors[]``; we want real
    HTTP semantics for auth failures (401), so any ``PermissionError`` raised
    by a resolver is translated into a 401 on the way out.

    The translation is installed on ``run()`` — the single execution entry
    point shared by the POST, GET and websocket handlers — rather than on the
    POST handler. Strawberry keeps renaming and relocating that handler
    (``post_template`` → ``handle_http_post``) and registers it as a route
    inside ``__init__``, so patching it by name silently stops taking effect
    on newer releases and leaves ``/graphql`` unmounted.
    """

    async def get_context(request: Request) -> dict:
        return {"request": request}

    router = GraphQLRouter(schema, context_getter=get_context)

    # Read-only deployments must refuse mutations *before* the document is
    # validated: ``Mutation.submit_crawl`` raises MutationLockError, but an
    # unknown or invalid mutation (``mutation { hack }``) never reaches a
    # resolver, so the lock has to sit on the transport, not in the schema.
    original_execute_operation = getattr(router, "execute_operation", None)
    if original_execute_operation is not None:
        async def execute_operation_with_mutation_lock(*args, **kwargs):
            request_data = kwargs.get("request_data")
            if request_data is None and len(args) >= 3:
                request_data = args[2]
            if _mutations_locked() and _carries_mutation(request_data):
                raise MutationLockError(
                    "mutations are disabled on this read-only GraphQL surface")
            return await original_execute_operation(*args, **kwargs)

        router.execute_operation = (  # type: ignore[method-assign]
            execute_operation_with_mutation_lock)
    else:  # pragma: no cover - strawberry without AsyncBaseHTTPView
        logger.warning("cannot enforce the GraphQL mutation lock: no "
                       "execute_operation hook on %s", type(router).__name__)

    original_run = getattr(router, "run", None)
    if original_run is not None:
        async def run_with_auth_status(*args, **kwargs):
            try:
                return await original_run(*args, **kwargs)
            except PermissionError as exc:
                return _unauthorized(exc)

        router.run = run_with_auth_status  # type: ignore[method-assign]
    else:  # pragma: no cover - very old strawberry without AsyncBaseHTTPView
        original_post = getattr(router, "post_template", None)
        if original_post is not None:
            async def post_with_auth_status(*args, **kwargs):
                try:
                    return await original_post(*args, **kwargs)
                except PermissionError as exc:
                    return _unauthorized(exc)

            router.post_template = post_with_auth_status  # type: ignore[assignment]
        else:
            logger.warning(
                "cannot install GraphQL 401 translation: no supported "
                "handler hook found on %s", type(router).__name__)

    return router
