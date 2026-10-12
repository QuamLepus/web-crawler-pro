"""Redis-backed rate limiting for the API gateway.

Two complementary algorithms, both implemented as **atomic Lua scripts**
(no read-modify-write races across multiple gateway pods):

* :class:`TokenBucket` — sustained ``rate`` tokens/sec with burst capacity.
  Best for per-API-key limits; returns retry-after seconds on denial.
* :class:`SlidingWindowCounter` — exact-window counts (e.g. 1000 crawl
  requests/minute) used for plan-level quotas and abuse protection.

The FastAPI middleware in :mod:`src.gateway.middleware` composes:
    global IP limit -> per-key token bucket -> per-endpoint sliding window
and emits standard ``X-RateLimit-*`` / ``Retry-After`` headers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# refill: now-based token bucket. KEYS[1]=bucket hash
# ARGV: capacity, refill_per_sec, requested, now_ms, ttl_ms
_LUA_BUCKET = """
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local want = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = cap end
if ts == nil then ts = now end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(cap, tokens + elapsed * rate)
local allowed = 0
local retry_ms = 0
if tokens >= want then
  tokens = tokens - want
  allowed = 1
else
  local deficit = want - tokens
  retry_ms = math.ceil(deficit / rate * 1000)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return {allowed, math.floor(tokens), retry_ms}
"""

# sliding window log using a sorted set of request timestamps
_LUA_WINDOW = """
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window_ms)
local count = redis.call('ZCARD', KEYS[1])
local allowed = 0
if count < limit then
  redis.call('ZADD', KEYS[1], now, member)
  redis.call('PEXPIRE', KEYS[1], window_ms)
  allowed = 1
  count = count + 1
end
local reset = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local retry_ms = 0
if allowed == 0 and #reset == 2 then
  retry_ms = tonumber(reset[2]) + window_ms - now
end
return {allowed, count, math.max(0, retry_ms)}
"""


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_s: float
    limit: int

    def headers(self) -> dict[str, str]:
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            h["Retry-After"] = str(max(1, int(round(self.retry_after_s))))
        return h


def _decode(v) -> bytes | str:
    return v


class RateLimiter:
    """Facade over the two Redis scripts with duck-typed client (fakeredis ok)."""

    def __init__(self, redis_client) -> None:
        self.r = redis_client
        self._bucket = redis_client.register_script(_LUA_BUCKET)
        self._window = redis_client.register_script(_LUA_WINDOW)

    # ------------------------------------------------------------------ #
    def token_bucket(self, key: str, *, capacity: int, refill_per_sec: float,
                     cost: int = 1, ttl_s: int = 3600) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        allowed, tokens, retry_ms = self._bucket(
            keys=[f"rl:bucket:{key}"],
            args=[capacity, refill_per_sec, cost, now_ms, ttl_s * 1000],
        )
        return RateLimitResult(
            allowed=bool(int(allowed)), remaining=int(tokens),
            retry_after_s=int(retry_ms) / 1000.0, limit=capacity,
        )

    def sliding_window(self, key: str, *, limit: int, window_s: int,
                       member: str | None = None) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        member = member or f"{now_ms}-{id(object())}"
        allowed, count, retry_ms = self._window(
            keys=[f"rl:window:{key}"],
            args=[now_ms, window_s * 1000, limit, member],
        )
        return RateLimitResult(
            allowed=bool(int(allowed)), remaining=limit - int(count),
            retry_after_s=int(retry_ms) / 1000.0, limit=limit,
        )

    # ------------------------------------------------------------------ #
    def reset(self, key: str) -> None:
        self.r.delete(f"rl:bucket:{key}", f"rl:window:{key}")
