"""Quota management: monthly meters + enforcement for requests/storage/tokens.

Redis data model (all keys tenant-scoped, month-partitioned so counters roll
over naturally and old months remain queryable for invoicing):

    quota:{tenant}:{YYYY-MM}:{metric}   -> float counter (INCRBYFLOAT)
    quota:limit:{tenant}:{metric}       -> override limit set by admin
    storage:bytes:{tenant}              -> live storage gauge

``QuotaService.check_and_consume`` is atomic (Lua script) so concurrent
workers cannot race past a limit.  The scheduler in
:mod:`src.compute.scheduler` uses the same metric names, keeping billing and
job admission consistent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.gateway.billing.plans import Plan, get_plan

logger = logging.getLogger(__name__)

METRIC_REQUESTS = "requests"
METRIC_TOKENS = "tokens_processed"
METRIC_STORAGE_GB = "storage_gb"

# Atomic check-and-increment; returns {allowed(0/1), used_after, limit}
_LUA_CONSUME = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if limit < 0 then
  redis.call('INCRBYFLOAT', KEYS[1], amount)
  return {1, used + amount, -1}
end
if used + amount > limit then
  return {0, used, limit}
end
redis.call('INCRBYFLOAT', KEYS[1], amount)
return {1, used + amount, limit}
"""


def current_period(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


@dataclass
class QuotaStatus:
    metric: str
    period: str
    used: float
    limit: float | None          # None => unlimited
    remaining: float | None

    @property
    def pct(self) -> float | None:
        if self.limit in (None, 0):
            return None
        return round(100.0 * self.used / self.limit, 2)


class QuotaExceededError(Exception):
    """Raised when an operation would exceed a tenant's plan quota."""

    def __init__(self, tenant: str, status: QuotaStatus) -> None:
        self.tenant = tenant
        self.status = status
        super().__init__(
            f"tenant '{tenant}' exceeded {status.metric}: "
            f"{status.used:,.0f}/{status.limit:,.0f} for {status.period}"
        )


class QuotaService:
    """Enforce + meter quotas against the tenant's :class:`Plan`."""

    def __init__(self, redis_client, plan_resolver=None) -> None:
        """``plan_resolver(tenant_id) -> Plan``; defaults to free plan."""
        self.r = redis_client
        self._resolver = plan_resolver or (lambda _t: get_plan("free"))
        try:
            self._script = self.r.register_script(_LUA_CONSUME)
        except AttributeError:  # pragma: no cover - exotic clients
            self._script = None

    # ------------------------------------------------------------------ #
    # limits
    # ------------------------------------------------------------------ #
    def _key(self, tenant: str, metric: str, period: str | None = None) -> str:
        return f"quota:{tenant}:{period or current_period()}:{metric}"

    def limit_for(self, tenant: str, metric: str) -> float | None:
        """Admin override wins over plan default; -1 sentinel == unlimited."""
        override = self.r.get(f"quota:limit:{tenant}:{metric}")
        if override is not None:
            val = float(override)
            return None if val < 0 else val
        plan = self._resolver(tenant)
        q = plan.quotas
        mapping = {
            METRIC_REQUESTS: q.requests_per_month,
            METRIC_TOKENS: q.tokens_processed_per_month,
            METRIC_STORAGE_GB: q.storage_gb,
        }
        return mapping.get(metric)

    def set_override(self, tenant: str, metric: str, limit: float | None) -> None:
        if limit is None:
            self.r.set(f"quota:limit:{tenant}:{metric}", -1)
        else:
            self.r.set(f"quota:limit:{tenant}:{metric}", limit)

    # ------------------------------------------------------------------ #
    # metering
    # ------------------------------------------------------------------ #
    def consume(self, tenant: str, metric: str, amount: float = 1,
                *, enforce: bool = True) -> QuotaStatus:
        """Atomically add ``amount`` to the monthly meter.

        With ``enforce=True`` raises :class:`QuotaExceededError` (and does NOT
        increment) when the limit would be crossed — callers should treat that
        as HTTP 402/429.  With ``enforce=False`` meters unconditionally
        (used for post-hoc billing of overages on paid plans).
        """
        period = current_period()
        limit = self.limit_for(tenant, metric)
        key = self._key(tenant, metric, period)
        eff_limit = -1 if limit is None else limit
        if self._script is not None:
            allowed, used, lim = self._script(keys=[key], args=[amount, eff_limit])
            allowed = int(allowed)
            used = float(used)
        else:  # fallback: non-atomic but correct single-threaded
            used = float(self.r.get(key) or 0)
            if eff_limit >= 0 and enforce and used + amount > eff_limit:
                raise QuotaExceededError(tenant, self._status(metric, period, used, limit))
            self.r.incrbyfloat(key, amount)
            used += amount
        if not allowed and enforce:
            raise QuotaExceededError(tenant, self._status(metric, period, used, limit))
        st = self._status(metric, period, used, limit)
        self.r.expire(key, 60 * 86400 * 40)  # keep ~40 days for invoicing
        return st

    def refund(self, tenant: str, metric: str, amount: float) -> None:
        """Give quota back when a job fails before doing work."""
        self.r.decrbyfloat(self._key(tenant, metric), amount)

    def _status(self, metric: str, period: str, used: float,
                limit: float | None) -> QuotaStatus:
        remaining = None if limit is None else max(0.0, limit - used)
        return QuotaStatus(metric=metric, period=period, used=used,
                           limit=limit, remaining=remaining)

    def usage(self, tenant: str, metric: str,
              period: str | None = None) -> QuotaStatus:
        period = period or current_period()
        used = float(self.r.get(self._key(tenant, metric, period)) or 0)
        return self._status(metric, period, used, self.limit_for(tenant, metric))

    def snapshot(self, tenant: str) -> dict[str, QuotaStatus]:
        return {m: self.usage(tenant, m)
                for m in (METRIC_REQUESTS, METRIC_TOKENS, METRIC_STORAGE_GB)}

    # ------------------------------------------------------------------ #
    # storage gauge (live bytes, not month-partitioned)
    # ------------------------------------------------------------------ #
    def add_storage_bytes(self, tenant: str, nbytes: int) -> None:
        self.r.incrby(f"storage:bytes:{tenant}", nbytes)

    def remove_storage_bytes(self, tenant: str, nbytes: int) -> None:
        self.r.decrby(f"storage:bytes:{tenant}", nbytes)

    def storage_gb(self, tenant: str) -> float:
        raw = self.r.get(f"storage:bytes:{tenant}")
        return (int(raw) if raw else 0) / (1024 ** 3)

    def check_storage(self, tenant: str, add_gb: float) -> None:
        limit = self.limit_for(tenant, METRIC_STORAGE_GB)
        if limit is not None and self.storage_gb(tenant) + add_gb > limit:
            raise QuotaExceededError(
                tenant,
                self._status(METRIC_STORAGE_GB, current_period(),
                             self.storage_gb(tenant), limit))

    # ------------------------------------------------------------------ #
    # invoicing helpers
    # ------------------------------------------------------------------ #
    def month_totals(self, tenant: str, period: str) -> dict[str, float]:
        out: dict[str, float] = {"_tenant": tenant}
        for m in (METRIC_REQUESTS, METRIC_TOKENS):
            out[m] = float(self.r.get(self._key(tenant, m, period)) or 0)
        return out
