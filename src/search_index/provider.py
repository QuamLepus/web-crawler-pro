"""``OwnIndexProvider`` — the drop-in replacement for DuckDuckGo.

This is the whole point of the package: forty lines that satisfy the
``SearchProvider`` protocol from :mod:`src.discovery.providers`, so the
discovery pipeline stops caring where its results come from.

    DISCOVERY_PROVIDERS=duckduckgo      -> today (free, rate-limited, ToS-grey)
    DISCOVERY_PROVIDERS=own             -> our index (free, unlimited, offline)

What changes when you flip that switch:

* no API key, no quota, no per-query cost;
* works with the network unplugged — ``WCP_OFFLINE=1`` still returns results;
* results are *domains*, not pages, and they carry an authority score;
* no third-party terms of service to worry about.

What does not change: discovery still fans the topic out across providers,
still ranks by agreement, still de-duplicates, still crawls what you approve.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from src.search_index.query import SearchIndex, SearchResult

logger = logging.getLogger(__name__)


class OwnIndexProvider:
    """Search the platform's own index. Implements the ``SearchProvider`` protocol."""

    name = "own"

    def __init__(self, index_dir: str | Path | None = None, *,
                 collapse_host: bool | None = None,
                 authority_weight: float | None = None,
                 snippet_width: int | None = None,
                 sources: Sequence[str] | None = None,
                 mmap: bool = True) -> None:
        from src.config import get_config

        cfg = get_config()
        self.index_dir = Path(index_dir or cfg.SEARCH_INDEX_DIR) / "index"
        self.collapse_host = (cfg.SEARCH_INDEX_COLLAPSE_HOST
                              if collapse_host is None else collapse_host)
        self.authority_weight = (cfg.SEARCH_INDEX_AUTHORITY_WEIGHT
                                 if authority_weight is None else authority_weight)
        self.snippet_width = snippet_width or cfg.SEARCH_INDEX_SNIPPET_WIDTH
        self.sources = tuple(sources or ())
        self._mmap = mmap
        self._index: SearchIndex | None = None
        self._unavailable = False

    # -- wiring -------------------------------------------------------------- #
    @property
    def index(self) -> SearchIndex | None:
        """The underlying :class:`SearchIndex`, or ``None`` if it cannot open."""
        if self._index is None and not self._unavailable:
            try:
                self._index = SearchIndex(
                    self.index_dir, mmap=self._mmap,
                    collapse_host=self.collapse_host,
                    authority_weight=self.authority_weight,
                    snippet_width=self.snippet_width)
            except Exception as exc:  # noqa: BLE001 - "no index" is a normal state
                self._unavailable = True
                logger.debug("own index unavailable at %s: %s", self.index_dir, exc)
        return self._index

    @property
    def available(self) -> bool:
        index = self.index
        return bool(index is not None and index.available)

    def stats(self) -> dict:
        index = self.index
        if index is None:
            return {"available": False, "index_dir": str(self.index_dir)}
        payload = index.stats().to_dict()
        payload["available"] = index.available
        return payload

    # -- the protocol -------------------------------------------------------- #
    def search(self, query: str, limit: int = 10) -> list[SearchHit]:  # noqa: F821
        """Return up to ``limit`` :class:`SearchHit` results from our own index."""

        index = self.index
        if index is None or not index.available:
            return []
        try:
            results: list[SearchResult] = index.search(
                query, limit, sources=self.sources or None)
        except Exception as exc:  # noqa: BLE001 - a bad index must not break discovery
            logger.warning("own index search failed for %r: %s", query, exc)
            return []
        return [self._to_hit(result, rank, query) for rank, result in enumerate(results, 1)]

    @staticmethod
    def _to_hit(result: SearchResult, rank: int, query: str) -> SearchHit:  # noqa: F821
        from src.discovery.providers import SearchHit

        doc = result.doc
        return SearchHit(
            url=doc.url,
            title=doc.title or doc.host,
            snippet=result.snippet or doc.display_snippet(),
            provider="own",
            rank=rank,
            query=query,
        )

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None
