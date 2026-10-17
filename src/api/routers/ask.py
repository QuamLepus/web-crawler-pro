"""Budget-aware retrieval endpoint for grounded, citation-bearing answers.

This endpoint searches the tenant's existing index; it does not silently launch
an unbounded web crawl. ``budget`` is an explicit ceiling for any future
retrieval expansion and is echoed as such, not billed as actual usage.
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Request
from src.api.routers.jobs import CtxDep
from src.api.routers.search import _search_backend

router = APIRouter(tags=["intelligence"])


class AskBudget(BaseModel):
    max_pages: int = Field(default=50, ge=1, le=500)
    max_seconds: int = Field(default=60, ge=1, le=3600)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4096)
    budget: AskBudget = Field(default_factory=AskBudget)
    top_k: int = Field(default=8, ge=1, le=20)


@router.post("/ask", summary="Ask a question against indexed sources")
async def ask(body: AskRequest, ctx: CtxDep, request: Request) -> dict[str, Any]:
    try:
        hits = _search_backend(request).search(body.question, top_k=body.top_k)
    except Exception as exc:
        raise HTTPException(503, "search backend unavailable") from exc
    cited = []
    excerpts = []
    for hit in hits:
        text = str(hit.get("text", "")).strip()
        if not text:
            continue
        meta = hit.get("metadata") or {}
        cited.append({"doc_id": hit.get("id"), "url": meta.get("url"),
                      "score": float(hit.get("score", 0)),
                      "collected_at": meta.get("collected_at") or meta.get("fetched_at")})
        excerpts.append(text)
    if not excerpts:
        return {"answer": None, "citations": [], "confidence": 0.0,
                "note": "No matching indexed sources were found; no web crawl was started.",
                "budget": body.budget.model_dump(), "cost_usd": None}
    terms = {t.lower() for t in body.question.split() if len(t) > 3}
    sentences = []
    for text in excerpts:
        sentences.extend(s.strip() for s in text.replace("\n", " ").split(". ")
                         if len(s.strip()) >= 20)
    ranked = sorted(((sum(term in s.lower() for term in terms), s) for s in sentences), reverse=True)
    answer = ". ".join(s for score, s in ranked[:4] if score) or excerpts[0][:600]
    confidence = round(min(0.95, 0.35 + 0.12 * len(cited)), 2)
    return {"answer": answer, "citations": cited, "confidence": confidence,
            "method": "extractive-index-retrieval", "budget": body.budget.model_dump(),
            "cost_usd": None, "cost_note": "No cost estimate is available for index retrieval."}
