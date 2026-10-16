"""Workflow engine: execute a graph, record every node, never lose the reason.

Execution rules that make the builder trustworthy:

* **Topological order** — a node runs once, when its dependencies are done.
* **Fail-fast by default, skip by choice.** A node that raises fails the run
  unless it is marked ``on_error: skip``; either way the node's error text is
  kept, so "why is the dataset empty" has an answer in the run log.
* **Bounded**: the run carries its own timeout and page budget; a workflow is
  not an excuse to crawl the internet.
* **Injectable collaborators.** Every side effect (discovery, crawl, extract,
  dedupe, dataset write, notify) arrives through :class:`WorkflowDeps`, so the
  engine is testable with fakes and the API can wire the real services.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from src.workflows.models import Node, NodeRun, Workflow, WorkflowRun

logger = logging.getLogger("wcp.workflows.engine")


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #
@dataclass
class WorkflowDeps:
    """Everything a node handler is allowed to touch.

    Each field is optional: a missing collaborator makes the node *skip* with
    a stated reason instead of failing, which is what lets the same workflow
    run in a laptop demo and in a full deployment.
    """

    discovery: Callable[..., Any] | None = None      # (topic, count) -> candidates
    crawl: Callable[..., Any] | None = None          # (urls, max_pages, ...) -> records
    extract: Callable[..., Any] | None = None        # (pages, recipe/schema) -> records
    clean: Callable[..., Any] | None = None          # (records) -> records
    dedupe: Callable[..., Any] | None = None         # (records, mode) -> records
    graph_write: Callable[..., Any] | None = None    # (records) -> entities
    dataset_write: Callable[..., Any] | None = None  # (dataset, records) -> version
    analysis: Callable[..., Any] | None = None       # (records, fields) -> summary
    report: Callable[..., Any] | None = None         # (dataset, records) -> markdown
    notify: Callable[..., Any] | None = None         # (event, payload) -> delivery
    webhook_post: Callable[..., Any] | None = None    # (url, payload) -> status
    export: Callable[..., Any] | None = None         # (dataset, fmt) -> (payload, ctype)
    recipes: Any = None
    now: Callable[[], float] | None = None
    max_seconds: float = 900.0
    max_pages: int = 5_000

    def time(self) -> float:
        return (self.now or time.time)()


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
Handler = Callable[[Node, "RunContext", WorkflowDeps], dict[str, Any]]


@dataclass
class RunContext:
    """Data flowing between nodes: the workflow's working memory."""

    topic: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    sites: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    entities: int = 0
    dataset: str = ""
    dataset_version: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    report_markdown: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    pages_fetched: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "candidates": len(self.candidates),
            "sites": len(self.sites),
            "pages": len(self.pages),
            "records": len(self.records),
            "entities": self.entities,
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "pages_fetched": self.pages_fetched,
            "artifacts": list(self.artifacts),
        }


def _require(dep: Any, name: str) -> None:
    if dep is None:
        raise SkipNode(f"no {name} collaborator is configured in this deployment")


class SkipNode(Exception):
    """Raised by a handler when the node cannot run here (recorded, not fatal)."""


def h_topic_discovery(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.discovery, "topic-discovery")
    topic = str(node.config.get("topic") or ctx.topic or "")
    if not topic:
        raise ValueError("topic_discovery needs a topic")
    ctx.topic = topic
    count = int(node.config.get("count", 25))
    result = deps.discovery(topic, count) or {}
    candidates = result.get("candidates", result) if isinstance(result, dict) else result
    ctx.candidates = list(candidates or [])
    return {"summary": f"{len(ctx.candidates)} candidate site(s) for {topic!r}"}


def h_website_selection(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    minimum = float(node.config.get("min_score", 0.0))
    limit = int(node.config.get("limit", 20))
    include = [s.lower() for s in (node.config.get("include") or [])]
    pool = ctx.candidates or ctx.sites
    chosen = []
    for row in pool:
        score = float(row.get("score", row.get("confidence", 0)) or 0)
        host = str(row.get("host", row.get("domain", row.get("url", "")))).lower()
        if include and not any(host.endswith(i) for i in include):
            continue
        if score < minimum:
            continue
        chosen.append(row)
    chosen = sorted(chosen, key=lambda r: -float(r.get("score", 0) or 0))[:limit]
    if not chosen and pool:
        chosen = list(pool)[:limit]
    ctx.sites = chosen
    return {"summary": f"selected {len(chosen)} site(s) (min score {minimum})"}


def h_crawler(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.crawl, "crawler")
    urls = [str(u) for u in (node.config.get("urls") or []) if u]
    if not urls:
        urls = [str(s.get("url") or s.get("host") or "") for s in ctx.sites]
    urls = [u for u in urls if u]
    if not urls:
        raise SkipNode("no sites selected to crawl")
    max_pages = min(int(node.config.get("max_pages", 100)), deps.max_pages)
    records = deps.crawl(urls, max_pages=max_pages,
                         stealth=bool(node.config.get("stealth", False)),
                         render_share=float(node.config.get("render_share", 0.0)),
                         priority=str(node.config.get("priority", "normal"))) or []
    pages = list(records) if isinstance(records, list) else list(
        records.get("pages", [])) if isinstance(records, dict) else []
    ctx.pages = pages
    ctx.pages_fetched = len(pages)
    if not ctx.records and node.config.get("keep_raw_pages", True):
        # A crawler with no extractor downstream still produces a usable raw
        # capture dataset: keeping the page row beats returning an empty run.
        ctx.records = [_raw_page_row(p) for p in pages]
    return {"summary": f"crawled {len(pages)} page(s) from {len(urls)} site(s)"}


def _raw_page_row(page: dict[str, Any]) -> dict[str, Any]:
    keep = ("url", "title", "text", "html", "status", "fetched_at", "content_hash",
            "provenance_id", "render_mode", "published_at", "host")
    row = {k: page[k] for k in keep if k in page}
    if "html" in row:
        row["html_chars"] = len(str(row.pop("html") or ""))
    if "text" in row:
        row["text"] = str(row["text"])[:4000]
    return row or {"url": page.get("url", "")}


def h_ai_extraction(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.extract, "extraction")
    pages = ctx.pages or []
    if not pages:
        raise SkipNode("nothing crawled to extract")
    records = deps.extract(pages, recipe=str(node.config.get("recipe", "")),
                           schema=node.config.get("schema"),
                           llm=bool(node.config.get("llm", False))) or []
    ctx.records = list(records)
    return {"summary": f"extracted {len(ctx.records)} record(s)"}


def h_data_cleaning(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    records = ctx.records or []
    if not records:
        raise SkipNode("no records to clean")
    if deps.clean is not None:
        ctx.records = list(deps.clean(records, normalize=bool(node.config.get(
            "normalize", True)), drop_empty=bool(node.config.get("drop_empty", True))) or [])
    else:
        ctx.records = [_clean_row(r) for r in records]
    return {"summary": f"{len(ctx.records)} record(s) after cleaning"}


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    import re

    out = {}
    for key, value in row.items():
        if isinstance(value, str):
            out[key] = re.sub(r"\s+", " ", value).strip()
        else:
            out[key] = value
    return {k: v for k, v in out.items() if v not in (None, "")}


def h_dedupe(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    records = ctx.records or []
    if not records:
        raise SkipNode("no records to dedupe")
    before = len(records)
    if deps.dedupe is not None:
        ctx.records = list(deps.dedupe(records, mode=str(node.config.get("mode", "exact")),
                                       threshold=float(node.config.get("threshold", 0.8)))
                           or [])
    else:
        ctx.records = _exact_dedupe(records)
    return {"summary": f"removed {before - len(ctx.records)} duplicate(s) of {before}"}


def _exact_dedupe(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for row in records:
        key = json.dumps({k: v for k, v in row.items()
                          if k not in ("fetched_at", "updated_at")}, sort_keys=True,
                         default=str)[:2000]
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def h_graph_write(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.graph_write, "knowledge graph")
    records = ctx.records or []
    if not records:
        raise SkipNode("no records to write into the graph")
    result = deps.graph_write(records, kinds=node.config.get("kinds"))
    entities = result.get("entities", 0) if isinstance(result, dict) else int(result or 0)
    ctx.entities = int(entities)
    return {"summary": f"{ctx.entities} entity touch(es) written to the graph"}


def h_dataset_write(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.dataset_write, "dataset store")
    records = ctx.records or []
    if not records:
        raise SkipNode("no records to store")
    name = str(node.config.get("dataset") or ctx.dataset or "workflow-output")
    result = deps.dataset_write(name, records,
                                primary_key=str(node.config.get("primary_key", "url")),
                                live=bool(node.config.get("live", True))) or {}
    ctx.dataset = name
    ctx.dataset_version = str(result.get("version", {}).get("version", "")
                              if isinstance(result.get("version"), dict)
                              else result.get("version", ""))
    changed = result.get("changed", True)
    return {"summary": f"dataset {name} {ctx.dataset_version or 'v?'} "
                       f"({len(records)} rows, changed={changed})"}


def h_analysis(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    records = ctx.records or []
    if not records:
        raise SkipNode("no records to analyse")
    if deps.analysis is not None:
        ctx.summary = dict(deps.analysis(records,
                                        numeric_fields=node.config.get("numeric_fields") or [],
                                        top=int(node.config.get("top", 5))) or {})
    else:
        from src.studio.service import summarise

        ctx.summary = summarise(records,
                                numeric_fields=node.config.get("numeric_fields") or [])
    numeric = ctx.summary.get("numeric", {})
    return {"summary": f"analysed {len(records)} row(s); "
                       f"{len(numeric)} numeric field(s)"}


def h_report(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    if deps.report is not None:
        ctx.report_markdown = str(deps.report(ctx.dataset or node.config.get("dataset", ""),
                                              ctx.records) or "")
    else:
        ctx.report_markdown = _fallback_report(ctx)
    ctx.artifacts["report"] = f"report:{ctx.dataset or 'workflow'}"
    return {"summary": f"report rendered ({len(ctx.report_markdown)} chars)"}


def _fallback_report(ctx: RunContext) -> str:
    lines = [f"# Workflow report — {ctx.topic or 'run'}", "",
             f"- Records: {len(ctx.records)}",
             f"- Dataset: {ctx.dataset or '—'} {ctx.dataset_version}",
             f"- Pages fetched: {ctx.pages_fetched}",
             f"- Entities: {ctx.entities}", ""]
    if ctx.summary.get("numeric"):
        lines += ["## Numeric summary", ""]
        for name, stats in ctx.summary["numeric"].items():
            lines.append(f"- **{name}**: min {stats['min']}, median {stats['median']}, "
                         f"max {stats['max']} (n={stats['count']})")
    if ctx.records:
        lines += ["", "## Sample rows", "",
                  "| " + " | ".join(list(ctx.records[0])[:6]) + " |",
                  "|" + "---|" * min(6, len(ctx.records[0]))]
        for row in ctx.records[:10]:
            cells = [str(row.get(k))[:40].replace("|", "\\|")
                     for k in list(row)[:6]]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def h_notify(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.notify, "notification")
    event = str(node.config.get("event") or "workflow.completed")
    payload = {"event": event, "message": str(node.config.get("message", "")),
               **ctx.snapshot()}
    deps.notify(event, payload)
    return {"summary": f"emitted {event}"}


def h_webhook(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.webhook_post, "webhook sender")
    url = str(node.config.get("url") or "")
    if not url:
        raise ValueError("webhook node needs a url")
    status = deps.webhook_post(url, {"event": node.config.get("event", "workflow.run"),
                                     **ctx.snapshot()})
    return {"summary": f"POST {url} -> {status}"}


def h_export(node: Node, ctx: RunContext, deps: WorkflowDeps) -> dict[str, Any]:
    _require(deps.export, "exporter")
    fmt = str(node.config.get("format", "csv"))
    dataset = str(node.config.get("dataset") or ctx.dataset or "")
    payload, ctype = deps.export(dataset, fmt)
    ctx.artifacts[f"export:{fmt}"] = f"export:{dataset}.{fmt}"
    return {"summary": f"exported {dataset} as {fmt} ({len(payload)} bytes, {ctype})"}


HANDLERS: dict[str, Handler] = {
    "topic_discovery": h_topic_discovery,
    "website_selection": h_website_selection,
    "crawler": h_crawler,
    "ai_extraction": h_ai_extraction,
    "data_cleaning": h_data_cleaning,
    "dedupe": h_dedupe,
    "graph_write": h_graph_write,
    "dataset_write": h_dataset_write,
    "analysis": h_analysis,
    "report": h_report,
    "notify": h_notify,
    "webhook": h_webhook,
    "export": h_export,
}


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def run_workflow(workflow: Workflow, deps: WorkflowDeps | None = None, *,
                 run: WorkflowRun | None = None,
                 on_node: Callable[[NodeRun, RunContext], None] | None = None
                 ) -> WorkflowRun:
    """Execute the workflow once and return a full, inspectable run record."""
    deps = deps or WorkflowDeps()
    problems = workflow.validate()
    fatal = [p for p in problems if "terminal" not in p]
    run = run or WorkflowRun(workflow_id=workflow.workflow_id, tenant=workflow.tenant)
    if fatal:
        run.state = "failed"
        run.error = "; ".join(fatal)
        run.finished_at = deps.time()
        return run

    ctx = RunContext()
    deadline = deps.time() + deps.max_seconds
    try:
        order = workflow.topological_order()
    except ValueError as exc:
        run.state = "failed"
        run.error = str(exc)
        run.finished_at = deps.time()
        return run

    for node in order:
        node_run = NodeRun(node_id=node.id, type=node.type)
        started = deps.time()
        if started > deadline:
            node_run.status = "skipped"
            node_run.error = "run deadline reached"
            run.nodes.append(node_run)
            if on_node:
                on_node(node_run, ctx)
            continue
        handler = HANDLERS.get(node.type)
        try:
            if handler is None:
                raise SkipNode(f"no handler for node type {node.type!r}")
            output = handler(node, ctx, deps) or {}
            node_run.status = "ok"
            node_run.output_summary = str(output.get("summary", ""))[:300]
        except SkipNode as exc:
            node_run.status = "skipped"
            node_run.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one node must not kill the API
            logger.warning("workflow %s node %s failed: %s",
                           workflow.workflow_id, node.id, exc)
            node_run.status = "failed"
            node_run.error = f"{type(exc).__name__}: {exc}"
            if node.on_error != "skip":
                run.nodes.append(node_run)
                run.state = "failed"
                run.error = f"{node.id} ({node.type}): {node_run.error}"
                run.finished_at = deps.time()
                if on_node:
                    on_node(node_run, ctx)
                return run
        node_run.ms = round((deps.time() - started) * 1000.0, 1)
        run.nodes.append(node_run)
        if on_node:
            on_node(node_run, ctx)

    run.outputs = ctx.snapshot()
    run.outputs["summary"] = ctx.summary.get("numeric", {})
    if ctx.report_markdown:
        run.outputs["report_markdown"] = ctx.report_markdown[:20_000]
    run.state = ("ok" if all(n.status != "failed" for n in run.nodes)
                 else "partial")
    run.finished_at = deps.time()
    return run


def dry_run(workflow: Workflow) -> dict[str, Any]:
    """Validate and describe a workflow without touching anything."""
    problems = workflow.validate()
    order = []
    if not problems:
        order = [{"id": n.id, "type": n.type, "config": n.config}
                 for n in workflow.topological_order()]
    return {
        "workflow_id": workflow.workflow_id,
        "valid": not problems,
        "problems": problems,
        "order": order,
        "nodes": len(workflow.nodes),
        "edges": len(workflow.edges),
        "schedule": workflow.schedule,
    }
