"""v1 router: the platform's own search index.

    GET  /v1/search-index/stats                 index size, sources, build state
    GET  /v1/search-index/query?q=…&limit=10    search it
    POST /v1/search-index/query                 same, with filters in the body
    GET  /v1/search-index/spell?q=…             correct a query against our vocab
    GET  /v1/search-index/levels                the $0 roadmap behind the index

This is the HTTP face of :mod:`src.search_index`. When an index has been built
(``wcp search-index build --level N``) these endpoints answer topic queries
without touching DuckDuckGo, Brave, SerpAPI or anyone else — no key, no quota,
no per-query cost, and it keeps working with the network unplugged.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep

logger = logging.getLogger("wcp.api.search_index")
router = APIRouter(prefix="/search-index", tags=["search-index"])


class OwnSearchQuery(BaseModel):
    q: str = Field(..., min_length=1, max_length=2048,
                   description='Supports "phrases", -exclusions, site:host, source:x')
    limit: int = Field(10, ge=1, le=100)
    sources: list[str] = Field(default_factory=list,
                               description="curlie / wikipedia / commoncrawl")
    collapse_host: bool | None = Field(
        None, description="one result per website (default: SEARCH_INDEX_COLLAPSE_HOST)")
    rerank: str | None = Field(
        None, description="none | embedding | cross-encoder "
                          "(default: SEARCH_INDEX_RERANK)")
    spell: bool | None = Field(
        None, description="correct typos against the index vocabulary")


class OwnSearchHit(BaseModel):
    url: str
    title: str = ""
    host: str = ""
    snippet: str = ""
    score: float = 0.0
    bm25: float = 0.0
    semantic: float = 0.0
    authority: float = 0.0
    source: str = ""
    rank: int = 0


def _index(request: Request):
    """One shared, lazily-built :class:`SearchIndex` per process."""
    cache = request.app.state.__dict__.setdefault("own_index_cache", {})
    if "index" not in cache:
        from src.config import get_config
        from src.search_index.query import SearchIndex

        cfg = get_config()
        cache["index"] = SearchIndex(
            f"{cfg.SEARCH_INDEX_DIR}/index",
            collapse_host=cfg.SEARCH_INDEX_COLLAPSE_HOST,
            authority_weight=cfg.SEARCH_INDEX_AUTHORITY_WEIGHT,
            snippet_width=cfg.SEARCH_INDEX_SNIPPET_WIDTH,
            max_loaded_shards=cfg.SEARCH_INDEX_MAX_LOADED_SHARDS,
            rerank=cfg.SEARCH_INDEX_RERANK,
            rerank_weight=cfg.SEARCH_INDEX_RERANK_WEIGHT,
            rerank_candidates=cfg.SEARCH_INDEX_RERANK_CANDIDATES,
            embed_model=cfg.SEARCH_INDEX_EMBED_MODEL,
            spell=cfg.SEARCH_INDEX_SPELL,
        )
    return cache["index"]


@router.get("/stats", summary="Size and build state of the own index")
async def stats(ctx: CtxDep, request: Request) -> dict:
    index = _index(request)
    payload = index.stats().to_dict()
    payload["available"] = index.available
    payload["reranker"] = getattr(index.reranker, "name", "none") \
        if index.available else "none"
    try:
        from src.search_index.indexer import SearchIndexBuilder

        with SearchIndexBuilder(index.index_dir.parent) as builder:
            payload["extras"] = builder.extras()
    except Exception as exc:  # noqa: BLE001 - stats must stay readable
        payload["extras"] = {"error": str(exc)}
    return payload


@router.get("/spell", summary="Correct a query against the index vocabulary")
async def spell(ctx: CtxDep, request: Request,
                q: Annotated[str, Query(min_length=1, max_length=512)]) -> dict:
    """Return the corrected query and what changed.

    The dictionary is built from the index's own terms (`wcp search-index
    spell`), so it knows the jargon in *your* corpus and will not "fix"
    kubernetes into something else.
    """
    index = _index(request)
    if not index.available:
        raise HTTPException(503, "no own search index has been built yet")
    corrected, corrections = index.correct(q)
    checker = index.spell_checker
    return {"query": q, "corrected_query": corrected,
            "corrections": [{"from": a, "to": b} for a, b in corrections],
            "trained": checker is not None,
            "terms": len(checker) if checker is not None else 0}


@router.get("/levels", summary="The free roadmap: what each index level costs")
async def levels(_ctx: CtxDep) -> dict:
    from src.search_index.models import LEVELS

    return {"levels": [LEVELS[level].to_dict() for level in sorted(LEVELS)]}


@router.get("/query", summary="Search the own index")
async def query_get(ctx: CtxDep, request: Request,
                    q: Annotated[str, Query(min_length=1, max_length=2048)],
                    limit: Annotated[int, Query(ge=1, le=100)] = 10,
                    sources: Annotated[str, Query(
                        description="comma-separated source filter")] = "",
                    collapse_host: bool | None = None,
                    rerank: Annotated[str, Query(
                        description="none | embedding | cross-encoder")] = "",
                    spell: bool | None = None) -> dict:
    return _run(request, q, limit, sources, collapse_host, rerank or None, spell)


@router.post("/query", summary="Search the own index")
async def query_post(body: OwnSearchQuery, ctx: CtxDep, request: Request) -> dict:
    return _run(request, body.q, body.limit, ",".join(body.sources),
                body.collapse_host, body.rerank, body.spell)


def _run(request: Request, query: str, limit: int, sources: str,
         collapse_host: bool | None, rerank: str | None = None,
         spell: bool | None = None) -> dict:
    index = _index(request)
    if not index.available:
        raise HTTPException(
            503, "no own search index has been built yet — run "
                 "`python -m src.cli search-index build --level 0`")

    # Correct once, here, so the response can say what it changed — then search
    # the corrected string with spelling off rather than correcting twice.
    corrections: list[tuple[str, str]] = []
    effective = query
    if spell is None:
        spell = index.spell_enabled
    if spell:
        try:
            effective, corrections = index.correct(query)
        except Exception as exc:  # noqa: BLE001 - never 500 on a typo pass
            logger.warning("spell correction failed: %s", exc)

    started = time.perf_counter()
    try:
        results = index.search(
            effective, limit,
            sources=[s for s in sources.split(",") if s] or None,
            collapse_host=collapse_host, spell=False, rerank=rerank)
    except Exception as exc:
        logger.exception("own index query failed")
        raise HTTPException(503, f"own index unavailable: {exc}") from exc
    took_ms = int((time.perf_counter() - started) * 1000)
    hits = [
        OwnSearchHit(url=r.doc.url, title=r.doc.title or r.doc.host,
                     host=r.doc.host, snippet=r.snippet, score=round(r.score, 5),
                     bm25=round(r.bm25, 5), semantic=round(r.semantic, 5),
                     authority=round(r.doc.authority, 3),
                     source=r.doc.source, rank=rank).model_dump()
        for rank, r in enumerate(results, 1)
    ]
    payload = {"query": query, "count": len(hits), "took_ms": took_ms,
               "results": hits,
               "reranker": getattr(index.reranker, "name", "none")}
    if corrections:
        payload["corrected_query"] = effective
        payload["corrections"] = [{"from": a, "to": b} for a, b in corrections]
    return payload
