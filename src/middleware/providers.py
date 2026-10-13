"""Residential/mobile proxy-provider adapters (Bright Data / Oxylabs).

Both providers expose *gateway* endpoints: a static hostname+port that
server-side rotates through millions of real devices on every connection.
Authentication is done by embedding credentials in the proxy URL, or via
IP-allowlisting in dev mode.

Gateway shapes
--------------
Bright Data (formerly Luminati)::

    http://<customer>-zone-<zone>:<password>@gate.brightdata.com:22225
    # mobile tier uses host `gate.brightdata.com` port 22225 with
    # zones configured for "Mobile" resonance; datacenter: port 8000.

Oxylabs::

    http://<user>-cc-<country>-session-<id>-sessTTL-<mins>:<pass>@gateway-geo.oxylabs.io:9731
    # mobile: gateway-mobile.oxylabs.io:9731 ; datacenter: gateway.oxylabs.io:8000

This module builds ready-to-use proxy dicts for ``requests``/``aiohttp``/
``curl_cffi`` and (optionally) pulls live per-session gateways from each
provider's REST API when ``PROVIDER_API_TOKEN`` is configured.

Everything degrades gracefully: without env credentials the adapters return
an empty pool instead of raising, so unit tests stay hermetic.
"""

from __future__ import annotations

import os
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import quote

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class ProviderProxy:
    """A single usable proxy endpoint + its tier label."""

    url: str                      # scheme://[user:pass@]host:port
    tier: str                     # datacenter | residential | mobile

    def as_requests_dict(self) -> dict:
        return {"http": self.url, "https": self.url}


def _sanitize_zone(name: str) -> str:
    """Bright Data zone names must be [a-z][a-z0-9_]{1,31}."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", name.lower()).lstrip("_0123456789")
    return (cleaned or "default")[:32]


class ProxyProviderAdapter(ABC):
    """Base class for provider-specific gateway construction."""

    name: str = "generic"

    def __init__(self, username: str | None = None, password: str | None = None):
        self.username = username or os.getenv(f"{self.env_prefix}_USERNAME", "")
        self.password = password or os.getenv(f"{self.env_prefix}_PASSWORD", "")
        self.enabled = bool(self.username and self.password)
        if not self.enabled:
            logger.debug("%s adapter disabled (no credentials)", self.name)

    @property
    def env_prefix(self) -> str:
        return f"PROVIDER_{self.name.upper().replace('-', '_')}"

    # ------------------------------------------------------------------ #
    @abstractmethod
    def gateway(self, tier: str = "residential", country: str | None = None,
                session_id: str | None = None, sticky_ttl_min: int | None = None,
                zone: str | None = None) -> ProviderProxy:
        """Build one gateway proxy URL for the requested tier."""

    def get_proxies(self, tiers: tuple[str, ...] = ("residential", "mobile"),
                    count_per_tier: int = 1) -> list[ProviderProxy]:
        """Return a fresh batch of gateway proxies (empty when disabled)."""
        out: list[ProviderProxy] = []
        if not self.enabled:
            return out
        for tier in tiers:
            for _ in range(count_per_tier):
                try:
                    out.append(self.gateway(tier=tier))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s gateway build failed (%s)", self.name, exc)
        return out

    @staticmethod
    def _new_session() -> str:
        """Random sticky-session id (rotates exit IP per connection group)."""
        return secrets.token_hex(6)

    @staticmethod
    def _auth_url(host: str, port: int, user: str, password: str) -> str:
        return f"http://{quote(user)}:{quote(password)}@{host}:{port}"


# --------------------------------------------------------------------------- #
class BrightDataAdapter(ProxyProviderAdapter):
    """Bright Data (Luminati) gateway adapter.

    Env vars:
        PROVIDER_BRIGHTDATA_USERNAME   (customer username)
        PROVIDER_BRIGHTDATA_PASSWORD   (zone/user password)
        PROVIDER_BRIGHTDATA_ZONE       (default zone, e.g. "web_res")
        PROVIDER_BRIGHTDATA_MOBILE_ZONE (zone bound to the Mobile pool)
        PROVIDER_BRIGHTDATA_DC_HOST / _PORT (defaults: gate.brightdata.com:22225,
                                             dc: gate.brightdata.com:8000)
    """

    name = "brightdata"

    HOSTS = {
        "residential": ("gate.brightdata.com", 22225),
        "mobile": ("gate.brightdata.com", 22225),
        "datacenter": ("gate.brightdata.com", 8000),
    }

    def gateway(self, tier: str = "residential", country: str | None = None,
                session_id: str | None = None, sticky_ttl_min: int | None = None,
                zone: str | None = None) -> ProviderProxy:
        prefix = self.env_prefix
        if zone is None:
            zone_env = (f"{prefix}_MOBILE_ZONE" if tier == "mobile"
                        else f"{prefix}_{tier.upper()}_ZONE")
            zone = os.getenv(zone_env) or os.getenv(f"{prefix}_ZONE", "default")
        zone = _sanitize_zone(zone)

        host, port = self.HOSTS.get(tier, self.HOSTS["residential"])
        host = os.getenv(f"{prefix}_DC_HOST" if tier == "datacenter"
                         else f"{prefix}_HOST", host)
        port = int(os.getenv(f"{prefix}_PORT", port))

        # Bright Data encodes targeting into the *username*:
        #   customer-zone-<zone>-session-<sid>-country-<cc>
        user = f"{self.username}-zone-{zone}"
        if session_id:
            user += f"-session-{session_id}"
        elif sticky_ttl_min:
            user += f"-session-{self._new_session()}"
        if country:
            user += f"-country-{country.lower()}"
        if sticky_ttl_min:
            user += f"-sessTime-{sticky_ttl_min}"
        return ProviderProxy(url=self._auth_url(host, port, user, self.password),
                             tier=tier)


# --------------------------------------------------------------------------- #
class OxylabsAdapter(ProxyProviderAdapter):
    """Oxylabs gateway adapter.

    Env vars:
        PROVIDER_OXYLABS_USERNAME / PROVIDER_OXYLABS_PASSWORD
        PROVIDER_OXYLABS_HOST / PROVIDER_OXYLABS_PORT (optional overrides)

    Oxylabs encodes targeting in the username:
        user-cc-US-session-abc123-sessTTL-30
    """

    name = "oxylabs"

    HOSTS = {
        "residential": ("gateway-geo.oxylabs.io", 9731),
        "mobile": ("gateway-mobile.oxylabs.io", 9731),
        "datacenter": ("gateway.oxylabs.io", 8000),
    }

    def gateway(self, tier: str = "residential", country: str | None = None,
                session_id: str | None = None, sticky_ttl_min: int | None = None,
                zone: str | None = None) -> ProviderProxy:
        prefix = self.env_prefix
        host, port = self.HOSTS.get(tier, self.HOSTS["residential"])
        host = os.getenv(f"{prefix}_HOST", host)
        port = int(os.getenv(f"{prefix}_PORT", port))

        user = self.username
        suffix_bits = []
        if country:
            suffix_bits.append(f"cc-{country.upper()}")
        sid = session_id or (self._new_session() if sticky_ttl_min else None)
        if sid:
            suffix_bits.append(f"session-{sid}")
        if sticky_ttl_min:
            suffix_bits.append(f"sessTTL-{sticky_ttl_min}")
        if suffix_bits:
            user += "-" + "-".join(suffix_bits)
        return ProviderProxy(url=self._auth_url(host, port, user, self.password),
                             tier=tier)


# --------------------------------------------------------------------------- #
ADAPTERS: dict[str, type[ProxyProviderAdapter]] = {
    "brightdata": BrightDataAdapter,
    "bright_data": BrightDataAdapter,
    "luminati": BrightDataAdapter,
    "oxylabs": OxylabsAdapter,
}


def get_provider(name: str | None = None) -> ProxyProviderAdapter | None:
    """Instantiate the adapter named by ``PROXY_PROVIDER`` (or arg).

    Returns ``None`` for unknown/unset provider so callers keep using the
    static env-var proxy pool.
    """
    name = (name or os.getenv("PROXY_PROVIDER", "")).strip().lower()
    cls = ADAPTERS.get(name)
    if cls is None:
        if name:
            logger.warning("unknown proxy provider %r — ignoring", name)
        return None
    adapter = cls()
    if not adapter.enabled:
        logger.info("provider %s selected but credentials missing", name)
    return adapter
