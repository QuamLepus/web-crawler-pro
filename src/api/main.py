"""WebCrawlerPro v1 API application factory.

Composes the FastAPI app around the gateway auth services:

* :class:`AuthContextMiddleware` — resolves the caller (JWT bearer or
  ``X-API-Key``) once per request and stashes an auth context on
  ``request.state`` (``ctx`` / ``tenant``) so routers can depend on it.
* :class:`~src.api.middleware.RateLimitMiddleware` — Redis token-bucket /
  fixed-window limiter with plan-aware caps.
* GraphQL endpoint (``/graphql``) — a tiny dependency-free executor for the
  documented ``viewer`` / ``job`` / ``jobs`` queries, auth-gated like REST.
* Routers: ``/v1/jobs``, ``/v1/search``, ``/v1/billing``, ``/v1/webhooks``,
  ``/v1/marketplace``.

Run:  uvicorn src.api.main:app --port 8001
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.api.middleware import RateLimitConfig, RateLimitMiddleware
from src.api.routers import billing, jobs, marketplace, search, webhooks
from src.gateway.auth.tokens import TokenExpired, TokenInvalid, decode_token
from src.gateway.billing.plans import get_plan

logger = logging.getLogger("wcp.api")

# Paths that never require authentication.
PUBLIC_PATHS: tuple[str, ...] = (
    "/healthz", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json",
)


# --------------------------------------------------------------------------- #
# Auth context middleware
# --------------------------------------------------------------------------- #
class AuthContextMiddleware(BaseHTTPMiddleware):
    """Resolve JWT/API-key credentials into ``request.state.ctx``.

    On success sets:
      * ``request.state.ctx``     – dict(principal, tenant, key_id, scopes, role, user)
      * ``request.state.tenant``  – convenience for rate-limiter & routers

    Public paths are always passed through unauthenticated. For any other
    path the behaviour depends on ``require_auth``:

      * ``require_auth=True`` (default): missing/invalid credentials -> 401
        JSON with structured error body (and ``WWW-Authenticate`` header).
      * ``require_auth=False``: anonymous requests are allowed but carry no
        ctx; authenticated ones still get their tenant resolved.
    """

    def __init__(self, app, services_getter: Callable[[], Any], *,
                 require_auth: bool = True,
                 public_paths: tuple[str, ...] = PUBLIC_PATHS) -> None:
        super().__init__(app)
        self._services_getter = services_getter
        self.require_auth = require_auth
        self.public_paths = tuple(public_paths)

    # ------------------------------------------------------------------ #
    def _is_public(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.public_paths)

    @staticmethod
    def _unauthorized(detail: str) -> Response:
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": detail}},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def dispatch(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if self._is_public(path):
            return await call_next(request)

        try:
            svc = self._services_getter()
        except Exception as exc:  # pragma: no cover - wiring bug
            logger.exception("services unavailable")
            return JSONResponse({"error": {"code": "internal",
                                           "message": f"services unavailable: {exc}"}},
                                status_code=500)

        ctx, err = self._resolve_ctx(svc, request)

        if err is not None:
            return self._unauthorized(err)

        if ctx is None:
            # No valid credential present.
            if self.require_auth:
                return self._unauthorized(
                    "provide Authorization: Bearer <jwt> or X-API-Key header")
            return await call_next(request)

        request.state.ctx = ctx
        request.state.tenant = ctx["tenant"]
        return await call_next(request)

    # ------------------------------------------------------------------ #
    def _resolve_ctx(self, svc, request: Request) -> Optional[dict]:
        """Return ``(ctx_or_None, error_message_or_None)``."""
        # 1) API key takes precedence for machine traffic.
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            record = svc.api_keys.lookup(api_key)
            if record is None:
                return None, "invalid api key"
            return {"principal": "apikey", "tenant": record.tenant,
                    "key_id": record.key_id, "scopes": record.scopes,
                    "role": "member", "user": None}, None

        # 2) Bearer JWT.
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            try:
                claims = decode_token(token)
            except TokenExpired:
                return None, "token expired"
            except TokenInvalid:
                return None, "invalid token"
            if claims.get("typ") != "access":
                return None, "wrong token type"
            try:
                user = svc.users.get_user(claims["sub"])
            except Exception as exc:  # defensive: storage layer hiccup
                logger.exception("user lookup failed")
                return None, f"user lookup failed: {exc}"
            if user is None or getattr(user, "disabled", False) \
                    or svc.users.is_locked(user.id):
                return None, "user unavailable"
            return {"principal": "jwt", "tenant": user.tenant, "key_id": user.id,
                    "scopes": ["read", "crawl", "admin"], "role": user.role,
                    "user": user}, None
        return None, None


# --------------------------------------------------------------------------- #
# Minimal GraphQL executor (no external deps)
# --------------------------------------------------------------------------- #
re_job_arg = re.compile(r"\bjob\s*\(")
re_job_literal = re.compile(r'job\s*\(\s*id:\s*"([^"]+)"')
re_jobs_field = re.compile(r"\bjobs\s*[\{(]")


def _gql_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"errors": [{"message": message}]}, status_code=status_code)


def execute_graphql(services, ctx: Optional[dict], query: str,
                    variables: Optional[dict] = None) -> dict:
    """Evaluate the supported read-only operations.

    Supported queries::

        { viewer { tenant principal role } }
        query ($id: ID!) { job(id: $id) { jobId state url maxPages pagesDone } }
        { jobs { items { jobId state url } total } }
    """
    variables = variables or {}
    q = (query or "").strip()
    if not q:
        raise ValueError("empty query")
    if "mutation" in q.split("{")[0].lower():
        raise PermissionError("mutations are not exposed over GraphQL; use REST")

    def _need_tenant() -> str:
        if ctx is None:
            raise PermissionError("authentication required for GraphQL")
        return ctx["tenant"]

    data: dict[str, Any] = {}

    if "viewer" in q:
        tenant = _need_tenant()
        org = services.users.get_org(tenant)
        data["viewer"] = {
            "tenant": tenant,
            "name": org.name if org else tenant,
            "plan": services.plan_for(tenant).name,
            "principal": ctx["principal"],
            "role": ctx["role"],
        }

    if re_job_arg.search(q):
        job_id = variables.get("id") or variables.get("jobId") or ""
        if not job_id:
            m = re_job_literal.search(q)
            job_id = m.group(1) if m else ""
        if not job_id:
            raise ValueError("job(id:) requires an id argument")
        tenant = _need_tenant()
        st = services.scheduler.status(job_id)
        if st is None or st.get("tenant") != tenant:
            data["job"] = None
        else:
            data["job"] = {"jobId": job_id, "state": st.get("status", "unknown"),
                           "url": st.get("url", ""), "detail": st}

    if re_jobs_field.search(q):
        tenant = _need_tenant()
        r = services.redis
        members = r.zrevrange(jobs.TENANT_JOBS.format(tenant=tenant), 0, 49)
        items = []
        for jid in members:
            jid_s = jid.decode() if isinstance(jid, bytes) else jid
            rec = r.hgetall(jobs.JOB_KEY.format(job_id=jid_s))
            dec = lambda k: k.decode() if isinstance(k, bytes) else k  # noqa: E731
            rd = {dec(k): dec(v) for k, v in rec.items()} if rec else {}
            items.append({"jobId": jid_s, "state": rd.get("state", "queued"),
                          "url": rd.get("url", ""),
                          "maxPages": int(rd.get("max_pages", 0) or 0),
                          "pagesDone": int(rd.get("pages_done", 0) or 0)})
        total = r.zcard(jobs.TENANT_JOBS.format(tenant=tenant)) or 0
        data["jobs"] = {"items": items, "total": int(total)}

    if not data:
        raise ValueError("unsupported GraphQL operation")
    return {"data": data}


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_api_app(redis_client=None, *, require_auth: bool = True) -> FastAPI:
    from src.gateway.app import Services, _default_redis

    app = FastAPI(
        title="WebCrawlerPro v1 API",
        version="1.0.0",
        description="Public v1 surface: crawl jobs, semantic search/RAG, "
                    "billing, webhooks, marketplace and GraphQL.",
        docs_url="/docs", redoc_url="/redoc",
    )
    app.state.services = Services(redis_client if redis_client is not None
                                  else _default_redis())

    # Order matters: rate-limit outermost, auth innermost so the limiter can
    # read request.state.tenant set by auth (plan-aware caps).
    app.add_middleware(AuthContextMiddleware,
                       services_getter=lambda: app.state.services,
                       require_auth=require_auth)
    plan_of_tenant = lambda t: RateLimitConfig.from_plan(  # noqa: E731
        get_plan(app.state.services.plan_for(t).name).quotas.api_rate_per_minute)
    app.add_middleware(RateLimitMiddleware,
                       redis_client=app.state.services.redis,
                       plan_resolver=plan_of_tenant)

    # ---- ops ---------------------------------------------------------- #
    @app.get("/healthz", tags=["ops"])
    async def healthz():
        return {"status": "ok", "ts": time.time()}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request):
        try:
            request.app.state.services.redis.ping()
        except Exception as exc:
            raise HTTPException(503, f"redis not ready: {exc}")
        return {"status": "ready"}

    # ---- auth & keys (delegated to the gateway implementation) -------- #
    from src.gateway import app as gateway_app

    _saved_app = getattr(gateway_app, "app", None)
    gateway_app.app = app  # route decorators inside create_app attach to `app`
    try:
        gateway_app.create_app(redis_client=app.state.services.redis)
    finally:
        gateway_app.app = _saved_app

    # ---- v1 routers --------------------------------------------------- #
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(search.router, prefix="/v1")
    app.include_router(billing.router, prefix="/v1")
    app.include_router(webhooks.router, prefix="/v1")
    app.include_router(marketplace.router, prefix="/v1")

    # ---- GraphQL ------------------------------------------------------ #
    @app.post("/graphql", tags=["graphql"])
    async def graphql_endpoint(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _gql_error("invalid JSON body")
        query = body.get("query", "")
        variables = body.get("variables") or {}
        ctx = getattr(request.state, "ctx", None)
        try:
            result = execute_graphql(request.app.state.services, ctx, query,
                                     variables)
        except PermissionError as exc:
            return _gql_error(str(exc), 401)
        except HTTPException as exc:  # invalid credentials surfaced by resolver
            return _gql_error(str(exc.detail), exc.status_code)
        except (ValueError, KeyError) as exc:
            return _gql_error(str(exc))
        return JSONResponse(result)

    @app.get("/graphql", tags=["graphql"])
    async def graphql_get(request: Request, query: str = "",
                          variables: str = ""):
        try:
            vars_dict = json.loads(variables) if variables else {}
        except json.JSONDecodeError:
            return _gql_error("variables must be valid JSON")
        ctx = getattr(request.state, "ctx", None)
        try:
            result = execute_graphql(request.app.state.services, ctx, query,
                                     vars_dict)
        except PermissionError as exc:
            return _gql_error(str(exc), 401)
        except HTTPException as exc:
            return _gql_error(str(exc.detail), exc.status_code)
        except (ValueError, KeyError) as exc:
            return _gql_error(str(exc))
        return JSONResponse(result)

    return app


try:  # module-level ASGI object for `uvicorn src.api.main:app`
    app = create_api_app()
except Exception:  # pragma: no cover
    logger.exception("v1 api app failed to start")
    raise
