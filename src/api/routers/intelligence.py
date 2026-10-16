"""v1 routers: intelligence surfaces — /ask, /research, /graph, /domain.

    POST /v1/ask                    question + budget → cited answer
    POST /v1/ask/plan               how a question is decomposed (spends nothing)
    POST /v1/research               full report with citations and a passport
    GET  /v1/research               recent reports for the tenant
    GET  /v1/research/{id}          one report (markdown included)
    GET  /v1/graph                  entities for the tenant
    GET  /v1/graph/{entity_id}      subgraph for the visual view
    GET  /v1/graph/{entity_id}/edges  neighbours with evidence
    POST /v1/graph/extract          extract entities from text (paid in nothing)
    GET  /v1/domain/{host}          public domain score
    POST /v1/doc-diff               classify the difference between two documents

Every endpoint here answers with evidence or explicitly says it has none; none
of them fabricate. Budgets are enforced by the services layer, not by the
router, so a Celery worker and an MCP client get the same behaviour.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Annotated, Any, Sequence

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep

logger = logging.getLogger("wcp.api.intelligence")
router = APIRouter(tags=["intelligence"])

REPORT_KEY = "wcp:research:{tenant}:{report_id}"
REPORT_INDEX = "wcp:research:{tenant}:index"
REPORT_TTL = 60 * 60 * 24 * 90


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class BudgetBody(BaseModel):
    max_pages: int = Field(25, ge=1, le=2000)
    max_seconds: float = Field(30.0, gt=0, le=3600)
    max_usd: float = Field(0.50, gt=0, le=1000)
    render_share: float = Field(0.0, ge=0.0, le=1.0)
    llm: bool = False


class AskBody(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    budget: BudgetBody = Field(default_factory=BudgetBody)
    mode: str = Field("auto", pattern="^(auto|corpus|web|index)$")
    urls: list[str] = Field(default_factory=list, max_length=100)


class ResearchBody(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    budget: BudgetBody = Field(default_factory=BudgetBody)
    subquestions: list[str] = Field(default_factory=list, max_length=10)
    urls: list[str] = Field(default_factory=list, max_length=200)
    mode: str = Field("auto", pattern="^(auto|corpus|web|index)$")


class GraphExtractBody(BaseModel):
    text: str = Field(..., min_length=3, max_length=200_000)
    url: str = ""


class DiffBody(BaseModel):
    before: str = Field("", max_length=2_000_000)
    after: str = Field("", max_length=2_000_000)
    url: str = ""
    kinds: list[str] = Field(default_factory=list)


def _platform(request: Request):
    return request.app.state.services.platform


# --------------------------------------------------------------------------- #
# /ask
# --------------------------------------------------------------------------- #
@router.post("/ask", summary="Ask a question, get a cited answer within a budget")
async def ask(body: AskBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    tenant = ctx["tenant"]
    platform = _platform(request)
    try:
        return platform.ask(body.question, budget=body.budget.model_dump(),
                            mode=body.mode, tenant=tenant,
                            urls=body.urls or None)
    except Exception as exc:  # noqa: BLE001 - an ask must not 500 the API
        logger.exception("ask failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"ask could not run: {exc}")


@router.post("/ask/plan", summary="How a question is decomposed (no spend)")
async def ask_plan(body: AskBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    return {"plan": _platform(request).ask_plan(body.question),
            "budget": body.budget.model_dump(), "tenant": ctx["tenant"]}


# --------------------------------------------------------------------------- #
# /research
# --------------------------------------------------------------------------- #
@router.post("/research", status_code=status.HTTP_201_CREATED,
             summary="Run a research report (plan → gather → verify → report)")
async def create_research(body: ResearchBody, request: Request, ctx: CtxDep
                          ) -> dict[str, Any]:
    tenant = ctx["tenant"]
    platform = _platform(request)
    try:
        report = platform.research(
            body.question, budget=body.budget.model_dump(), tenant=tenant,
            urls=body.urls or None, subquestions=body.subquestions or None,
            mode=body.mode)
    except Exception as exc:  # noqa: BLE001
        logger.exception("research failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"research could not run: {exc}")
    _store_report(request, tenant, report)
    return report


@router.get("/research", summary="Recent research reports")
async def list_research(request: Request, ctx: CtxDep,
                        limit: Annotated[int, Query(ge=1, le=100)] = 20
                        ) -> dict[str, Any]:
    tenant = ctx["tenant"]
    r = request.app.state.services.redis
    ids = [i.decode() if isinstance(i, bytes) else str(i)
           for i in (r.zrevrange(REPORT_INDEX.format(tenant=tenant), 0, limit - 1) or [])]
    out = []
    for report_id in ids:
        raw = r.get(REPORT_KEY.format(tenant=tenant, report_id=report_id))
        if raw:
            payload = json.loads(raw)
            payload.pop("markdown", None)
            out.append(payload)
    return {"items": out, "total": len(out)}


@router.get("/research/{report_id}", summary="One research report")
async def get_research(report_id: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    tenant = ctx["tenant"]
    raw = request.app.state.services.redis.get(
        REPORT_KEY.format(tenant=tenant, report_id=report_id))
    if not raw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return json.loads(raw)


@router.get("/research/{report_id}/report.md", summary="The report as markdown",
            response_class=__import__("fastapi.responses", fromlist=["PlainTextResponse"]
                                      ).PlainTextResponse)
async def get_research_markdown(report_id: str, request: Request, ctx: CtxDep):
    from fastapi.responses import PlainTextResponse

    tenant = ctx["tenant"]
    raw = request.app.state.services.redis.get(
        REPORT_KEY.format(tenant=tenant, report_id=report_id))
    if not raw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    payload = json.loads(raw)
    return PlainTextResponse(payload.get("markdown", ""),
                             headers={"Content-Disposition":
                                      f'attachment; filename="{report_id}.md"'})


def _store_report(request: Request, tenant: str, report: dict[str, Any]) -> None:
    r = request.app.state.services.redis
    report_id = report.get("report_id", "")
    if not report_id:
        return
    try:
        r.setex(REPORT_KEY.format(tenant=tenant, report_id=report_id), REPORT_TTL,
                json.dumps(report, default=str))
        r.zadd(REPORT_INDEX.format(tenant=tenant), {report_id: time.time()})
    except Exception as exc:  # noqa: BLE001 - persistence is not the deliverable
        logger.warning("could not persist report %s: %s", report_id, exc)


# --------------------------------------------------------------------------- #
# /graph
# --------------------------------------------------------------------------- #
@router.get("/graph", summary="Entities in the tenant's knowledge graph")
async def graph_entities(request: Request, ctx: CtxDep,
                         limit: Annotated[int, Query(ge=1, le=500)] = 100,
                         kind: str = "") -> dict[str, Any]:
    graph = _platform(request).graph
    tenant = ctx["tenant"]
    return {
        "entities": [e.to_dict() for e in graph.store.entities(tenant, limit=limit,
                                                                kind=kind)],
        "stats": graph.store.stats(tenant),
        "kinds": ["company", "product", "person", "technology", "place", "topic"],
    }


@router.get("/graph/search", summary="Find an entity by name (with resolution)")
async def graph_search(request: Request, ctx: CtxDep,
                       q: Annotated[str, Query(min_length=1, max_length=200)],
                       limit: Annotated[int, Query(ge=1, le=50)] = 10
                       ) -> dict[str, Any]:
    graph = _platform(request).graph
    return {"query": q,
            "entities": [e.to_dict() for e in graph.store.search(ctx["tenant"], q,
                                                                 limit=limit)]}


@router.get("/graph/duplicates", summary="Proposed merges (never applied silently)")
async def graph_duplicates(request: Request, ctx: CtxDep,
                           threshold: Annotated[float, Query(ge=0.5, le=0.99)] = 0.82
                           ) -> dict[str, Any]:
    from src.graph.service import merge_duplicates

    graph = _platform(request).graph
    proposals = merge_duplicates(ctx["tenant"], graph.store, threshold=threshold)
    return {"proposals": proposals, "count": len(proposals),
            "note": "merging destroys information: review before applying"}


@router.get("/graph/{entity_id}", summary="Neighbourhood subgraph for the viewer")
async def graph_subgraph(entity_id: str, request: Request, ctx: CtxDep,
                         depth: Annotated[int, Query(ge=1, le=3)] = 2,
                         limit: Annotated[int, Query(ge=1, le=200)] = 40
                         ) -> dict[str, Any]:
    graph = _platform(request).graph
    subgraph = graph.store.subgraph(ctx["tenant"], entity_id, depth=depth,
                                    limit=limit)
    if not subgraph["nodes"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity not found")
    from src.graph.service import graph_to_mermaid

    return {**subgraph, "mermaid": graph_to_mermaid(subgraph)}


@router.get("/graph/{entity_id}/edges", summary="Edges with their evidence")
async def graph_edges(entity_id: str, request: Request, ctx: CtxDep,
                      direction: str = "both",
                      limit: Annotated[int, Query(ge=1, le=200)] = 50
                      ) -> dict[str, Any]:
    graph = _platform(request).graph
    return {"entity_id": entity_id,
            "edges": graph.store.edges(ctx["tenant"], entity_id,
                                       direction=direction, limit=limit)}


@router.post("/graph/extract", summary="Extract entities and relations from text")
async def graph_extract(body: GraphExtractBody, request: Request, ctx: CtxDep
                        ) -> dict[str, Any]:
    graph = _platform(request).graph
    return graph.ingest_text(ctx["tenant"], body.text, url=body.url)


# --------------------------------------------------------------------------- #
# /domain and /doc-diff
# --------------------------------------------------------------------------- #
@router.get("/domain/{host}", summary="Public domain score (0-100, with components)")
async def domain_score(host: str, request: Request, ctx: CtxDep) -> dict[str, Any]:
    if "." not in host:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "host must be a domain name")
    return _platform(request).domain_score(host, tenant=ctx["tenant"])


@router.post("/doc-diff", summary="Classify what changed between two documents")
async def doc_diff(body: DiffBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    platform = _platform(request)
    events = platform.diff_text(body.before, body.after, url=body.url,
                                kinds=body.kinds or None)
    significant = [e for e in events if e["significance"] >= 0.35]
    return {
        "events": events,
        "significant": significant,
        "has_change": bool(events),
        "summary": (significant[0]["summary"] if significant
                    else (events[0]["summary"] if events else "no meaningful change")),
    }
