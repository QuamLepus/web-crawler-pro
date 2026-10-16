"""The read side: ``SearchIndex`` — query an index built by :mod:`src.search_index.indexer`.

Query pipeline, in order:

1. **Parse** — ``"solar panel" -blog site:.edu source:wikipedia``.
2. **Retrieve** — BM25 across every shard, top-k per shard, merged. We ask each
   shard for ``oversample × limit`` candidates because the next steps prune.
3. **Verify** — phrases and exclusions are checked against the real document
   (see :mod:`src.search_index.bm25` for why they are not indexed).
4. **Blend** — ``score = bm25 × (1 + authority_weight × authority/10)``. This is
   the step that turns a text-match engine into something that behaves: with
   authority weighting, "python" returns python.org instead of a scraper farm.
5. **Collapse** — one result per host, because discovery wants *websites*, not
   pages. ``collapse_host=False`` gives raw page results.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from src.search_index.bm25 import ShardedBM25
from src.search_index.models import Doc, IndexStats
from src.search_index.store import DocStore
from src.search_index.text import ParsedQuery, analyze, highlight, parse_query

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchResult:
    """One hit, with the pieces that explain *why* it matched."""

    doc: Doc
    score: float = 0.0
    bm25: float = 0.0
    snippet: str = ""

    def to_dict(self) -> dict:
        payload = self.doc.to_dict()
        payload.update({"score": round(self.score, 5),
                        "bm25": round(self.bm25, 5),
                        "snippet": self.snippet})
        return payload


def _host_matches(host: str, wanted: str) -> bool:
    """``site:.edu`` -> suffix match; ``site:example.org`` -> exact or subdomain."""
    host = (host or "").lower()
    wanted = (wanted or "").lower().lstrip(".")
    if not host or not wanted:
        return False
    if wanted.startswith("."):
        return host.endswith(wanted)
    return host == wanted or host.endswith("." + wanted)


class SearchIndex:
    """Read-only facade over the sharded index + document store."""

    def __init__(self, index_dir: str | Path, *, mmap: bool = True,
                 max_loaded_shards: int = 8,
                 snippet_width: int = 240,
                 authority_weight: float = 0.35,
                 collapse_host: bool = True,
                 oversample: int = 4) -> None:
        self.index_dir = Path(index_dir)
        self.snippet_width = int(snippet_width)
        self.authority_weight = float(authority_weight)
        self.collapse_host = bool(collapse_host)
        self.oversample = max(1, int(oversample))
        self._bm25 = ShardedBM25(self.index_dir, mmap=mmap,
                                 max_loaded_shards=max_loaded_shards)
        self._store = DocStore(self.index_dir.parent / "docs.sqlite3",
                               readonly=True)

    # -- lifecycle ----------------------------------------------------------- #
    @classmethod
    def open(cls, index_dir: str | Path, **kwargs) -> SearchIndex:
        return cls(index_dir, **kwargs)

    @property
    def available(self) -> bool:
        return self._bm25.shard_count > 0

    def reload(self) -> int:
        return self._bm25.reload()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- inspection ---------------------------------------------------------- #
    def stats(self) -> IndexStats:
        from src.search_index.indexer import STATE_FILE

        state: dict = {}
        path = self.index_dir.parent / STATE_FILE
        if path.exists():
            import json

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                state = payload.get("sources", {}) if isinstance(payload, dict) else {}
            except (OSError, ValueError):
                state = {}
        info = self._bm25.stats()
        return IndexStats(
            index_dir=str(self.index_dir.parent),
            level=0,
            docs=info["docs"],
            shards=info["shards"],
            terms=info["terms"],
            sources=dict(info["sources"]),
            state=state,
            exists=True,
            ready=info["shards"] > 0,
        )

    # -- search -------------------------------------------------------------- #
    def _verify(self, doc: Doc, parsed: ParsedQuery, text: str) -> bool:
        if parsed.source and doc.source.lower() != parsed.source:
            return False
        if parsed.site and not _host_matches(doc.host, parsed.site):
            return False
        if parsed.excludes and (set(parsed.excludes) & set(analyze(text))):
            return False
        if parsed.phrases:
            tokens = analyze(text)
            joined = " ".join(tokens)
            for phrase in parsed.phrases:
                if " ".join(phrase) not in joined:
                    return False
        return True

    def search(self, query: str, limit: int = 10, *,
               sources: Sequence[str] | None = None,
               collapse_host: bool | None = None) -> list[SearchResult]:
        """Run ``query`` and return up to ``limit`` results, best first."""
        if not self.available or not (query or "").strip():
            return []
        parsed = parse_query(query)
        if parsed.is_empty:
            return []

        wanted = max(limit * (self.oversample + 2), 50)
        candidates = self._bm25.search(parsed, wanted, sources=sources,
                                       oversample=self.oversample)
        if not candidates:
            return []

        rowids = [rid for rid, _ in candidates]
        try:
            by_rowid = self._store.fetch_map(rowids)
        except Exception as exc:  # noqa: BLE001 - a broken store must not 500
            logger.warning("doc store read failed: %s", exc)
            return []

        collapse = self.collapse_host if collapse_host is None else collapse_host
        results: list[SearchResult] = []
        seen_hosts: set[str] = set()
        for rowid, bm25 in candidates:
            doc = by_rowid.get(rowid)
            if doc is None:
                continue
            text = doc.index_text
            if not self._verify(doc, parsed, text):
                continue
            if collapse and doc.host in seen_hosts:
                continue
            authority = max(0.0, min(10.0, doc.authority or 0.0))
            score = bm25 * (1.0 + self.authority_weight * authority / 10.0)
            results.append(SearchResult(
                doc=doc, score=score, bm25=bm25,
                snippet=highlight(text, parsed.all_terms(), self.snippet_width)))
            if collapse:
                seen_hosts.add(doc.host)
            if len(results) >= limit:
                break
        return results

    def suggest(self, query: str, limit: int = 10) -> list[str]:
        """Titles of the top matches — handy for autosuggest endpoints."""
        return [r.doc.title for r in self.search(query, limit=limit) if r.doc.title]
