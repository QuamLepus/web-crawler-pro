"""Redis-backed URL Frontier.

A production frontier needs four properties:
  1. Deduplication   -> Redis SET (`seen:{domain}`) + optional Bloom filter
  2. Prioritisation  -> Redis ZSET scored by priority (BFS depth decay)
  3. Politeness      -> per-domain delay enforcement (next-allowed timestamp)
  4. Crash safety    -> in-flight URLs kept in a `processing` ZSET until acked

All operations are done with atomic Lua scripts where needed so multiple
worker nodes can pop from the frontier concurrently without races.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from urllib.parse import urldefrag, urlparse

import redis

# Atomically pop up to `batch` ready URLs while honoring per-domain politeness.
# KEYS: pending(ZSET) processing(ZSET) politeness(HASH)
# ARGV: now, batch, visibility_timeout, politeness_delay
_POP_SCRIPT = """
local popped = {}
local now = tonumber(ARGV[1])
local batch = tonumber(ARGV[2])
local vis = tonumber(ARGV[3])
local delay = tonumber(ARGV[4])
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', '+inf',
                              'WITHSCORES', 'LIMIT', 0, batch * 20)
local checked = 0
for i = 1, #candidates, 2 do
  local url = candidates[i]
  local score = tonumber(candidates[i + 1])
  -- priority tiers: only take low-priority items when nothing better exists
  if score < -5 and #popped > 0 then break end
  local domain = string.match(url, '^[%a][%w+.-]*://([^/]+)') or 'unknown'
  local next_ok = tonumber(redis.call('HGET', KEYS[3], domain) or '0')
  if next_ok <= now then
    redis.call('ZREM', KEYS[1], url)
    redis.call('ZADD', KEYS[2], now + vis, url)
    redis.call('HSET', KEYS[3], domain, now + delay)
    table.insert(popped, url)
    if #popped >= batch then break end
  end
  checked = checked + 1
  if checked >= batch * 20 then break end
end
return popped
"""


@dataclass
class CrawlURL:
    url: str
    priority: float
    depth: int = 0
    parent: str = ""


class URLFrontier:
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        namespace: str = "frontier",
        politeness_delay: float = 1.0,
        max_depth: int = 10,
    ):
        self.r = redis.from_url(redis_url, decode_responses=True)
        self.ns = namespace
        self.politeness_delay = politeness_delay
        self.max_depth = max_depth
        self._pop_sha: str | None = None

    # --- key helpers -----------------------------------------------------
    def _k(self, name: str, domain: str | None = None) -> str:
        if domain:
            return f"{self.ns}:{name}:{domain}"
        return f"{self.ns}:{name}"

    @staticmethod
    def normalize(url: str) -> str:
        """Strip fragment, trailing slash, lowercase host."""
        url, _ = urldefrag(url)
        p = urlparse(url)
        path = p.path.rstrip("/") or "/"
        return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(),
                          path=path, query=p.query).geturl()

    @staticmethod
    def _domain(url: str) -> str:
        return urlparse(url).netloc.lower()

    # --- write side ------------------------------------------------------
    def add(self, url: str, priority: float = 0.0, depth: int = 0,
            parent: str = "") -> bool:
        """Add URL if never seen and within depth budget. Returns True if enqueued."""
        if depth > self.max_depth:
            return False
        norm = self.normalize(url)
        if not norm.startswith(("http://", "https://")):
            return False
        domain = self._domain(norm)
        seen_key = self._k("seen", domain)
        # SADD returns 0 when already present -> dedupe is atomic
        if self.r.sadd(seen_key, norm) == 0:
            return False
        self.r.zadd(self._k("pending"), {norm: priority}, nx=True)
        return True

    def seed(self, urls: list[str]) -> int:
        return sum(1 for u in urls if self.add(u, priority=1000.0))

    # --- read side -------------------------------------------------------
    def _run_pop(self, batch: int) -> list[str]:
        now = time.time()
        keys = (self._k("pending"), self._k("processing"), self._k("politeness"))
        args = (now, batch, 600, self.politeness_delay)
        if self._pop_sha is None:
            self._pop_sha = self.r.script_load(_POP_SCRIPT)
        try:
            return self.r.evalsha(self._pop_sha, len(keys), *keys, *args) or []
        except redis.exceptions.NoScriptError:
            self._pop_sha = self.r.script_load(_POP_SCRIPT)
            return self.r.evalsha(self._pop_sha, len(keys), *keys, *args) or []

    def pop(self, batch: int = 1) -> list[CrawlURL]:
        """Atomically fetch up to `batch` ready URLs honoring politeness."""
        return [CrawlURL(url=u, priority=self.r.zscore(self._k("pending"), u) or 0.0)
                for u in self._run_pop(batch)]

    def ack(self, url: str) -> None:
        self.r.zrem(self._k("processing"), self.normalize(url))

    def nack(self, url: str, new_priority: float = -10.0) -> None:
        """Return a failed URL to pending with lower priority."""
        norm = self.normalize(url)
        self.r.zrem(self._k("processing"), norm)
        self.r.zadd(self._k("pending"), {norm: new_priority})

    def recover_stale(self) -> int:
        """Re-enqueue in-flight URLs whose visibility timeout expired (crashed workers)."""
        now = time.time()
        stale = self.r.zrangebyscore(self._k("processing"), "-inf", now)
        for u in stale:
            self.nack(u)
        return len(stale)

    # --- stats -----------------------------------------------------------
    def stats(self) -> dict:
        return {
            "pending": self.r.zcard(self._k("pending")),
            "in_flight": self.r.zcard(self._k("processing")),
            "seen": self.r.scard(self._k("seen")) or self._total_seen(),
        }

    def _total_seen(self) -> int:
        total = 0
        for key in self.r.scan_iter(match=f"{self.ns}:seen:*", count=1000):
            total += self.r.scard(key)
        return total

    def fingerprint(self, text: str) -> str:
        """Content fingerprint helper (used by dedupe stage downstream)."""
        return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]
