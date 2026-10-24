"""Tenant-scoped catalog API for declarative agent manifests.

This surface publishes and discovers agent metadata only; it does not run
managed agents or accept publisher executable code.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request

from src.agent_marketplace.managed import list_managed_agents, run_managed_agent
from src.agent_marketplace.service import (
    AgentMarketplaceError,
    AgentMarketplaceService,
)
from src.api.routers.jobs import CtxDep
from src.api.schemas import (
    AgentCardCreate,
    AgentCardOut,
    AgentCardUpdate,
    ManagedAgentOut,
    ManagedAgentRunOut,
    ManagedAgentRunRequest,
)

router = APIRouter(prefix="/agent-marketplace", tags=["agent-marketplace"])


def _service(request: Request) -> AgentMarketplaceService:
    service = getattr(request.app.state.services, "agent_marketplace", None)
    if service is None:
        service = AgentMarketplaceService(request.app.state.services.redis)
        request.app.state.services.agent_marketplace = service
    return service


def _out(card) -> AgentCardOut:
    return AgentCardOut.model_validate(asdict(card))


@router.get("/managed/agents", response_model=list[ManagedAgentOut])
def list_server_managed_agents() -> list[ManagedAgentOut]:
    return [ManagedAgentOut.model_validate(agent)
            for agent in list_managed_agents()]


@router.post("/managed/agents/{agent_id}/runs",
             response_model=ManagedAgentRunOut)
def execute_managed_agent(
    agent_id: str,
    body: ManagedAgentRunRequest,
    ctx: CtxDep,
) -> ManagedAgentRunOut:
    del ctx  # authentication is required; no tenant data is stored or shared
    try:
        result = run_managed_agent(agent_id, body.records)
    except AgentMarketplaceError as exc:
        raise HTTPException(404, "managed agent not found") from exc
    return ManagedAgentRunOut.model_validate(result)


@router.post("/agents", response_model=AgentCardOut, status_code=201)
async def create_agent_card(body: AgentCardCreate, ctx: CtxDep,
                            request: Request) -> AgentCardOut:
    card = _service(request).create(
        str(ctx["tenant"]), manifest=body.manifest.model_dump(mode="json"),
        tags=body.tags, license=body.license)
    return _out(card)


@router.get("/agents", response_model=list[AgentCardOut])
async def search_agent_cards(
    request: Request,
    q: str = Query("", max_length=120),
    tag: str = Query("", max_length=40),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=5000),
) -> list[AgentCardOut]:
    cards = _service(request).search(query=q, tag=tag, limit=limit, offset=offset)
    return [_out(card) for card in cards]


@router.get("/my/agents", response_model=list[AgentCardOut])
async def list_my_agent_cards(
    ctx: CtxDep,
    request: Request,
    status: str | None = Query(None, pattern="^(draft|published|suspended|archived)$"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=5000),
) -> list[AgentCardOut]:
    cards = _service(request).list_owned(
        str(ctx["tenant"]), status=status, limit=limit, offset=offset)
    return [_out(card) for card in cards]


@router.get("/agents/{agent_id}", response_model=AgentCardOut)
async def get_agent_card(agent_id: str, ctx: CtxDep,
                         request: Request) -> AgentCardOut:
    card = _service(request).get_for_viewer(agent_id, str(ctx["tenant"]))
    if card is None:
        raise HTTPException(404, "agent listing not found")
    return _out(card)


@router.patch("/agents/{agent_id}", response_model=AgentCardOut)
async def update_agent_card(agent_id: str, body: AgentCardUpdate,
                            ctx: CtxDep, request: Request) -> AgentCardOut:
    changes = body.model_dump(exclude_unset=True, mode="json")
    try:
        card = _service(request).update(agent_id, str(ctx["tenant"]), changes=changes)
    except AgentMarketplaceError as exc:
        if str(exc) == "not found":
            raise HTTPException(404, "agent listing not found") from exc
        raise HTTPException(422, str(exc)) from exc
    return _out(card)
