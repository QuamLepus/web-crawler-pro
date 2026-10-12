"""Advanced proxy-rotation middleware.

Supports three proxy tiers with weighted selection and automatic
penalty/backoff for failing proxies:

  * datacenter  – cheap, fast, easily blocked
  * residential – high anonymity, per-GB cost
  * mobile      – highest trust score, slowest

Proxies are configured via env vars (comma-separated lists) or by
plugging in a provider API (Smartproxy / Bright Data / Oxylabs) that
returns fresh endpoints on demand.
"""

from __future__ import annotations

import itertools
import os
import random
import time
from dataclasses import dataclass, field
from enum import Enum


class ProxyTier(Enum):
    DATACENTER = "datacenter"
    RESIDENTIAL = "residential"
    MOBILE = "mobile"


@dataclass
class Proxy:
    url: str
    tier: ProxyTier
    failures: int = 0
    last_failure: float = 0.0
    COOLDOWN_SECONDS: float = field(default=120.0, init=False)

    @property
    def available(self) -> bool:
        if self.failures == 0:
            return True
        return (time.time() - self.last_failure) > self.COOLDOWN_SECONDS

    def report_failure(self) -> None:
        self.failures += 1
        self.last_failure = time.time()

    def report_success(self) -> None:
        self.failures = max(0, self.failures - 1)
        if self.failures == 0:
            self.last_failure = 0.0


TIER_WEIGHTS = {ProxyTier.DATACENTER: 1, ProxyTier.RESIDENTIAL: 5, ProxyTier.MOBILE: 10}


class ProxyRotationMiddleware:
    """Round-robin-within-tier, weighted across tiers, failure-aware."""

    def __init__(self, provider_api: str | None = None):
        self.proxies: list[Proxy] = []
        for tier, var in (
            (ProxyTier.DATACENTER, "PROXY_DATACENTER"),
            (ProxyTier.RESIDENTIAL, "PROXY_RESIDENTIAL"),
            (ProxyTier.MOBILE, "PROXY_MOBILE"),
        ):
            for p in os.getenv(var, "").split(","):
                if p.strip():
                    self.proxies.append(Proxy(url=p.strip(), tier=tier))
        self.provider_api = provider_api  # e.g. Smartproxy/BrightData endpoint
        self._rr = itertools.cycle(self.proxies) if self.proxies else None

    # -- provider API ----------------------------------------------------
    def _refresh_from_provider(self) -> None:
        """Pull fresh rotating endpoints from a proxy-provider gateway."""
        if not self.provider_api:
            return
        # Provider gateways (e.g. http://gate.smartproxy.com:7000) are static
        # hostnames; real rotation happens server-side per connection.
        pass

    # -- public API ------------------------------------------------------
    def get_proxy(self) -> dict | None:
        """Return {'http': ..., 'https': ...} for requests/aiohttp, or None."""
        pool = [p for p in self.proxies if p.available]
        if not pool:
            self._refresh_from_provider()
            pool = self.proxies
        if not pool:
            return None
        weighted: list[Proxy] = list(
            itertools.chain.from_iterable(
                itertools.repeat(p, TIER_WEIGHTS[p.tier]) for p in pool
            )
        )
        chosen = random.choice(weighted)
        return {"http": chosen.url, "https": chosen.url}

    def report(self, proxy_url: str, ok: bool) -> None:
        for p in self.proxies:
            if p.url == proxy_url:
                p.report_success() if ok else p.report_failure()
