"""v1 router: the crawl engine — cost, optimizer and the doctor.

    GET  /v1/cost/presets              canned crawl shapes with their price
    POST /v1/cost/estimate             cost + wall-clock for a spec
    POST /v1/cost/budget               invert: how many pages fit in $N
    POST /v1/optimizer/plan            which URLs to fetch, in what order, why
    POST /v1/optimizer/score           score one URL with its reasons
    GET  /v1/optimizer/weights         the scoring weights (explainable by design)
    GET  /v1/doctor                    per-phase health + diagnosis + advice
    POST /v1/doctor/spans              record spans (used by workers/CLI)
    GET  /v1/doctor/causes             failure taxonomy with counts

Every response here is meant to be quotable in a budget meeting: assumptions
are spelled out, and the numbers are reproducible from the same inputs.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Sequence

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep
from src.cost.simulator import CrawlSpec, PRESETS
from src.doctor.service import FAILURE_CAUSES, PHASES, Span, diagnose
from src.optimizer.policy import WEIGHTS, PageSignals, plan as plan_pages, score_page

logger = logging.getLogger("wcp.api.engine")
router = APIRouter(tags=["engine"])


class SpecBody(BaseModel):
    pages: int = Field(10_000, ge=1, le=100_000_000)
    avg_page_kb: float = Field(180.0, gt=0, le=100_000)
    render_share: float = Field(0.15, ge=0.0, le=1.0)
    proxy_tier: str = Field("none", pattern="^(none|datacenter|residential|mobile)$")
    captcha_rate: float = Field(0.0, ge=0.0, le=1.0)
    extract_llm_share: float = Field(0.0, ge=0.0, le=1.0)
    tokens_in_per_page: int = Field(1800, ge=0, le=200_000)
    tokens_out_per_page: int = Field(350, ge=0, le=200_000)
    embed_share: float = Field(0.0, ge=0.0, le=1.0)
    keep_snapshots: bool = True
    retention_days: int = Field(90, ge=1, le=3650)
    concurrency: int = Field(16, ge=1, le=2000)
    host_delay_seconds: float = Field(1.0, ge=0.0, le=120)
    pages_per_host: float = Field(25.0, gt=0, le=100_000)
    duplicate_rate: float = Field(0.12, ge=0.0, le=0.99)
    fetch_failure_rate: float = Field(0.05, ge=0.0, le=0.99)


class BudgetBody(BaseModel):
    budget_usd: float = Field(..., gt=0, le=1_000_000)
    spec: SpecBody | None = None


class SignalsBody(BaseModel):
    signals: list[dict[str, Any]] = Field(..., max_length=100_000)
    budget_pages: int | None = Field(None, ge=1, le=1_000_000)
    budget_usd: float | None = Field(None, gt=0, le=1_000_000)
    per_host_cap: int | None = Field(None, ge=1, le=1_000_000)
    max_host_share: float = Field(0.35, gt=0.0, le=1.0)


class OneSignalBody(BaseModel):
    signal: dict[str, Any]


class SpansBody(BaseModel):
    spans: list[dict[str, Any]] = Field(..., max_length=10_000)
    store: bool = True


def _platform(request: Request):
    return request.app.state.services.platform


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
@router.get("/cost/presets", summary="Canned crawl shapes and what they cost")
async def cost_presets(request: Request, ctx: CtxDep) -> dict[str, Any]:
    return {"presets": _platform(request).cost_presets(),
            "price_book": __import__("dataclasses").asdict(
                _platform(request).price_book),
            "note": "prices are the platform's own cost-to-serve defaults "
                    "(override with WCP_PRICE_* env vars)"}


@router.post("/cost/estimate", summary="Cost and time for a crawl spec")
async def cost_estimate(body: SpecBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    return _platform(request).cost_estimate(body.model_dump())


@router.post("/cost/budget", summary="How many pages fit in a budget")
async def cost_budget(body: BudgetBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    spec = body.spec.model_dump() if body.spec else None
    return _platform(request).pages_for_budget(body.budget_usd, spec)


@router.get("/cost/specs", summary="The spec fields the simulator accepts")
async def cost_specs() -> dict[str, Any]:
    return {"fields": list(CrawlSpec().to_dict().keys()),
            "presets": sorted(PRESETS)}


# --------------------------------------------------------------------------- #
# optimizer
# --------------------------------------------------------------------------- #
@router.get("/optimizer/weights", summary="Scoring weights and how they combine")
async def optimizer_weights() -> dict[str, Any]:
    return {
        "weights": WEIGHTS,
        "note": "score = 100 × Σ(weight × component), scaled by (0.6 + 0.4 × "
                "content density); every score is returned with human reasons",
        "actions": ["crawl", "defer", "skip"],
        "fetch_strategies": ["http", "browser"],
        "fields": list(PageSignals(url="").to_dict().keys()),
    }


@router.post("/optimizer/score", summary="Score one URL with its reasons")
async def optimizer_score(body: OneSignalBody, request: Request, ctx: CtxDep
                          ) -> dict[str, Any]:
    signal = PageSignals.from_dict(body.signal)
    if not signal.url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "signal.url is required")
    return score_page(signal).to_dict()


@router.post("/optimizer/plan", summary="Budgeted fetch plan with reasons per row")
async def optimizer_plan(body: SignalsBody, request: Request, ctx: CtxDep
                         ) -> dict[str, Any]:
    platform = _platform(request)
    try:
        result = platform.plan_crawl(body.signals, budget_pages=body.budget_pages,
                                     budget_usd=body.budget_usd,
                                     per_host_cap=body.per_host_cap,
                                     max_host_share=body.max_host_share)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return {**result, "note": "the selected list is the fetch order, not a sample"}


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
@router.get("/doctor", summary="Crawl health: phases, percentiles, advice")
async def doctor_report(request: Request, ctx: CtxDep,
                        limit: Annotated[int, Query(ge=1, le=2000)] = 300
                        ) -> dict[str, Any]:
    platform = _platform(request)
    spans = platform.doctor.recent(ctx["tenant"], limit=limit)
    return {"report": diagnose(spans).to_dict(),
            "counters": platform.doctor.counters(ctx["tenant"]),
            "phases": list(PHASES),
            "causes": list(FAILURE_CAUSES)}


@router.post("/doctor/spans", summary="Record fetch spans (workers/CLI)")
async def doctor_spans(body: SpansBody, request: Request, ctx: CtxDep
                       ) -> dict[str, Any]:
    platform = _platform(request)
    spans = [Span.from_dict(row) for row in body.spans]
    if body.store:
        for span in spans:
            platform.doctor.record(ctx["tenant"], span)
    report = diagnose(spans)
    return {"recorded": len(spans) if body.store else 0,
            "report": report.to_dict()}


@router.get("/doctor/causes", summary="Failure taxonomy with counts")
async def doctor_causes(request: Request, ctx: CtxDep) -> dict[str, Any]:
    counters = _platform(request).doctor.counters(ctx["tenant"])
    causes = counters.get("causes", {})
    return {
        "total": counters.get("total", 0),
        "failures": counters.get("failures", 0),
        "success_rate": (round(1 - counters.get("failures", 0) / counters["total"], 4)
                         if counters.get("total") else None),
        "causes": causes,
        "vocabulary": list(FAILURE_CAUSES),
    }
