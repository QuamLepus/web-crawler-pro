"""v1 router: automation — workflows, agents and the MCP bridge.

    GET  /v1/workflows                 list workflows
    POST /v1/workflows                 create (validated DAG)
    GET  /v1/workflows/templates       the templates worth starting from
    GET  /v1/workflows/catalogue       node vocabulary for the builder
    GET  /v1/workflows/{id}            one workflow
    PUT  /v1/workflows/{id}            replace (validated)
    DELETE /v1/workflows/{id}
    POST /v1/workflows/{id}/preview    dry-run: validity + execution order
    POST /v1/workflows/{id}/run        execute and return the run record
    GET  /v1/workflows/{id}/runs       run history
    GET  /v1/agents                    agent marketplace catalogue
    POST /v1/agents                    publish an agent manifest
    POST /v1/agents/{slug}/install     install → you own the workflow
    GET  /v1/agents/installed          what this tenant installed
    GET  /v1/agents/revenue            the revenue ledger and the split
    POST /v1/mcp                       JSON-RPC bridge (tools/list, tools/call)
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep
from src.workflows.models import Edge, Node, Workflow, new_blank, node_catalogue, template

logger = logging.getLogger("wcp.api.automation")
router = APIRouter(tags=["automation"])


class NodeBody(BaseModel):
    id: str = ""
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    label: str = ""
    position: dict[str, float] = Field(default_factory=dict)
    on_error: str = "fail"


class EdgeBody(BaseModel):
    source: str
    target: str


class WorkflowBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    description: str = Field("", max_length=1000)
    nodes: list[NodeBody] = Field(default_factory=list, max_length=200)
    edges: list[EdgeBody] = Field(default_factory=list, max_length=400)
    schedule: str = ""
    state: str = Field("draft", pattern="^(draft|active|paused)$")


class AgentBody(BaseModel):
    slug: str = Field(..., min_length=2, max_length=80)
    name: str = Field(..., min_length=2, max_length=160)
    version: str = "1.0.0"
    author: str = ""
    category: str = "custom"
    description: str = ""
    workflow: dict[str, Any] | None = None
    workflow_template: str = ""
    permissions: list[str] = Field(default_factory=list)
    data_scope: str = "public-web"
    price_cents_month: int = Field(0, ge=0, le=1_000_000)
    tags: list[str] = Field(default_factory=list)


class InstallBody(BaseModel):
    version: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class McpBody(BaseModel):
    jsonrpc: str = "2.0"
    id: Any = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def _platform(request: Request):
    return request.app.state.services.platform


def _to_workflow(body: WorkflowBody, tenant: str, *, workflow_id: str = "") -> Workflow:
    try:
        nodes = [Node.from_dict(n.model_dump()) for n in body.nodes]
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return Workflow(
        name=body.name, tenant=tenant, workflow_id=workflow_id,
        description=body.description, nodes=nodes,
        edges=[Edge(**e.model_dump()) for e in body.edges],
        schedule=body.schedule, state=body.state)


# --------------------------------------------------------------------------- #
# workflows
# --------------------------------------------------------------------------- #
@router.get("/workflows", summary="List workflows with run statistics")
async def list_workflows(request: Request, ctx: CtxDep,
                         limit: Annotated[int, Query(ge=1, le=200)] = 50
                         ) -> dict[str, Any]:
    store = _platform(request).workflows
    return {"items": [w.to_dict() for w in store.list(ctx["tenant"], limit=limit)],
            "stats": store.stats(ctx["tenant"])}


@router.get("/workflows/templates", summary="Workflow templates")
async def list_templates() -> dict[str, Any]:
    from src.workflows.models import template_list

    return {"templates": template_list()}


@router.get("/workflows/catalogue", summary="Node vocabulary for the builder")
async def catalogue() -> dict[str, Any]:
    return {"nodes": node_catalogue(),
            "note": "fixed vocabulary: a free-form code node would be an RCE endpoint"}


@router.post("/workflows", status_code=status.HTTP_201_CREATED,
             summary="Create a workflow")
async def create_workflow(body: WorkflowBody, request: Request, ctx: CtxDep
                          ) -> dict[str, Any]:
    platform = _platform(request)
    workflow = _to_workflow(body, ctx["tenant"])
    problems = workflow.validate()
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"problems": problems})
    platform.workflows.save(workflow)
    try:
        platform.audit.log("workflow.create", tenant=ctx["tenant"],
                           actor=str(ctx.get("user", "")),
                           workflow_id=workflow.workflow_id, nodes=len(workflow.nodes))
    except Exception:  # noqa: BLE001
        pass
    return workflow.to_dict()


@router.post("/workflows/blank", status_code=status.HTTP_201_CREATED,
             summary="Create a minimal valid workflow")
async def create_blank(request: Request, ctx: CtxDep, name: str = "New workflow"
                       ) -> dict[str, Any]:
    workflow = new_blank(name, ctx["tenant"])
    _platform(request).workflows.save(workflow)
    return workflow.to_dict()


@router.post("/workflows/from-template", status_code=status.HTTP_201_CREATED,
             summary="Create from a template")
async def create_from_template(request: Request, ctx: CtxDep, template_id: str
                               ) -> dict[str, Any]:
    try:
        payload = template(template_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"unknown template {template_id!r}")
    workflow = Workflow.from_dict(payload)
    workflow.tenant = ctx["tenant"]
    _platform(request).workflows.save(workflow)
    return workflow.to_dict()


@router.get("/workflows/{workflow_id}", summary="One workflow")
async def get_workflow(workflow_id: str, request: Request, ctx: CtxDep
                       ) -> dict[str, Any]:
    store = _platform(request).workflows
    workflow = store.get(ctx["tenant"], workflow_id)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")
    return {**workflow.to_dict(),
            "runs": [r.to_dict() for r in store.runs(ctx["tenant"],
                                                     workflow_id=workflow_id, limit=5)],
            "problems": workflow.validate()}


@router.put("/workflows/{workflow_id}", summary="Replace a workflow")
async def put_workflow(workflow_id: str, body: WorkflowBody, request: Request,
                       ctx: CtxDep) -> dict[str, Any]:
    store = _platform(request).workflows
    if store.get(ctx["tenant"], workflow_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")
    workflow = _to_workflow(body, ctx["tenant"], workflow_id=workflow_id)
    problems = workflow.validate()
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"problems": problems})
    store.save(workflow)
    return workflow.to_dict()


@router.delete("/workflows/{workflow_id}", summary="Delete a workflow")
async def delete_workflow(workflow_id: str, request: Request, ctx: CtxDep
                          ) -> dict[str, Any]:
    store = _platform(request).workflows
    if store.get(ctx["tenant"], workflow_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")
    store.delete(ctx["tenant"], workflow_id)
    return {"deleted": workflow_id}


@router.post("/workflows/{workflow_id}/preview", summary="Dry-run (validity + order)")
async def preview_workflow(workflow_id: str, request: Request, ctx: CtxDep
                           ) -> dict[str, Any]:
    try:
        return _platform(request).workflow_preview(workflow_id, tenant=ctx["tenant"])
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")


@router.post("/workflows/{workflow_id}/run", summary="Execute a workflow now")
async def run_workflow_endpoint(workflow_id: str, request: Request, ctx: CtxDep
                                ) -> dict[str, Any]:
    try:
        return _platform(request).run_workflow(ctx["tenant"], workflow_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")


@router.get("/workflows/{workflow_id}/runs", summary="Run history")
async def workflow_runs(workflow_id: str, request: Request, ctx: CtxDep,
                        limit: Annotated[int, Query(ge=1, le=100)] = 20
                        ) -> dict[str, Any]:
    store = _platform(request).workflows
    return {"items": [r.to_dict() for r in
                      store.runs(ctx["tenant"], workflow_id=workflow_id, limit=limit)]}


# --------------------------------------------------------------------------- #
# agent marketplace
# --------------------------------------------------------------------------- #
@router.get("/agents", summary="Agent marketplace catalogue")
async def list_agents(request: Request, ctx: CtxDep, category: str = "", q: str = ""
                      ) -> dict[str, Any]:
    market = _platform(request).agents
    return {"items": market.catalog(category=category, query=q),
            "stats": market.stats(),
            "categories": ["monitoring", "research", "commerce", "news",
                           "compliance", "academic", "custom"]}


@router.post("/agents", status_code=status.HTTP_201_CREATED, summary="Publish an agent")
async def publish_agent(body: AgentBody, request: Request, ctx: CtxDep) -> dict[str, Any]:
    from src.marketplace.agents import AgentManifest

    manifest = AgentManifest(
        slug=body.slug, name=body.name, version=body.version, author=body.author,
        category=body.category, description=body.description,
        workflow=body.workflow, workflow_template=body.workflow_template,
        permissions=body.permissions, data_scope=body.data_scope,
        price_cents_month=body.price_cents_month, tags=body.tags)
    try:
        return _platform(request).agents.publish(manifest)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.get("/agents/installed", summary="Agents installed by this tenant")
async def installed_agents(request: Request, ctx: CtxDep) -> dict[str, Any]:
    market = _platform(request).agents
    return {"items": market.installed(ctx["tenant"]),
            "installs": market.stats()["installs"]}


@router.get("/agents/revenue", summary="Revenue ledger and the publisher split")
async def agent_revenue(request: Request, ctx: CtxDep, slug: str = "") -> dict[str, Any]:
    return _platform(request).agents.revenue(slug)


@router.post("/agents/{slug}/install", summary="Install an agent (you own the workflow)")
async def install_agent(slug: str, body: InstallBody, request: Request, ctx: CtxDep
                        ) -> dict[str, Any]:
    platform = _platform(request)
    try:
        manifest = platform.agents.get(slug, body.version)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent {slug!r} not found")
    if manifest.permissions:
        role = ctx.get("role", "member")
        missing = [p for p in manifest.permissions
                   if not platform.can(role, p)]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"installing {slug} needs {missing}; your role is {role!r}")
    try:
        return platform.install_agent(ctx["tenant"], slug, config=body.config)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


# --------------------------------------------------------------------------- #
# MCP bridge (HTTP transport, same tools as the stdio server)
# --------------------------------------------------------------------------- #
@router.get("/mcp/tools", summary="MCP tool descriptors")
async def mcp_tools(request: Request, ctx: CtxDep) -> dict[str, Any]:
    return {"tools": [t.descriptor() for t in _mcp_server(request, ctx).tools.values()],
            "transport": ["stdio: python -m src.mcp.server", "http: POST /v1/mcp"]}


@router.post("/mcp", summary="MCP JSON-RPC (initialize / tools/list / tools/call)")
async def mcp(request: Request, ctx: CtxDep, body: McpBody) -> dict[str, Any]:
    server = _mcp_server(request, ctx)
    response = server.handle(body.model_dump())
    if response is None:
        return {"jsonrpc": "2.0", "id": None, "result": {}}
    return response


def _mcp_server(request: Request, ctx: dict):
    from src.mcp.container import PlatformMcpAdapter
    from src.mcp.server import McpServer, build_tools

    adapter = PlatformMcpAdapter(_platform(request), tenant=ctx["tenant"])
    return McpServer(build_tools(adapter))
