"""v1 router: semantic search + RAG over collected data."""

from __future__ import annotations

import logging
import time
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.routers.jobs import CtxDep, get_redis
from src.api.schemas import SearchQuery, SearchResponse, SearchResult

logger = logging.getLogger("wcp.api.search")
router = APIRouter(prefix="/search", tags=["search"])


def _search_backend(request: Request):
    """Lazily build the tenant-scoped SemanticSearch (ChromaDB or fallback)."""
    cache = request.app.state.__dict__.setdefault("search_cache", {})
    tenant = getattr(request.state, "tenant", "default")
    if tenant not in cache:
        from src.ai.vector_search import SemanticSearch

        cache[tenant] = SemanticSearch(collection=f"wcp_{tenant}")
    return cache[tenant]


@router.post("", response_model=SearchResponse, summary="Semantic (vector) search")
async def semantic_search(body: SearchQuery, ctx: CtxDep, request: Request) -> SearchResponse:
    t0 = time.perf_counter()
    backend = _search_backend(request)
    try:
        hits = backend.search(body.query, top_k=body.top_k)
    except Exception as exc:
        logger.exception("vector search failed")
        raise HTTPException(503, f"search backend unavailable: {exc}")
    results = []
    for h in hits:
        meta = h.get("metadata", {}) or {}
        if body.filter and any(meta.get(k) != v for k, v in body.filter.items()):
            continue
        results.append(SearchResult(
            doc_id=h["id"], score=float(h.get("score", 0.0)),
            text=h.get("text", ""), metadata=meta))
    took = int((time.perf_counter() - t0) * 1000)
    return SearchResponse(results=results, took_ms=took,
                          embedding_backend=getattr(backend, "backend_name", "hash-fallback"))


@router.get("/rag", summary="RAG answer: retrieve context + LLM synthesis")
async def rag_query(ctx: CtxDep, request: Request,
                    q: Annotated[str, Query(min_length=3, max_length=4096)],
                    top_k: Annotated[int, Query(ge=1, le=20)] = 5,
                    include_sources: bool = True) -> dict:
    """Retrieval-Augmented Generation endpoint.

    Retrieves the most relevant collected chunks and synthesises an answer
    with the configured LLM (LangChain when installed; deterministic extractive
    summariser otherwise so the endpoint always works offline).
    """
    backend = _search_backend(request)
    try:
        hits = backend.search(q, top_k=top_k)
    except Exception as exc:
        raise HTTPException(503, f"retrieval failed: {exc}")

    contexts = [h.get("text", "") for h in hits if h.get("text")]
    if not contexts:
        return {"answer": None, "sources": [], "note": "no context found"}

    answer: Optional[str] = None
    engine = "extractive"
    try:  # prefer LangChain/OpenAI if available & configured
        from src.ai.agents.rag_chain import answer_with_rag

        answer = answer_with_rag(q, contexts)
        engine = "llm"
    except Exception:
        answer = None
    if answer is None:
        # simple, honest extractive fallback: best sentences containing query terms
        terms = {t.lower() for t in q.split() if len(t) > 3}
        scored = []
        for c in contexts:
            for s in c.replace("\n", " ").split(". "):
                s = s.strip()
                if len(s) < 20:
                    continue
                score = sum(1 for t in terms if t in s.lower())
                if score:
                    scored.append((score, s))
        scored.sort(reverse=True)
        answer = ". ".join(s for _, s in scored[:3]) or contexts[0][:500]

    out: dict = {"answer": answer, "engine": engine}
    if include_sources:
        out["sources"] = [{"doc_id": h["id"], "score": h.get("score", 0),
                           "url": (h.get("metadata") or {}).get("url")}
                          for h in hits]
    return out
