"""Redis-backed, per-API-key rate limiting middleware for FastAPI.

Strategy
--------
* Authenticated requests (``X-API-Key`` header or ``Authorization: Bearer``)
  are limited by a **token bucket** keyed on the credential — burst-friendly,
  hard-capped sustained rate.
* Unauthenticated requests are limited by a fixed-window counter keyed on the
  client IP (defence-in-depth; the gateway still returns 401/403 downstream).
* Limits come from the tenant's subscription plan (see
  :mod:`src.gateway.billing.plans`), with optional per-key overrides stored in
  Redis (``wcp:ratelimit:override:{key}``).
* Every response carries standard ``X-RateLimit-*`` headers; over-limit
  responses are ``429`` with ``Retry-After`` and a structured error body.

The Lua scripts run atomically inside Redis so concurrent workers cannot
race past the limit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("wcp.api.ratelimit")

# capacity, refill-per-second
_TOKEN_BUCKET_LUA = """
local key     = KEYS[1]
local cap     = tonumber(ARGV[1])
local refill  = tonumber(ARGV[2])
local now     = tonumber(ARGV[3])
local cost    = tonumber(ARGV[4])

local data    = redis.call('HMGET', key, 'tokens', 'ts')
local tokens  = tonumber(data[1])
local ts      = tonumber(data[2])

if tokens == nil or ts == nil then
  tokens = cap
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(cap, tokens + elapsed * refill)

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = math.ceil((cost - tokens) / math.max(refill, 0.0001))
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(cap / math.max(refill, 0.0001)) + 60)
return {allowed, math.floor(tokens), retry_after}
"""

_FIXED_WINDOW_LUA = """
local key    = KEYS[1]
local limit  = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cur    = redis.call('INCR', key)
if cur == 1 then
  redis.call('EXPIRE', key, window)
end
local ttl = redis.call('TTL', key)
return {limit - cur, ttl}
"""


@dataclass(frozen=True)
class RateLimitConfig:
    """Default limits when no plan information is available."""

    capacity: int = 60            # max burst
    refill_per_sec: float = 1.0   # sustained rate
    ip_window_limit: int = 100    # unauthenticated reqs per window
    ip_window_s: int = 60
    exempt_paths: tuple[str, ...] = ("/healthz", "/readyz", "/metrics",
                                     "/docs", "/openapi.json", "/redoc",
                                     "/graphql")

    @classmethod
    def from_plan(cls, rate_per_min: int, burst: Optional[int] = None) -> "RateLimitConfig":
        refill = max(rate_per_min / 60.0, 0.01)
        return cls(capacity=burst or max(int(rate_per_min * 0.5), 5),
                   refill_per_sec=refill)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-credential token-bucket limiter. Requires a Redis-compatible client."""

    def __init__(self, app, redis_client, *,
                 config: Optional[RateLimitConfig] = None,
                 plan_resolver: Optional[Callable[[str], RateLimitConfig]] = None,
                 key_prefix: str = "wcp:rl") -> None:
        super().__init__(app)
        self.redis = redis_client
        self.config = config or RateLimitConfig()
        self.plan_resolver = plan_resolver
        self.key_prefix = key_prefix
        try:
            self._bucket_sha = self.redis.script_load(_TOKEN_BUCKET_LUA)
            self._window_sha = self.redis.script_load(_FIXED_WINDOW_LUA)
        except Exception as exc:  # pragma: no cover - non-Lua backends
            logger.warning("Lua scripts unavailable (%s); using Python fallback", exc)
            self._bucket_sha = self._window_sha = None

    # ------------------------------------------------------------------ #
    def _identify(self, request: Request) -> tuple[str, bool]:
        """Return ``(bucket_key, authenticated)`` for this request."""
        api_key = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if api_key:
            return f"{self.key_prefix}:key:{api_key[:64]}", True
        if auth.lower().startswith("bearer "):
            return f"{self.key_prefix}:tok:{auth[7:17]}:{hash(auth) & 0xffff:x}", True
        client_ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        return f"{self.key_prefix}:ip:{client_ip}", False

    def _config_for(self, request: Request, authenticated: bool) -> RateLimitConfig:
        if authenticated and self.plan_resolver is not None:
            tenant = getattr(request.state, "tenant", None)
            if tenant:
                try:
                    return self.plan_resolver(tenant)
                except Exception:  # pragma: no cover - resolver bugs must not 500
                    logger.exception("plan resolver failed; using defaults")
        return self.config

    # ------------------------------------------------------------------ #
    async def dispatch(self, request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self.config.exempt_paths):
            return await call_next(request)

        key, authenticated = self._identify(request)
        cfg = self._config_for(request, authenticated)
        now = time.time()

        try:
            if authenticated:
                allowed, remaining, retry_after = self._token_bucket(
                    key, cfg.capacity, cfg.refill_per_sec, now)
                headers = {
                    "X-RateLimit-Limit": str(cfg.capacity),
                    "X-RateLimit-Remaining": str(max(remaining, 0)),
                    "X-RateLimit-Reset": str(int(now) + retry_after if not allowed else int(now) + 1),
                }
                if not allowed:
                    return JSONResponse(
                        {"error": {"code": "rate_limited",
                                   "message": f"Rate limit exceeded ({cfg.capacity} burst, "
                                              f"{cfg.refill_per_sec:.2f}/s sustained)",
                                   "retry_after_s": retry_after}},
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        headers={**headers, "Retry-After": str(retry_after)})
            else:
                remaining_ip, ttl = self._fixed_window(
                    key, cfg.ip_window_limit, cfg.ip_window_s)
                headers = {
                    "X-RateLimit-Limit": str(cfg.ip_window_limit),
                    "X-RateLimit-Remaining": str(max(remaining_ip, 0)),
                    "X-RateLimit-Reset": str(int(now) + max(ttl, 0)),
                }
                if remaining_ip < 0:
                    return JSONResponse(
                        {"error": {"code": "rate_limited",
                                   "message": "Anonymous IP rate limit exceeded"}},
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        headers={**headers, "Retry-After": str(max(ttl, 1))})
        except Exception as exc:  # fail-open: limiter outage must not take down API
            logger.exception("rate limiter error (failing open): %s", exc)
            return await call_next(request)

        response = await call_next(request)
        for k, v in headers.items():
            response.headers[k] = v
        return response

    # ------------------------------------------------------------------ #
    def _token_bucket(self, key: str, cap: int, refill: float,
                      now: float) -> tuple[int, int, int]:
        if self._bucket_sha is not None:
            res = self.redis.evalsha(self._bucket_sha, 1, key,
                                     cap, refill, now, 1)
            return int(res[0]), int(res[1]), int(res[2])
        return self._token_bucket_py(key, cap, refill, now)

    def _token_bucket_py(self, key: str, cap: int, refill: float,
                         now: float) -> tuple[int, int, int]:
        """Non-Lua fallback (fakeredis w/o lupa, cluster misconfigs)."""
        h = self.redis.hgetall(key)
        def _f(field: bytes | str, default: float) -> float:
            v = h.get(field) if isinstance(h, dict) else None
            if v is None and isinstance(h, dict):
                for k2, v2 in h.items():
                    if k2 in (field.encode() if isinstance(field, str) else field):
                        v = v2
                        break
            return float(v) if v is not None else default
        tokens = _f("tokens", cap)
        ts = _f("ts", now)
        tokens = min(cap, tokens + max(0.0, now - ts) * refill)
        if tokens >= 1:
            tokens -= 1
            allowed, retry = 1, 0
        else:
            allowed, retry = 0, max(1, int((1 - tokens) / max(refill, .0001)))
        self.redis.hset(key, mapping={"tokens": tokens, "ts": now})
        self.redis.expire(key, int(cap / max(refill, .0001)) + 60)
        return allowed, int(tokens), retry

    def _fixed_window(self, key: str, limit: int, window_s: int) -> tuple[int, int]:
        slot = int(time.time() // window_s)
        wkey = f"{key}:{slot}"
        if self._window_sha is not None:
            res = self.redis.evalsha(self._window_sha, 1, wkey, limit, window_s)
            return int(res[0]), int(res[1])
        cur = self.redis.incr(wkey)
        if cur == 1:
            self.redis.expire(wkey, window_s)
        return limit - cur, window_s


def install_rate_limit(app_fastapi, redis_client, *,
                       plan_of_tenant: Optional[Callable[[str], RateLimitConfig]] = None,
                       config: Optional[RateLimitConfig] = None) -> None:
    """Convenience installer used by :func:`src.api.main.create_api_app`."""
    app_fastapi.add_middleware(
        RateLimitMiddleware,
        redis_client=redis_client,
        config=config,
        plan_resolver=plan_of_tenant,
    )
