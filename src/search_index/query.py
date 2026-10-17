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
from src.search_index.models import Doc, IndexStats, SearchResult
from src.search_index.rerank import Reranker, build_reranker
from src.search_index.spell import SPELL_FILE, SpellChecker
from src.search_index.store import DocStore
from src.search_index.text import ParsedQuery, analyze, highlight, parse_query

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QueryOptions:
    """Per-query overrides, so callers can experiment without touching config."""

    rerank: str | None = None            # None = use the index default
    rerank_weight: float | None = None
    spell: bool | None = None
    collapse_host: bool | None = None
    limit_candidates: int | None = None  # how many BM25 hits to rerank


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
                 oversample: int = 4,
                 rerank: str = "none",
                 rerank_weight: float = 0.35,
                 embed_model: str | None = None,
                 rerank_candidates: int = 100,
                 spell: bool = True,
                 reranker: Reranker | None = None,
                 spell_checker: SpellChecker | None = None) -> None:
        self.index_dir = Path(index_dir)
        self.snippet_width = int(snippet_width)
        self.authority_weight = float(authority_weight)
        self.collapse_host = bool(collapse_host)
        self.oversample = max(1, int(oversample))
        self.rerank_mode = (rerank or "none").strip().lower()
        self.rerank_weight = float(rerank_weight)
        self.rerank_candidates = max(1, int(rerank_candidates))
        self.spell_enabled = bool(spell)
        self._bm25 = ShardedBM25(self.index_dir, mmap=mmap,
                                 max_loaded_shards=max_loaded_shards)
        self._store = DocStore(self.index_dir.parent / "docs.sqlite3",
                               readonly=True)
        self._reranker = reranker
        self._rerankers: dict[str, Reranker] = {}
        self._spell = spell_checker

    # -- lifecycle ----------------------------------------------------------- #
    @classmethod
    def open(cls, index_dir: str | Path, **kwargs) -> SearchIndex:
        return cls(index_dir, **kwargs)

    @property
    def available(self) -> bool:
        return self._bm25.shard_count > 0

    def reload(self) -> int:
        self._spell = None
        self._reranker = None
        self._rerankers.clear()
        return self._bm25.reload()

    # -- optional stages ---------------------------------------------------- #
    @property
    def reranker(self) -> Reranker:
        """The configured reranker, built on first use (may be a no-op)."""
        if self._reranker is not None:
            return self._reranker
        return self.reranker_for(self.rerank_mode)

    def reranker_for(self, mode: str) -> Reranker:
        """The reranker for ``mode``, built once and cached per mode.

        Kept separate from :attr:`reranker` so a single ``search(rerank=…)``
        call can use a different mode from the configured default without
        throwing away the one already loaded.
        """
        key = (mode or "none").strip().lower()
        injected = self._reranker
        if injected is not None and _mode_matches(key, injected):
            return injected
        cached = self._rerankers.get(key)
        if cached is None:
            cached = build_reranker(
                key, index_dir=self.index_dir, weight=self.rerank_weight,
                max_loaded_shards=self._bm25._max_loaded)
            self._rerankers[key] = cached
        return cached

    @property
    def spell_checker(self) -> SpellChecker | None:
        """The corpus-trained spell checker, if one has been built."""
        if self._spell is None and self.spell_enabled:
            self._spell = SpellChecker.load(self.index_dir / SPELL_FILE)
        return self._spell

    def correct(self, query: str) -> tuple[str, list[tuple[str, str]]]:
        """Return ``(corrected_query, [(was, became), ...])``.

        Operators (``site:``, ``source:``, ``-exclusion``) and quoted phrases are
        left alone: correcting ``site:.edu`` into ``site:.end`` would be worse
        than the typo.
        """
        checker = self.spell_checker
        if checker is None or not (query or "").strip():
            return query, []
        parsed = parse_query(query)
        corrections: list[tuple[str, str]] = []

        terms, found = checker.correct_terms(parsed.terms)
        corrections.extend(found)
        parsed.terms = terms
        phrases = []
        for phrase in parsed.phrases:
            fixed, found = checker.correct_terms(phrase)
            corrections.extend(found)
            phrases.append(fixed)
        parsed.phrases = phrases
        excludes, found = checker.correct_terms(parsed.excludes)
        corrections.extend(found)
        parsed.excludes = excludes

        if not corrections:
            return query, []
        return _render_query(parsed), corrections

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- inspection ---------------------------------------------------------- #
    def analytics(self, **filters) -> dict:
        """Aggregate bounded dimensions over every indexed document, not top-k hits."""
        return self._store.analytics(**filters)

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
               collapse_host: bool | None = None,
               spell: bool | None = None,
               rerank: str | None = None) -> list[SearchResult]:
        """Run ``query`` and return up to ``limit`` results, best first.

        Pipeline: correct spelling -> BM25 retrieve -> verify phrases/exclusions
        -> blend with authority -> rerank -> collapse to one result per host.
        """
        if not self.available or not (query or "").strip():
            return []

        effective_query = query
        if spell is None:
            spell = self.spell_enabled
        if spell:
            effective_query, _corrections = self.correct(query)
        parsed = parse_query(effective_query)
        if parsed.is_empty:
            return []

        wanted = max(min(limit * (self.oversample + 2), self.rerank_candidates), 50)
        candidates = self._bm25.search(parsed, wanted, sources=sources,
                                       oversample=self.oversample)
        if not candidates:
            return []

        rowids = [hit.rowid for hit in candidates]
        try:
            by_rowid = self._store.fetch_map(rowids)
        except Exception as exc:  # noqa: BLE001 - a broken store must not 500
            logger.warning("doc store read failed: %s", exc)
            return []

        collapse = self.collapse_host if collapse_host is None else collapse_host
        results: list[SearchResult] = []
        seen_hosts: set[str] = set()
        for hit in candidates:
            doc = by_rowid.get(hit.rowid)
            if doc is None:
                continue
            text = doc.index_text
            if not self._verify(doc, parsed, text):
                continue
            if collapse and doc.host in seen_hosts:
                continue
            authority = max(0.0, min(10.0, doc.authority or 0.0))
            score = hit.score * (1.0 + self.authority_weight * authority / 10.0)
            results.append(SearchResult(
                doc=doc, doc_id=hit.rowid, shard_id=hit.shard_id,
                score=score, bm25=hit.score,
                snippet=highlight(text, parsed.all_terms(), self.snippet_width)))
            if collapse:
                seen_hosts.add(doc.host)
            if len(results) >= limit:
                break

        mode = (rerank or self.rerank_mode).strip().lower()
        if mode not in ("", "none", "off", "false") and len(results) > 1:
            results = self.reranker_for(mode).rerank(
                effective_query, results, top_k=limit)
        return results[:limit]

    def suggest(self, query: str, limit: int = 10) -> list[str]:
        """Titles of the top matches — handy for autosuggest endpoints."""
        return [r.doc.title for r in self.search(query, limit=limit) if r.doc.title]


def _mode_matches(mode: str, reranker: Reranker) -> bool:
    """Does ``reranker`` satisfy ``mode``? (``embedding`` vs ``embedding:…``)"""
    name = getattr(reranker, "name", "") or ""
    if not mode or mode in ("none", "off", "false"):
        return True
    return mode == name or mode == name.split(":")[0]


def _render_query(parsed: ParsedQuery) -> str:
    """Rebuild a query string from a corrected :class:`ParsedQuery`."""
    parts: list[str] = list(parsed.terms)
    parts.extend('"' + " ".join(phrase) + '"' for phrase in parsed.phrases)
    parts.extend("-" + term for term in parsed.excludes)
    if parsed.site:
        parts.append("site:" + parsed.site)
    if parsed.source:
        parts.append("source:" + parsed.source)
    return " ".join(parts)
