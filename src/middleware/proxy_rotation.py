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

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


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


_TIER_MAP = {
    "datacenter": ProxyTier.DATACENTER,
    "residential": ProxyTier.RESIDENTIAL,
    "mobile": ProxyTier.MOBILE,
}


class ProxyRotationMiddleware:
    """Round-robin-within-tier, weighted across tiers, failure-aware.

    Two pool sources, usable together:

    * Static env-var lists (``PROXY_DATACENTER`` / ``PROXY_RESIDENTIAL`` /
      ``PROXY_MOBILE``).
    * A live **provider adapter** (Bright Data / Oxylabs — see
      :mod:`src.middleware.providers`). Pass ``provider="brightdata"`` or set
      ``PROXY_PROVIDER``; fresh gateway URLs (with sticky-session/country
      targeting and the mobile tier) are minted on demand and added to the
      weighted pool as they are used.
    """

    def __init__(self, provider_api: str | None = None, provider: str | None = None,
                 country: str | None = None, sticky_ttl_min: int | None = None):
        self.proxies: list[Proxy] = []
        for tier, var in (
            (ProxyTier.DATACENTER, "PROXY_DATACENTER"),
            (ProxyTier.RESIDENTIAL, "PROXY_RESIDENTIAL"),
            (ProxyTier.MOBILE, "PROXY_MOBILE"),
        ):
            for p in os.getenv(var, "").split(","):
                if p.strip():
                    self.proxies.append(Proxy(url=p.strip(), tier=tier))
        self.provider_api = provider_api  # legacy hook, kept for compat
        # Live provider adapter (Bright Data / Oxylabs)
        from src.middleware.providers import get_provider

        self.adapter = get_provider(provider)
        self.country = country or os.getenv("PROXY_COUNTRY") or None
        self.sticky_ttl_min = sticky_ttl_min or int(
            os.getenv("PROXY_STICKY_TTL", "0") or 0) or None
        self._seen_provider_urls: set[str] = set()
        self._rr = itertools.cycle(self.proxies) if self.proxies else None

    # -- provider API ----------------------------------------------------
    def _refresh_from_provider(self) -> None:
        """Pull fresh rotating endpoints from a proxy-provider gateway.

        Bright Data / Oxylabs rotate exit IPs server-side per *connection*,
        so each refresh mints new session-tagged gateway URLs (including the
        premium mobile tier) and merges them into the weighted pool.
        """
        if self.provider_api:
            self.provider_api()  # legacy callable hook
        if not self.adapter or not self.adapter.enabled:
            return
        try:
            tiers = ("datacenter", "residential", "mobile")
            for pp in self.adapter.get_proxies(tiers=tiers, count_per_tier=2):
                if pp.url in self._seen_provider_urls:
                    continue
                self._seen_provider_urls.add(pp.url)
                tier = _TIER_MAP.get(pp.tier, ProxyTier.RESIDENTIAL)
                self.proxies.append(Proxy(url=pp.url, tier=tier))
            self._rr = itertools.cycle(self.proxies)
            logger.info("proxy pool refreshed from %s (%d endpoints)",
                        self.adapter.name, len(self.proxies))
        except Exception as exc:  # noqa: BLE001
            logger.warning("provider refresh failed: %s", exc)

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
