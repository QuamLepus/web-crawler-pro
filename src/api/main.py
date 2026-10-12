"""FastAPI application entrypoint for the WebCrawlerPro public API.

This is the runnable service (``uvicorn src.api.main:app``). It composes:

* **REST v1 routers** — jobs, search, webhooks, billing, marketplace
  (all under ``/api/v1``, contract defined in :mod:`src.api.schemas`).
* **GraphQL** — Strawberry schema at ``/graphql`` (+ GraphiQL IDE at
  ``/graphql/playground``) over the same service container.
* **Auth context** — resolves ``X-API-Key`` / JWT bearer into
  ``request.state.ctx`` before any router runs (see :func:`_AuthContextMiddleware`).
* **Rate limiting** — Redis token-bucket per credential
  (:class:`src.api.middleware.RateLimitMiddleware`), plan-aware.
* **Observability** — OpenTelemetry tracing, Prometheus ``/metrics``,
  request latency headers, Sentry error capture
  (:mod:`src.api.observability`).
* **Health probes** — ``/healthz`` (liveness), ``/readyz`` (readiness incl.
  Redis + scheduler check), ``/livez`` alias, all unauthenticated &
  metrics-excluded for k8s.

The service container (:class:`ApiServices`) reuses the gateway's auth,
quota, billing and webhook engines so REST, GraphQL and the internal gateway
always agree on tenant state.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Annotated, Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger("wcp.api")

API_PREFIX = "/api/v1"


# --------------------------------------------------------------------------- #
# Service container
# --------------------------------------------------------------------------- #
def _default_redis():
    """Real Redis when reachable, fakeredis otherwise (dev/test friendly)."""
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(url, decode_responses=False)
        client.ping()
        return client
    except Exception as exc:
        logger.warning("redis unavailable (%s); falling back to fakeredis", exc)
        import fakeredis

        return fakeredis.FakeRedis(decode_responses=False)


class ApiServices:
    """Lazy singleton wiring every dependency the routers touch.

    Attributes mirror what the routers read off ``request.app.state.services``:
    ``redis``, ``api_keys``, ``quotas``, ``webhooks``, ``scheduler``,
    ``billing``, ``plan_for()``, plus a lazily-created ``marketplace``.
    """

    def __init__(self, redis_client: Any = None) -> None:
        self._redis_override = redis_client
        self._built = False

    # lazy build so importing this module never needs network services ------ #
    def _build(self) -> None:
        if self._built:
            return
        import fakeredis  # noqa: F401  (kept for type of fallback client)

        from src.compute.scheduler import SchedulerService
        from src.gateway.auth.tokens import ApiKeyStore
        from src.gateway.billing.plans import get_plan
        from src.gateway.billing.quotas import QuotaService
        from src.gateway.billing.stripe_sync import BillingService
        from src.gateway.webhooks.service import WebhookService

        r = self._redis_override if self._redis_override is not None \
            else _default_redis()
        self.redis = r
        self.api_keys = ApiKeyStore(r)
        self.users = _make_userstore(r)
        self.quotas = QuotaService(r, plan_resolver=self.plan_for)
        self.webhooks = WebhookService(r)
        self.billing = BillingService(self.users, r)
        self.marketplace = None  # routers instantiate on demand
        try:
            from src.celery_app import celery_app
        except Exception:
            celery_app = None
        self.scheduler = SchedulerService(r, celery_app=celery_app)
        self._get_plan = get_plan
        self._built = True

    def __getattr__(self, item):  # triggers lazy build once
        self._build()
        return getattr(self, item)

    # ------------------------------------------------------------------ #
    def plan_for(self, tenant: str):
        org = None
        try:
            org = self.users.get_org(tenant)
        except Exception:
            pass
        return self._get_plan(org.plan if org else "free")


def _make_userstore(redis_client):
    try:
        from src.gateway.auth.users import UserStore

        return UserStore(redis_client)
    except Exception:  # pragma: no cover - defensive
        return None


# --------------------------------------------------------------------------- #
# Auth-context middleware (populates request.state.ctx for the routers)
# --------------------------------------------------------------------------- #
PUBLIC_PATHS = {"/healthz", "/livez", "/readyz", "/metrics", "/", "/docs",
                "/redoc", "/openapi.json", "/graphql", "/graphql/playground"}


class AuthContextMiddleware:
    """ASGI middleware resolving credentials into ``request.state.ctx``.

    Kept as pure ASGI (not BaseHTTPMiddleware) so it wraps everything cheaply;
    actual rejection happens in the routers' ``authenticate`` dependency —
    here we only *attach* identity when a valid credential is present.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        svc = scope["app"].state.services
        ctx = None
        api_key = headers.get("x-api-key", "")
        if api_key:
            try:
                record = svc.api_keys.lookup(api_key)
                if record is not None:
                    ctx = {"tenant": record.tenant, "scopes": record.scopes,
                           "role": "member", "user": None}
            except Exception:
                ctx = None
        if ctx is None:
            authz = headers.get("authorization", "")
            if authz.lower().startswith("bearer "):
                try:
                    from src.gateway.auth.tokens import decode_token

                    claims = decode_token(authz[7:])
                    if claims.get("typ", "access") == "access":
                        ctx = {"tenant": claims.get("tenant"),
                               "scopes": ["read", "crawl"],
                               "role": claims.get("role", "member"),
                               "user": claims.get("sub")}
                except Exception:
                    ctx = None
        # Always attach identity when resolvable (rate-limit middleware reads
        # request.state.tenant); only *reject* on protected paths.
        scope.setdefault("state", {})
        if ctx is not None:
            scope["state"]["ctx"] = ctx
            if ctx.get("tenant"):
                scope["state"]["tenant"] = ctx["tenant"]
        public = any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS) \
            or path.startswith("/api/auth")
        if not public and ctx is None:
            from starlette.responses import JSONResponse

            resp = JSONResponse({"detail": "authentication required"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_api_app(redis_client=None, *, enable_graphql: bool = True,
                   enable_rate_limit: bool = True) -> FastAPI:
    """Build the fully-wired public API application."""
    app = FastAPI(
        title="WebCrawlerPro Public API",
        version="2.1.0",
        description=(
            "Petabyte-scale web scraping & LLM training-data platform.\n\n"
            "REST surface lives under `/api/v1`; a GraphQL alternative is "
            "available at `/graphql`. Authenticate with `X-API-Key` or a "
            "JWT `Authorization: Bearer` token."
        ),
        contact={"name": "WebCrawlerPro Platform", "email": "api@webcrawlerpro.example"},
        license_info={"name": "Apache-2.0"},
        docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
    )
    app.state.services = ApiServices(
        redis_client if redis_client is not None else _default_redis())
    app.state.services._build()  # eager build at startup; fail loud early

    # ---- middleware (order: outermost added last) -------------------------- #
    app.add_middleware(AuthContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv(
            "CORS_ORIGINS", "https://app.webcrawlerpro.example").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining",
                        "X-Process-Time-Ms", "Retry-After"],
    )
    if enable_rate_limit:
        try:
            from src.api.middleware import RateLimitConfig, RateLimitMiddleware

            svc = app.state.services

            def _rate_config_for_tenant(tenant: str) -> RateLimitConfig:
                q = svc.plan_for(tenant).quotas
                return RateLimitConfig.from_plan(q.api_rate_per_minute,
                                                 burst=q.burst_per_minute)

            app.add_middleware(
                RateLimitMiddleware,
                redis_client=svc.redis,
                config=RateLimitConfig(),
                plan_resolver=_rate_config_for_tenant,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("rate-limit middleware disabled (%s)", exc)

    from src.api.observability import setup_observability

    setup_observability(app)

    @app.middleware("http")
    async def process_time_header(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = \
            f"{(time.perf_counter() - t0) * 1000:.1f}"
        return response

    # ---- exception mapping -------------------------------------------------- #
    from src.gateway.billing.quotas import QuotaExceededError
    from src.marketplace.service import MarketplaceError

    @app.exception_handler(QuotaExceededError)
    async def quota_handler(_req: Request, exc: QuotaExceededError):
        return JSONResponse(status_code=402, content={
            "error": "quota_exceeded", "metric": exc.status.metric,
            "used": exc.status.used, "limit": exc.status.limit,
            "period": exc.status.period, "upgrade": f"{API_PREFIX}/billing/checkout",
        })

    @app.exception_handler(MarketplaceError)
    async def marketplace_handler(_req: Request, exc: MarketplaceError):
        return JSONResponse(status_code=400,
                            content={"error": "marketplace_error",
                                     "detail": str(exc)})

    # ---- health probes (k8s liveness/readiness, unauthenticated) ------------ #
    @app.get("/healthz", tags=["ops"], summary="Liveness probe")
    async def healthz():
        return {"status": "ok", "service": "webcrawlerpro-api",
                "version": app.version, "ts": time.time()}

    @app.get("/livez", tags=["ops"], include_in_schema=False)
    async def livez():
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"], summary="Readiness probe (Redis + scheduler)")
    async def readyz(request: Request):
        svc = request.app.state.services
        checks: dict[str, Any] = {}
        try:
            checks["redis"] = bool(svc.redis.ping())
        except Exception as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"redis not ready: {exc}")
        checks["scheduler"] = hasattr(svc.scheduler, "submit")
        if not all(checks.values()):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, checks)
        return {"status": "ready", "checks": checks}

    # ---- root / index -------------------------------------------------------- #
    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "service": "WebCrawlerPro Public API",
            "version": app.version,
            "docs": "/docs",
            "graphql": "/graphql",
            "metrics": "/metrics",
            "health": ["/healthz", "/readyz"],
            "endpoints": [f"{API_PREFIX}{p}" for p in
                          ("/jobs", "/search", "/webhooks", "/billing",
                           "/marketplace")],
        }

    # ---- REST v1 routers ------------------------------------------------------ #
    from src.api.routers import billing, jobs, marketplace, search, webhooks

    api = APIRouter = __import__("fastapi").APIRouter
    v1 = api(prefix=API_PREFIX)
    v1.include_router(jobs.router)
    v1.include_router(search.router)
    v1.include_router(webhooks.router)
    v1.include_router(billing.router)
    v1.include_router(marketplace.router)
    app.include_router(v1)

    # ---- minimal auth helpers so tenants can bootstrap keys via this app ---- #
    _mount_auth_routes(app)

    # ---- GraphQL -------------------------------------------------------------- #
    if enable_graphql:
        try:
            from src.api.graphql import make_graphql_router

            app.include_router(make_graphql_router(), prefix="/graphql")
            logger.info("GraphQL mounted at /graphql")
        except Exception as exc:  # pragma: no cover - strawberry missing
            logger.warning("GraphQL disabled (strawberry unavailable: %s)", exc)

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - trivial
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper())
        logger.info("WebCrawlerPro API ready (prefix=%s)", API_PREFIX)

    return app


try:  # module-level ASGI object for `uvicorn src.api.main:app`
    app = create_api_app()
except Exception:  # pragma: no cover
    logger.exception("v1 api app failed to start")
    raise
# --------------------------------------------------------------------------- #
# Auth bootstrap models (module scope: FastAPI resolves annotations reliably)
# --------------------------------------------------------------------------- #
from pydantic import BaseModel, Field


class SignupBody(BaseModel):
    org_name: str = Field(min_length=2, max_length=120)
    email: str = Field(pattern=r"^[\w.+-]+@[\w-]+\.[\w.-]+$")
    password: str = Field(min_length=8, max_length=128)
    name: str = ""
    plan: str = "free"


class LoginBody(BaseModel):
    email: str
    password: str


class ApiKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = ["read", "crawl"]


def _mount_auth_routes(app: FastAPI) -> None:
    """Signup / login / API-key issuance routes needed by SDKs & tests."""
    from src.gateway.auth.tokens import issue_session

    @app.post("/api/auth/signup", tags=["auth"], summary="Create org + user")
    async def signup(body: SignupBody, request: Request):
        svc = request.app.state.services
        try:
            org = svc.users.create_org(body.org_name, plan=body.plan)
            user = svc.users.create_user(body.email, body.password, org.id,
                                         "owner", name=body.name)
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e))
        sess = issue_session(user.id, org.id, user.role)
        return {**sess, "org_id": org.id, "user_id": user.id}

    @app.post("/api/auth/login", tags=["auth"], summary="Login -> JWT pair")
    async def login(body: LoginBody, request: Request):
        svc = request.app.state.services
        user = svc.users.authenticate(body.email, body.password)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        return issue_session(user.id, user.tenant, user.role)

    @app.post("/api/auth/api-keys", tags=["auth"], status_code=201,
              summary="Issue an API key (Bearer JWT required)")
    async def create_api_key(body: ApiKeyBody, request: Request):
        svc = request.app.state.services
        ctx = getattr(request.state, "ctx", None)
        if not ctx or not ctx.get("user"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "JWT session required to mint API keys")
        record, plaintext = svc.api_keys.create(ctx["tenant"], body.name,
                                                body.scopes)
        return {"key_id": record.key_id, "api_key": plaintext,
                "tenant": record.tenant, "scopes": record.scopes}


app = create_api_app()
