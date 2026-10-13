"""Stealth Engine package exports (see module docstrings for details)."""

from src.middleware.capsolver import CapSolverClient, CapSolverError, Solution
from src.middleware.fingerprinting import (Fingerprint, make_fingerprint,
                                           playwright_context_kwargs, stealth_js)
from src.middleware.providers import (BrightDataAdapter, OxylabsAdapter,
                                      ProviderProxy, get_provider)
from src.middleware.proxy_rotation import ProxyRotationMiddleware, ProxyTier
from src.middleware.stealth_middleware import PageResult, StealthFetcher
from src.middleware.success_monitor import (Outcome, StrategyMonitor, classify,
                                            get_monitor, reset_monitor)

__all__ = [
    "BrightDataAdapter",
    "CapSolverClient",
    "CapSolverError",
    "Fingerprint",
    "OxylabsAdapter",
    "Outcome",
    "PageResult",
    "ProviderProxy",
    "ProxyRotationMiddleware",
    "ProxyTier",
    "Solution",
    "StealthFetcher",
    "StrategyMonitor",
    "classify",
    "get_monitor",
    "get_provider",
    "make_fingerprint",
    "playwright_context_kwargs",
    "reset_monitor",
    "stealth_js",
]
