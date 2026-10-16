"""Console pages for the Web Intelligence OS surface.

The original console exposed the crawler. These pages expose the rest of the
platform — answered questions with citations, change monitoring, the knowledge
graph, living datasets, workflows, recipes, the cost simulator, the provenance
ledger, the agent marketplace, roles/audit, MCP and the crawl doctor — using
the same rules as the pages that came before:

* **Server-rendered, no build step.** Every page is a document that works
  before any JavaScript runs; the script only adds transitions, charts and the
  palette.
* **Reads go through the platform services, in-process.** These are views of
  data we already hold, so a loopback HTTP call per page would be ceremony.
  Every *write* below is one call on the same service the API router uses, so
  the console cannot drift from the API contract.
* **Degrade, never explode.** A missing index, an empty ledger or an
  unconfigured model shows a stated empty state — never a 500 and never a fake
  number.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from src.webui.app import _guard, _redirect, render

logger = logging.getLogger("wcp.webui.pages")
router = APIRouter(include_in_schema=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _platform(request: Request):
    """The platform services, or ``None`` when the console is remote-only."""
    services = getattr(request.app.state, "services", None)
    return getattr(services, "platform", None) if services else None


def _safe(fn, *args, default=None, **kwargs):
    """Call a service method; a failure becomes a stated default, not a 500.

    This is the console's whole error strategy: an unreachable subsystem shows
    an empty state with the reason attached, because a page that half-renders
    is more useful than a traceback.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:                      # noqa: BLE001 - page must render
        logger.warning("console service call %s failed: %s",
                       getattr(fn, "__qualname__", fn), exc)
        return default


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("items", "rows", "data", "results", "entities", "events"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, dict)]
    return []


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value if isinstance(value, dict) else {}


KIND_STYLE = {
    "price_change": "gold", "new_product": "cyan", "announcement": "cyan",
    "policy_change": "danger", "jobs_change": "warn", "content_update": "",
    "removed_product": "danger", "cosmetic": "muted",
}


def _tone(kind: str) -> str:
    return KIND_STYLE.get(kind, "")


# --------------------------------------------------------------------------- #
# Overview: dashboard + system health
# --------------------------------------------------------------------------- #
@router.get("/app/system", response_class=HTMLResponse, name="system")
async def system_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    health = _safe(platform.health, tenant, default={}) if platform else {}
    counters = _safe(platform.doctor.counters, tenant, default={}) if platform else {}
    report = _safe(platform.doctor.report, tenant, default=None) if platform else None
    spans = _safe(platform.doctor.recent, tenant, 60, default=[]) if platform else []
    return render(request, "system.html", title="System health · Web Intelligence OS",
                  page="system", me=me, health=health or {},
                  counters=counters or {}, report=_dump(report) if report else {},
                  spans=[_dump(s) for s in spans])


# --------------------------------------------------------------------------- #
# Intelligence: ask (cited answers) and the research agent
# --------------------------------------------------------------------------- #
@router.get("/app/intelligence", response_class=HTMLResponse, name="intelligence")
async def intelligence_page(request: Request,
                            q: Annotated[str, Query(max_length=400)] = "",
                            tab: Annotated[str, Query()] = "ask"):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    result: dict[str, Any] = {}
    if q and platform is not None:
        if tab == "research":
            result = _safe(platform.research, q, default={}) or {}
        else:
            result = _safe(platform.ask, q, default={}) or {}
    return render(request, "intelligence.html", title="Answer & research · Web Intelligence OS",
                  page="intelligence", me=me, tab=tab, question=q,
                  result=result or {}, plan=_dump(_safe(platform.ask_plan, q,
                                                        default=None) if q and platform
                                                  else None),
                  examples=[
                      "What did the RBI say about digital lending in 2025?",
                      "Which Bangladeshi e-commerce sites sell the Pixel 10 and at "
                      "what price?",
                      "How has the price of the Pro plan changed this quarter?",
                  ])


@router.post("/app/intelligence/ask")
async def intelligence_ask(request: Request,
                           question: Annotated[str, Form(max_length=400)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    return _redirect("/app/intelligence?tab=ask&q=" + _quote(question))


@router.post("/app/intelligence/research")
async def intelligence_research(request: Request,
                                question: Annotated[str, Form(max_length=400)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    return _redirect("/app/intelligence?tab=research&q=" + _quote(question))


def _quote(text: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(text or "")


# --------------------------------------------------------------------------- #
# Change monitoring
# --------------------------------------------------------------------------- #
@router.get("/app/monitor", response_class=HTMLResponse, name="monitor")
async def monitor_page(request: Request,
                       watch: Annotated[str, Query(max_length=80)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    from src.watch.diff import EVENT_KINDS

    watches = [_dump(w) for w in _safe(platform.watches.list, tenant, default=[]) or []]
    stats = _safe(platform.watches.stats, tenant, default={}) or {}
    events = [_dump(e) for e in _safe(platform.watches.events, tenant, limit=60,
                                      default=[]) or []]
    selected = _dump(_safe(platform.watches.get, tenant, watch, default=None)) if (
        platform and watch) else {}
    return render(request, "monitor.html", title="Change monitoring · Web Intelligence OS",
                  page="monitor", me=me, watches=watches, stats=stats, events=events,
                  selected=selected, kinds=list(EVENT_KINDS), tone=_tone)


@router.post("/app/monitor")
async def monitor_create(request: Request,
                         name: Annotated[str, Form(max_length=160)] = "",
                         urls: Annotated[str, Form()] = "",
                         goal: Annotated[str, Form(max_length=400)] = "",
                         schedule: Annotated[str, Form(max_length=40)] = "1h",
                         min_significance: Annotated[float, Form()] = 0.35,
                         kinds: Annotated[list[str] | None, Form()] = None):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    targets = [line.strip() for line in (urls or "").replace(",", "\n").splitlines()
               if line.strip()][:50]
    try:
        watch = platform.watches.create(tenant, name or "Untitled watch", targets,
                                        goal=goal, kinds=kinds or None,
                                        schedule=schedule,
                                        min_significance=min_significance)
        return _redirect(f"/app/monitor?watch={watch.watch_id}",
                         "Monitor created — first run scheduled", "ok")
    except Exception as exc:                       # noqa: BLE001
        return _redirect("/app/monitor", f"Could not create the monitor: {exc}", "danger")


@router.post("/app/monitor/{watch_id}/run")
async def monitor_run(request: Request, watch_id: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    try:
        result = platform.watches.run(tenant, watch_id)
        changed = len(result.get("events") or [])
        return _redirect(f"/app/monitor?watch={watch_id}",
                         f"Checked {result.get('checked', 0)} target(s); "
                         f"{changed} change event(s) recorded"
                         if changed else "Checked — nothing changed",
                         "ok" if changed else "warn")
    except Exception as exc:                       # noqa: BLE001
        return _redirect(f"/app/monitor?watch={watch_id}",
                         f"Check failed: {exc}", "danger")


@router.post("/app/monitor/{watch_id}/pause")
async def monitor_pause(request: Request, watch_id: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    current = _safe(platform.watches.get, tenant, watch_id, default=None) if platform else None
    state = "paused" if getattr(current, "state", "") == "active" else "active"
    _safe(platform.watches.update, tenant, watch_id, state=state)
    return _redirect(f"/app/monitor?watch={watch_id}",
                     f"Monitor {state}", "ok" if state == "active" else "warn")


# --------------------------------------------------------------------------- #
# Knowledge graph
# --------------------------------------------------------------------------- #
@router.get("/app/graph", response_class=HTMLResponse, name="graph")
async def graph_page(request: Request,
                     q: Annotated[str, Query(max_length=120)] = "",
                     kind: Annotated[str, Query(max_length=24)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    stats = _safe(platform.graph.store.stats, tenant, default={}) if platform else {}
    entities = _safe(platform.graph.store.entities, tenant, limit=40, kind=kind,
                     default=[]) if platform else []
    focus = _safe(platform.graph.store.find, tenant, q, default=None) if (
        platform and q) else None
    subgraph = _safe(platform.graph.store.subgraph, tenant, focus.entity_id,
                     depth=2, limit=40, default={}) if focus else {}
    return render(request, "graph.html", title="Knowledge graph · Web Intelligence OS",
                  page="graph", me=me, stats=stats or {}, query=q, kind=kind,
                  entities=[_dump(e) for e in entities],
                  focus=_dump(focus) if focus else {},
                  subgraph=subgraph or {},
                  layout=_layout_nodes(subgraph or {}))


def _layout_nodes(subgraph: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Deterministic circle layout — no physics library, same picture every time."""
    import math

    nodes = subgraph.get("nodes") or []
    root = subgraph.get("root") or ""
    positions: dict[str, dict[str, float]] = {}
    ring = [n for n in nodes if n.get("entity_id") != root]
    for index, node in enumerate(ring):
        angle = (index / max(1, len(ring))) * 2 * math.pi - math.pi / 2
        positions[node["entity_id"]] = {
            "x": round(430 + 300 * math.cos(angle), 1),
            "y": round(170 + 128 * math.sin(angle), 1),
        }
    if root:
        positions[root] = {"x": 430.0, "y": 170.0}
    return positions


# --------------------------------------------------------------------------- #
# Living datasets
# --------------------------------------------------------------------------- #
@router.get("/app/datasets", response_class=HTMLResponse, name="datasets")
async def datasets_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    datasets = [_dump(d) for d in _safe(platform.datasets.list, tenant, limit=100,
                                        default=[]) or []]
    stats = _safe(platform.datasets.stats, tenant, default={}) or {}
    return render(request, "datasets.html", title="Living datasets · Web Intelligence OS",
                  page="datasets", me=me, datasets=datasets, stats=stats)


@router.post("/app/datasets")
async def datasets_create(request: Request,
                          name: Annotated[str, Form(max_length=120)] = "",
                          description: Annotated[str, Form(max_length=500)] = "",
                          primary_key: Annotated[str, Form(max_length=40)] = "url",
                          licence: Annotated[str, Form(max_length=80)] = "",
                          refresh: Annotated[str, Form(max_length=40)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    try:
        dataset = platform.datasets.create(tenant, name, description=description,
                                           primary_key=primary_key or "url",
                                           kind="custom")
        _redirect_note = ""
        watch_seconds = 0
        if refresh:
            from src.watch.models import parse_schedule

            watch_seconds = parse_schedule(refresh)
        return _redirect("/app/datasets",
                         f"Dataset {dataset.name} created"
                         + (f" (you asked for a {watch_seconds}s refresh — attach a "
                            f"monitor on Change monitoring)" if watch_seconds else ""),
                         "ok")
    except Exception as exc:                       # noqa: BLE001
        return _redirect("/app/datasets", f"Could not create: {exc}", "danger")


@router.get("/app/datasets/{ref:path}", response_class=HTMLResponse, name="dataset")
async def dataset_page(request: Request, ref: str,
                       version: Annotated[str, Query(max_length=40)] = "",
                       q: Annotated[str, Query(max_length=400)] = "",
                       compare: Annotated[str, Query(max_length=40)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    dataset = _safe(platform.datasets.get, tenant, ref, default=None) if platform else None
    if dataset is None:
        return _redirect("/app/datasets", f"No dataset {ref!r}", "warn")
    rows = _safe(platform.datasets.rows, dataset, version or None, limit=200,
                 default=[]) or []
    from src.studio.service import quality_report

    quality = _safe(quality_report, rows, key=dataset.primary_key, default={}) or {}
    diff = _safe(platform.datasets.diff, dataset, compare, version, default={}) if (
        compare or version) else {}
    answer = _safe(platform.ask_dataset, tenant, dataset.name, q, version=version,
                   default={}) if (q and platform) else {}
    from src.studio.nlq import columns as _cols

    return render(request, "dataset.html", title=f"{dataset.name} · Data Studio",
                  page="datasets", me=me, dataset=_dump(dataset), rows=rows,
                  quality=quality, diff=_dump(diff), question=q, answer=answer or {},
                  columns=_cols(rows), version=version or (
                      dataset.latest.version if dataset.latest else ""))


@router.post("/app/datasets/{ref:path}/ask")
async def dataset_ask(request: Request, ref: str,
                      question: Annotated[str, Form(max_length=400)] = "",
                      version: Annotated[str, Form(max_length=40)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    return _redirect(f"/app/datasets/{ref}?version={version}&q={_quote(question)}")


@router.get("/app/datasets/{ref:path}/export.{fmt}", name="dataset_export")
async def dataset_export(request: Request, ref: str, fmt: str,
                         version: Annotated[str, Query(max_length=40)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    dataset = _safe(platform.datasets.get, tenant, ref, default=None) if platform else None
    if dataset is None:
        return _redirect("/app/datasets", f"No dataset {ref!r}", "warn")
    fmt = fmt if fmt in ("csv", "json", "jsonl", "markdown") else "csv"
    payload, ctype = platform.datasets.export(dataset, version=version or None, fmt=fmt)
    return PlainTextResponse(payload, media_type=ctype)


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #
@router.get("/app/workflows", response_class=HTMLResponse, name="workflows")
async def workflows_page(request: Request,
                         workflow: Annotated[str, Query(max_length=80)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    from src.workflows.models import node_catalogue, template_list

    workflows = [_dump(w) for w in _safe(platform.workflows.list, tenant, default=[])
                 or []] if platform else []
    runs = [_dump(r) for r in _safe(platform.workflows.runs, tenant, limit=25,
                                    default=[]) or []] if platform else []
    selected = _safe(platform.workflows.get, tenant, workflow, default=None) if (
        platform and workflow) else None
    preview = (_safe(platform.workflow_preview, workflow, tenant=tenant, default=None)
               if selected else None)
    return render(request, "workflows.html", title="Workflows · Web Intelligence OS",
                  page="workflows", me=me, workflows=workflows, runs=runs,
                  templates=template_list(), catalogue=node_catalogue(),
                  selected=_dump(selected) if selected else {},
                  preview=preview or {})


@router.post("/app/workflows")
async def workflow_create(request: Request,
                          template: Annotated[str, Form(max_length=40)] = "",
                          name: Annotated[str, Form(max_length=140)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    from src.workflows.models import Workflow, new_blank, template as build_template

    try:
        if template:
            payload = build_template(template)
            payload["name"] = name or payload["name"]
            payload["tenant"] = tenant
            workflow = Workflow.from_dict(payload)
        else:
            workflow = new_blank(name or "Untitled workflow", tenant)
        platform.workflows.save(workflow)
        return _redirect(f"/app/workflows?workflow={workflow.workflow_id}",
                         f"Workflow {workflow.name!r} created", "ok")
    except Exception as exc:                       # noqa: BLE001
        return _redirect("/app/workflows", f"Could not create: {exc}", "danger")


@router.post("/app/workflows/{workflow_id}/run")
async def workflow_run(request: Request, workflow_id: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    try:
        run = platform.run_workflow(tenant, workflow_id)
        return _redirect(f"/app/workflows?workflow={workflow_id}",
                         f"Run {run.get('state')}: {run.get('ok', 0)} node(s) ok, "
                         f"{run.get('skipped', 0)} skipped, {run.get('failed', 0)} failed",
                         "ok" if run.get("state") == "ok" else "warn")
    except Exception as exc:                       # noqa: BLE001
        return _redirect(f"/app/workflows?workflow={workflow_id}",
                         f"Run failed: {exc}", "danger")


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #
@router.get("/app/recipes", response_class=HTMLResponse, name="recipes")
async def recipes_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    recipes = _safe(platform.recipes_list, default=[]) if platform else []
    stats = _safe(platform.recipes.stats, default={}) if platform else {}
    hosts = sorted({host for row in recipes for host in row.get("hosts", [])})
    return render(request, "recipes.html", title="Recipes · Web Intelligence OS",
                  page="recipes", me=me, recipes=recipes or [], stats=stats or {},
                  hosts=hosts)


@router.post("/app/recipes/seed")
async def recipes_seed(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    from src.recipes.library import builtin_recipes

    installed = []
    for recipe in builtin_recipes():
        result = platform.recipes.install(recipe, source="library", verify=True)
        if getattr(result, "ok", True):
            installed.append(recipe.ref)
    return _redirect("/app/recipes",
                     f"Installed {len(installed)} library recipe(s)" if installed
                     else "Nothing to install", "ok" if installed else "warn")


@router.post("/app/recipes/{name}/test")
async def recipe_test(request: Request, name: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    result = _safe(platform.recipe_test, name, default={}) or {}
    verdict = result.get("verdict", "unknown")
    return _redirect("/app/recipes", f"{name}: fixtures {verdict} "
                                     f"({result.get('passed', 0)} passed, "
                                     f"{result.get('failed', 0)} failed)",
                     "ok" if verdict == "pass" else "warn")


# --------------------------------------------------------------------------- #
# Cost simulator, optimiser, crawl doctor
# --------------------------------------------------------------------------- #
@router.get("/app/engine", response_class=HTMLResponse, name="engine")
async def engine_page(request: Request,
                      pages: Annotated[int, Query(ge=1, le=50_000_000)] = 100_000,
                      render_share: Annotated[float, Query(ge=0, le=1)] = 0.3,
                      stealth: Annotated[bool, Query()] = False,
                      tab: Annotated[str, Query(max_length=20)] = "cost"):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    estimate = _safe(platform.cost_estimate,
                     {"pages": pages, "render_share": render_share, "stealth": stealth},
                     default={}) if platform else {}
    presets = _safe(platform.cost_presets, default=[]) if platform else []
    signals = [{"url": "https://example.test/a", "change_rate": 0.8,
                "last_fetched_at": 0, "important": True, "needs_render": False},
               {"url": "https://example.test/b", "change_rate": 0.05,
                "last_fetched_at": 0, "needs_render": True}]
    plan = _safe(platform.plan_crawl, signals, budget_pages=min(pages, 20_000),
                 default={}) if platform else {}
    report = _dump(_safe(platform.doctor.report, tenant, default=None)) if platform else {}
    counters = _safe(platform.doctor.counters, tenant, default={}) if platform else {}
    spans = [_dump(s) for s in (_safe(platform.doctor.recent, tenant, 40, default=[])
                                or [])] if platform else []
    return render(request, "engine.html", title="Cost & optimiser · Web Intelligence OS",
                  page="engine", me=me, tab=tab, pages=pages,
                  render_share=render_share, stealth=stealth, estimate=estimate or {},
                  presets=presets or [], plan=plan or {}, report=report,
                  counters=counters or {}, spans=spans)


# --------------------------------------------------------------------------- #
# Provenance & time machine
# --------------------------------------------------------------------------- #
@router.get("/app/provenance", response_class=HTMLResponse, name="provenance")
async def provenance_page(request: Request,
                          url: Annotated[str, Query(max_length=400)] = "",
                          dataset: Annotated[str, Query(max_length=120)] = ""):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    ledger = platform.ledger if platform else None
    records = [_dump(r) for r in (_safe(ledger.for_url, tenant, url, limit=50,
                                        default=[]) if url and ledger
                                  else _safe(ledger.recent, tenant, limit=50,
                                             default=[]) if ledger else []) or []]
    stats = _dump(_safe(ledger.stats, tenant, default=None)) if ledger else {}
    counts = _safe(ledger.counts, tenant, default={}) if ledger else {}
    head = _safe(ledger.head, tenant, default="") if ledger else ""
    manifest = _safe(platform.provenance_manifest, dataset, default={}) if (
        platform and dataset) else {}
    return render(request, "provenance.html",
                  title="Provenance & replay · Web Intelligence OS",
                  page="provenance", me=me, records=records, stats=stats or {},
                  counts=counts or {}, head=head, url=url, dataset=dataset,
                  manifest=manifest or {})


@router.post("/app/provenance/verify")
async def provenance_verify(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    result = _safe(platform.ledger.verify, tenant, default={}) if platform else {}
    ok = result.get("ok", result.get("valid", False))
    return _redirect("/app/provenance",
                     f"Ledger verified: {result.get('records', '?')} record(s), "
                     f"hash chain {'intact' if ok else 'BROKEN at ' + str(result.get('broken_at', '?'))}",
                     "ok" if ok else "danger")


# --------------------------------------------------------------------------- #
# Agents, team, MCP
# --------------------------------------------------------------------------- #
@router.get("/app/agents", response_class=HTMLResponse, name="agents")
async def agents_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    market = platform.agents if platform else None
    return render(request, "agents.html", title="AI agents · Web Intelligence OS",
                  page="agents", me=me,
                  catalogue=_safe(market.catalog, default=[]) if market else [],
                  installed=_safe(market.installed, tenant, default=[]) if market else [],
                  revenue=_safe(market.revenue, default={}) if market else {},
                  stats=_safe(market.stats, default={}) if market else {})


@router.post("/app/agents/{slug}/install")
async def agent_install(request: Request, slug: str):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    try:
        result = platform.install_agent(tenant, slug)
        return _redirect("/app/agents", f"Installed {slug}: "
                                        f"{result.get('note', 'ready')}", "ok")
    except Exception as exc:                       # noqa: BLE001
        return _redirect("/app/agents", f"Could not install {slug}: {exc}", "danger")


@router.get("/app/team", response_class=HTMLResponse, name="team")
async def team_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    platform = _platform(request)
    tenant = me.get("tenant") or "public"
    return render(request, "team.html", title="Team, roles & audit · Web Intelligence OS",
                  page="team", me=me,
                  matrix=_safe(platform.rbac_matrix, default=[]) if platform else [],
                  audit=[_dump(e) for e in (_safe(platform.audit.recent, tenant, limit=60,
                                                  default=[]) or [])] if platform else [],
                  counts=_safe(platform.audit.counts_by_action, tenant, default={})
                  if platform else {},
                  policy=_dump(_safe(platform.retention_policy, tenant, default=None))
                  if platform else {},
                  retention=_safe(platform.retention_plan, tenant, default=[])
                  if platform else [],
                  usage=_safe(platform.usage, tenant, default={}) if platform else {})


@router.get("/app/mcp", response_class=HTMLResponse, name="mcp")
async def mcp_page(request: Request):
    api, me, redirect = await _guard(request)
    if redirect:
        return redirect
    from src.mcp.server import build_tools

    class _Probe:
        pass

    tools: list[dict[str, Any]] = []
    try:
        for tool in build_tools(_Probe()):
            tools.append({"name": getattr(tool, "name", ""),
                          "description": getattr(tool, "description", ""),
                          "schema": getattr(tool, "input_schema", None)
                          or getattr(tool, "parameters", {})})
    except Exception as exc:                       # noqa: BLE001
        logger.debug("tool probe failed: %s", exc)
    base = str(request.base_url).rstrip("/")
    return render(request, "mcp.html", title="MCP & SDKs · Web Intelligence OS",
                  page="mcp", me=me, tools=tools, base=base, token=current_token(request))


# --------------------------------------------------------------------------- #
# Public domain score page (no session required — this one is a front door)
# --------------------------------------------------------------------------- #
@router.get("/domain/{host}")
def public_domain(request: Request, host: str):
    """The public domain-score page: linkable, indexable, no session required.

    The score itself comes from the committed scorer (own index + provenance
    ledger), so this page can never disagree with the API twin — it just renders
    the same dict with questions answered in prose instead of JSON.
    """
    from src.search_index.domain_score import WEIGHTS, grade_for

    platform = _platform(request)
    host = (host or "").lower().strip("/")
    score = _safe(platform.domain_score, host, default={}) if platform else {}

    compare_host = (request.query_params.get("compare") or "").lower().strip("/")
    comparison: dict[str, Any] = {}
    if compare_host and compare_host != host and platform:
        other = _safe(platform.domain_score, compare_host, default={}) or {}
        components = sorted(WEIGHTS)
        rows = []
        for key in components:
            a = float((score.get("components") or {}).get(key, 0.0) or 0.0)
            b = float((other.get("components") or {}).get(key, 0.0) or 0.0)
            rows.append({"component": key, "a": round(a, 4), "b": round(b, 4),
                         "delta": round(a - b, 4)})
        comparison = {
            "a": {"host": score.get("host") or host, "score": score.get("score")},
            "b": {"host": other.get("host") or compare_host,
                  "score": other.get("score")},
            "rows": rows,
        }

    components = score.get("components") or {}
    breakdown = [
        {
            "component": key,
            "value": float(components.get(key, 0.0) or 0.0),
            "weight": WEIGHTS[key],
            "points": round(100.0 * WEIGHTS[key] * float(components.get(key, 0.0) or 0.0), 2),
        }
        for key in WEIGHTS if key in components
    ]
    breakdown.sort(key=lambda row: row["points"], reverse=True)
    grade = score.get("grade") or (grade_for(score["score"]) if score.get("score") is not None else "—")

    return render(request, "domain.html", title=f"Domain score: {host}", page="",
                  me=None, host=host, score=score or {}, grade=grade,
                  comparison=comparison, compare=compare_host,
                  breakdown=breakdown)



def current_token(request: Request) -> str:
    from src.webui.session import current_token as _current

    return _current(request)
