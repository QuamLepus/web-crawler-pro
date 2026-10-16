"""Visual workflow builder — the automation layer, as data.

A workflow is a small JSON graph of typed nodes:

    Topic discovery → Website selection → Crawler → AI extraction
        → Cleaning → Dedupe → Dataset → Analysis → Notify

The document is the contract; the builder in the console edits it, the API
validates and stores it, and the engine executes it. Keeping the graph as data
(rather than as Python) buys three things that matter: workflows are diffable
and reviewable, they can be exported/imported as templates, and a node's
behaviour can be tested without a UI.

Node types are a fixed vocabulary on purpose. A "run arbitrary code" node
would be a remote-code-execution endpoint with a login page.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

NODE_TYPES: dict[str, dict[str, Any]] = {
    "topic_discovery": {
        "label": "Topic discovery",
        "produces": ["candidates"],
        "config": {"topic": "string", "count": "int"},
        "description": "Turn a topic sentence into a ranked website shortlist.",
    },
    "website_selection": {
        "label": "Website selection",
        "produces": ["sites"],
        "config": {"min_score": "float", "limit": "int", "include": "list[str]"},
        "description": "Filter the shortlist: keep the sites worth crawling.",
    },
    "crawler": {
        "label": "Crawler",
        "produces": ["pages"],
        "config": {"max_pages": "int", "stealth": "bool", "render_share": "float",
                   "priority": "str"},
        "description": "Fetch the selected sites and collect pages.",
    },
    "ai_extraction": {
        "label": "AI extraction",
        "produces": ["records"],
        "config": {"recipe": "string", "schema": "object", "llm": "bool"},
        "description": "Apply a recipe (or an LLM schema) to each page.",
    },
    "data_cleaning": {
        "label": "Data cleaning",
        "produces": ["records"],
        "config": {"normalize": "bool", "trim_fields": "list[str]",
                   "drop_empty": "bool"},
        "description": "Normalise whitespace/unicode, canonicalise URLs.",
    },
    "dedupe": {
        "label": "Duplicate removal",
        "produces": ["records"],
        "config": {"mode": "str", "threshold": "float"},
        "description": "MinHash/SimHash near-duplicate removal.",
    },
    "graph_write": {
        "label": "Knowledge graph",
        "produces": ["entities"],
        "config": {"kinds": "list[str]"},
        "description": "Extract entities and relations into the graph.",
    },
    "dataset_write": {
        "label": "Dataset",
        "produces": ["dataset"],
        "config": {"dataset": "string", "primary_key": "string", "live": "bool"},
        "description": "Append a version to a living dataset.",
    },
    "analysis": {
        "label": "AI analysis",
        "produces": ["summary"],
        "config": {"numeric_fields": "list[str]", "top": "int"},
        "description": "Statistical digest / LLM summary of the collected rows.",
    },
    "report": {
        "label": "Report",
        "produces": ["markdown"],
        "config": {"dataset": "string", "include_provenance": "bool"},
        "description": "Render a markdown report with the dataset and its passport.",
    },
    "notify": {
        "label": "Notify",
        "produces": [],
        "config": {"event": "string", "message": "string"},
        "description": "Emit a webhook/notification event.",
    },
    "webhook": {
        "label": "Webhook",
        "produces": [],
        "config": {"url": "string", "event": "string"},
        "description": "POST a payload to an external endpoint.",
    },
    "export": {
        "label": "Export",
        "produces": ["artifact"],
        "config": {"dataset": "string", "format": "csv|json|jsonl|markdown"},
        "description": "Write the dataset out in a chosen format.",
    },
}

TERMINAL_TYPES = ("notify", "webhook", "export")


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    id: str
    type: str
    config: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    position: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    on_error: str = "fail"          # fail | skip

    def __post_init__(self) -> None:
        if self.type not in NODE_TYPES:
            raise ValueError(f"unknown node type {self.type!r}")
        if not self.label:
            self.label = NODE_TYPES[self.type]["label"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Node:
        return cls(id=row.get("id") or f"n{uuid.uuid4().hex[:6]}",
                   type=row.get("type", ""), config=dict(row.get("config") or {}),
                   label=row.get("label", ""),
                   position=dict(row.get("position") or {"x": 0.0, "y": 0.0}),
                   on_error=row.get("on_error", "fail"))


@dataclass
class Edge:
    source: str
    target: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Workflow:
    name: str
    tenant: str
    workflow_id: str = ""
    description: str = ""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    schedule: str = ""              # "" = manual only
    state: str = "draft"            # draft | active | paused
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_run_at: float | None = None
    runs: int = 0

    def __post_init__(self) -> None:
        if not self.workflow_id:
            self.workflow_id = "wf_" + uuid.uuid4().hex[:14]
        self.nodes = [n if isinstance(n, Node) else Node.from_dict(n) for n in self.nodes]
        self.edges = [e if isinstance(e, Edge) else Edge(**e) for e in self.edges]

    # -- graph helpers ------------------------------------------------------- #
    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def successors(self, node_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == node_id]

    def predecessors(self, node_id: str) -> list[str]:
        return [e.source for e in self.edges if e.target == node_id]

    def entry_nodes(self) -> list[Node]:
        targets = {e.target for e in self.edges}
        return [n for n in self.nodes if n.id not in targets]

    def topological_order(self) -> list[Node]:
        """Kahn's algorithm; raises on a cycle (a workflow must terminate)."""
        indegree = {n.id: 0 for n in self.nodes}
        for edge in self.edges:
            if edge.target in indegree and edge.source in indegree:
                indegree[edge.target] += 1
        ready = [n for n in self.nodes if indegree.get(n.id, 0) == 0]
        order: list[Node] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for nxt in self.successors(current.id):
                if nxt not in indegree:      # dangling edge: validate() reports it
                    continue
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    node = self.node(nxt)
                    if node:
                        ready.append(node)
        if len(order) != len(self.nodes):
            raise ValueError("workflow contains a cycle")
        return order

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.nodes:
            problems.append("workflow has no nodes")
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            problems.append("duplicate node ids")
        known = set(ids)
        for edge in self.edges:
            if edge.source not in known:
                problems.append(f"edge from unknown node {edge.source!r}")
            if edge.target not in known:
                problems.append(f"edge to unknown node {edge.target!r}")
            if edge.source == edge.target:
                problems.append(f"self-loop on {edge.source!r}")
        try:
            self.topological_order()
        except ValueError as exc:
            problems.append(str(exc))
        if not any(n.type in TERMINAL_TYPES or n.type == "dataset_write"
                   for n in self.nodes):
            problems.append("no terminal node (notify/webhook/export/dataset_write): "
                            "the run would produce nothing observable")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "tenant": self.tenant,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "schedule": self.schedule,
            "state": self.state,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
            "last_run_at": self.last_run_at,
            "runs": self.runs,
            "node_count": len(self.nodes),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Workflow:
        return cls(
            name=row.get("name", ""), tenant=row.get("tenant", ""),
            workflow_id=row.get("workflow_id", ""),
            description=row.get("description", ""),
            nodes=[Node.from_dict(n) for n in row.get("nodes") or []],
            edges=[Edge(**e) for e in row.get("edges") or []],
            schedule=row.get("schedule", ""), state=row.get("state", "draft"),
            created_at=float(row.get("created_at") or time.time()),
            updated_at=float(row.get("updated_at") or time.time()),
            last_run_at=row.get("last_run_at"), runs=int(row.get("runs") or 0),
        )


@dataclass
class NodeRun:
    node_id: str
    type: str
    status: str = "pending"      # pending | ok | skipped | failed
    ms: float = 0.0
    output_summary: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowRun:
    workflow_id: str
    run_id: str = ""
    tenant: str = ""
    state: str = "running"       # running | ok | failed | partial
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    nodes: list[NodeRun] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = "run_" + uuid.uuid4().hex[:14]

    @property
    def seconds(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id, "run_id": self.run_id,
            "tenant": self.tenant, "state": self.state,
            "started_at": round(self.started_at, 3),
            "finished_at": round(self.finished_at, 3) if self.finished_at else None,
            "seconds": round(self.seconds, 2),
            "nodes": [n.to_dict() for n in self.nodes],
            "outputs": self.outputs, "error": self.error,
            "ok": sum(1 for n in self.nodes if n.status == "ok"),
            "skipped": sum(1 for n in self.nodes if n.status == "skipped"),
            "failed": sum(1 for n in self.nodes if n.status == "failed"),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> WorkflowRun:
        run = cls(workflow_id=row.get("workflow_id", ""),
                  run_id=row.get("run_id", ""), tenant=row.get("tenant", ""),
                  state=row.get("state", "running"))
        run.started_at = float(row.get("started_at") or time.time())
        run.finished_at = row.get("finished_at")
        run.nodes = [n if isinstance(n, NodeRun) else NodeRun(**n)
                     for n in row.get("nodes") or []]
        run.outputs = dict(row.get("outputs") or {})
        run.error = row.get("error", "")
        return run


# --------------------------------------------------------------------------- #
# templates — the workflows worth starting from
# --------------------------------------------------------------------------- #
def template(name: str) -> dict[str, Any]:
    if name == "monitor_market":
        return {
            "name": "Monitor a market and alert on change",
            "description": "Topic → sites → crawl → extract → dataset version → notify",
            "schedule": "6h",
            "state": "active",
            "nodes": [
                {"id": "n1", "type": "topic_discovery",
                 "config": {"topic": "Bangladeshi e-commerce smartphone prices",
                            "count": 25}, "position": {"x": 40, "y": 40}},
                {"id": "n2", "type": "website_selection",
                 "config": {"min_score": 0.5, "limit": 10},
                 "position": {"x": 280, "y": 40}},
                {"id": "n3", "type": "crawler",
                 "config": {"max_pages": 200, "stealth": False, "render_share": 0.3},
                 "position": {"x": 520, "y": 40}},
                {"id": "n4", "type": "ai_extraction",
                 "config": {"recipe": "commerce-product", "llm": False},
                 "position": {"x": 760, "y": 40}},
                {"id": "n5", "type": "dedupe", "config": {"mode": "minhash"},
                 "position": {"x": 1000, "y": 40}},
                {"id": "n6", "type": "dataset_write",
                 "config": {"dataset": "smartphone-prices", "primary_key": "url",
                            "live": True},
                 "position": {"x": 1240, "y": 40}},
                {"id": "n7", "type": "analysis",
                 "config": {"numeric_fields": ["price"], "top": 5},
                 "position": {"x": 1480, "y": 40}},
                {"id": "n8", "type": "notify",
                 "config": {"event": "workflow.monitor.done"}, "position": {"x": 1720, "y": 40}},
            ],
            "edges": [{"source": f"n{i}", "target": f"n{i+1}"} for i in range(1, 8)],
        }
    if name == "research_report":
        return {
            "name": "Research report from a topic",
            "description": "Topic → sites → crawl → extract → analysis → report",
            "schedule": "",
            "state": "draft",
            "nodes": [
                {"id": "n1", "type": "topic_discovery",
                 "config": {"topic": "AI chip market 2026", "count": 40},
                 "position": {"x": 40, "y": 40}},
                {"id": "n2", "type": "website_selection",
                 "config": {"min_score": 0.6, "limit": 20},
                 "position": {"x": 300, "y": 40}},
                {"id": "n3", "type": "crawler", "config": {"max_pages": 300},
                 "position": {"x": 560, "y": 40}},
                {"id": "n4", "type": "ai_extraction",
                 "config": {"llm": True, "schema": {"claim": "string", "source": "string"}},
                 "position": {"x": 820, "y": 40}},
                {"id": "n5", "type": "graph_write", "config": {"kinds": ["company", "product"]},
                 "position": {"x": 1080, "y": 40}},
                {"id": "n6", "type": "dataset_write",
                 "config": {"dataset": "ai-chip-evidence", "primary_key": "url"},
                 "position": {"x": 1340, "y": 40}},
                {"id": "n7", "type": "report",
                 "config": {"dataset": "ai-chip-evidence", "include_provenance": True},
                 "position": {"x": 1600, "y": 40}},
            ],
            "edges": [{"source": f"n{i}", "target": f"n{i+1}"} for i in range(1, 7)],
        }
    if name == "dataset_hygiene":
        return {
            "name": "Dataset hygiene",
            "description": "Clean, dedupe and re-version an existing dataset",
            "schedule": "1d",
            "state": "draft",
            "nodes": [
                {"id": "n1", "type": "data_cleaning",
                 "config": {"normalize": True, "drop_empty": True},
                 "position": {"x": 40, "y": 40}},
                {"id": "n2", "type": "dedupe",
                 "config": {"mode": "simhash", "threshold": 3},
                 "position": {"x": 300, "y": 40}},
                {"id": "n3", "type": "dataset_write",
                 "config": {"dataset": "clean", "primary_key": "url"},
                 "position": {"x": 560, "y": 40}},
                {"id": "n4", "type": "export",
                 "config": {"dataset": "clean", "format": "csv"},
                 "position": {"x": 820, "y": 40}},
            ],
            "edges": [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"},
                      {"source": "n3", "target": "n4"}],
        }
    raise KeyError(name)


def template_list() -> list[dict[str, Any]]:
    out = []
    for key in ("monitor_market", "research_report", "dataset_hygiene"):
        payload = template(key)
        out.append({"id": key, "name": payload["name"],
                    "description": payload["description"],
                    "nodes": len(payload["nodes"]), "schedule": payload["schedule"]})
    return out


def new_blank(name: str, tenant: str) -> Workflow:
    """A minimal valid workflow, so the builder never opens on an empty canvas."""
    return Workflow(
        name=name, tenant=tenant, state="draft",
        description="Starts with a crawl → dataset spine; add nodes as needed.",
        nodes=[Node.from_dict({"id": "n1", "type": "crawler",
                               "config": {"max_pages": 50},
                               "position": {"x": 60, "y": 60}}),
               Node.from_dict({"id": "n2", "type": "dedupe", "config": {},
                               "position": {"x": 340, "y": 60}}),
               Node.from_dict({"id": "n3", "type": "dataset_write",
                               "config": {"dataset": "my-dataset"},
                               "position": {"x": 620, "y": 60}})],
        edges=[Edge("n1", "n2"), Edge("n2", "n3")],
    )


def node_catalogue() -> list[dict[str, Any]]:
    return [{"type": key, **value} for key, value in NODE_TYPES.items()]


def types_producing(output: str) -> Iterable[str]:
    for key, value in NODE_TYPES.items():
        if output in value.get("produces", []):
            yield key
