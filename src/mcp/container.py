"""Adapters that let the MCP server talk to the platform.

Two entry points:

* :func:`container_from_env` — build the whole platform from environment
  variables (used by ``python -m src.mcp.server`` as a standalone process).
* :class:`PlatformMcpAdapter` — wrap an already-running
  :class:`~src.services.PlatformServices` (used by the HTTP bridge at
  ``POST /v1/mcp``), so an MCP client and the console share one Redis, one
  index and one budget ledger.

An MCP server must never be a second, subtly different implementation of the
product. Both paths end in the same service methods.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

logger = logging.getLogger("wcp.mcp.container")


class PlatformMcpAdapter:
    """The exact surface :func:`src.mcp.server.build_tools` expects."""

    def __init__(self, platform: Any, *, tenant: str = "public") -> None:
        self.platform = platform
        self.tenant = tenant

    def own_search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        result = self.platform.search(query, limit=limit)
        hits = result.get("hits") or []
        if not hits:
            hits = result.get("corpus_fallback") or []
        return hits

    def ask(self, question: str, *, budget: dict[str, Any] | None = None,
            mode: str = "auto") -> dict[str, Any]:
        return self.platform.ask(question, budget=budget, mode=mode,
                                 tenant=self.tenant)

    def ask_plan(self, question: str) -> dict[str, Any]:
        return self.platform.ask_plan(question)

    def domain_score(self, host: str) -> dict[str, Any]:
        return self.platform.domain_score(host, tenant=self.tenant)

    def watch_once(self, url: str, kinds: Sequence[str] | None = None
                   ) -> dict[str, Any]:
        return self.platform.watch_once(url, kinds=kinds, tenant=self.tenant)

    def provenance_manifest(self, dataset: str, version: str | None = None
                            ) -> dict[str, Any]:
        return self.platform.provenance_manifest(dataset, tenant=self.tenant,
                                                 version=version or "")

    def cost_estimate(self, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.platform.cost_estimate(spec)

    def recipes_list(self) -> list[dict[str, Any]]:
        return self.platform.recipes_list()


def container_from_env() -> PlatformMcpAdapter:  # pragma: no cover - process glue
    """Standalone MCP process: connect to Redis and wire the platform."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        client.ping()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis unavailable (%s) — MCP falls back to in-process memory",
                       exc)
        import fakeredis

        client = fakeredis.FakeStrictRedis(decode_responses=True)
    from src.services import PlatformServices

    return PlatformMcpAdapter(PlatformServices(client),
                              tenant=os.getenv("WCP_MCP_TENANT", "public"))
