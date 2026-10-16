"""Tests for the workflow builder's model and engine.

A visual builder is only trustworthy if the graph is validated before it runs
(cycles, dangling edges, no terminal node) and if a partial run is
*inspectable*: which node failed, with what message, and what the rest skipped.
"""

from __future__ import annotations

import fakeredis
import pytest

from src.workflows.engine import RunContext, SkipNode, WorkflowDeps, dry_run, run_workflow
from src.workflows.models import (
    Edge,
    Node,
    Workflow,
    WorkflowRun,
    new_blank,
    node_catalogue,
    template,
    template_list,
)
from src.workflows.store import WorkflowStore


def _chain(*types: str, configs: list[dict] | None = None) -> Workflow:
    """A straight-line graph; crawler configs get seed urls automatically."""
    nodes = [Node(id=f"n{i}", type=t, config=(configs or [{} for _ in types])[i - 1])
             for i, t in enumerate(types, start=1)]
    for node in nodes:
        if node.type == "crawler" and "urls" not in node.config:
            node.config["urls"] = ["https://a.test/"]
    edges = [Edge(f"n{i}", f"n{i + 1}") for i in range(1, len(nodes))]
    return Workflow(name="test", tenant="t1", nodes=nodes, edges=edges)


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
def test_unknown_node_type_is_refused():
    with pytest.raises(ValueError):
        Node(id="n1", type="run_shell")


def test_validation_catches_cycles_dangling_edges_and_missing_terminals():
    cyclic = Workflow(name="c", tenant="t1",
                      nodes=[Node(id="a", type="crawler"), Node(id="b", type="dedupe")],
                      edges=[Edge("a", "b"), Edge("b", "a")])
    problems = cyclic.validate()
    assert any("cycle" in p for p in problems)

    dangling = Workflow(name="d", tenant="t1",
                        nodes=[Node(id="a", type="crawler")],
                        edges=[Edge("a", "ghost")])
    assert any("unknown node" in p for p in dangling.validate())

    no_terminal = Workflow(name="t", tenant="t1",
                           nodes=[Node(id="a", type="crawler")], edges=[])
    assert any("terminal" in p for p in no_terminal.validate())

    loop = Workflow(name="l", tenant="t1", nodes=[Node(id="a", type="crawler")],
                    edges=[Edge("a", "a")])
    assert any("self-loop" in p for p in loop.validate())


def test_topological_order_is_stable():
    workflow = Workflow(
        name="w", tenant="t1",
        nodes=[Node(id="a", type="crawler"), Node(id="b", type="dedupe"),
               Node(id="c", type="dataset_write"), Node(id="d", type="notify")],
        edges=[Edge("a", "c"), Edge("b", "d")],
    )
    order = [n.id for n in workflow.topological_order()]
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")


def test_templates_and_blank_workflow_are_valid():
    for row in template_list():
        payload = template(row["id"])
        workflow = Workflow.from_dict(payload)
        payload_tenant = payload
        assert workflow.validate() == [], payload_tenant["name"]
    blank = new_blank("demo", "t1")
    assert blank.validate() == []
    assert any(node["type"] == "crawler" for node in node_catalogue())


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def test_dry_run_describes_without_running():
    workflow = Workflow.from_dict(template("monitor_market"))
    preview = dry_run(workflow)
    assert preview["valid"] is True
    assert [row["type"] for row in preview["order"]][:2] == ["topic_discovery",
                                                             "website_selection"]


def test_full_run_with_injected_collaborators():
    calls: list[str] = []

    deps = WorkflowDeps(
        discovery=lambda topic, count: (calls.append("discovery"), {
            "candidates": [{"url": "https://shop.test", "host": "shop.test",
                            "score": 0.9}]})[1],
        crawl=lambda urls, **kw: (calls.append("crawl"), [
            {"url": u, "html": "<h1>Pixel 10</h1>", "text": "Pixel 10 costs $1099",
             "title": "Pixel 10", "provenance_id": "pr_1"} for u in urls])[1],
        extract=lambda pages, **kw: (calls.append("extract"), [
            {"url": p["url"], "title": p["title"], "price": {"amount": 1099.0},
             "provenance_id": "pr_1"} for p in pages])[1],
        dataset_write=lambda name, rows, **kw: (calls.append("dataset"), {
            "version": {"version": "v1"}, "changed": True})[1],
        analysis=lambda rows, **kw: {"numeric": {"price": {"min": 1099.0}}},
        notify=lambda event, payload: calls.append(f"notify:{event}"),
    )
    workflow = _chain(
        "topic_discovery", "website_selection", "crawler", "ai_extraction",
        "data_cleaning", "dedupe", "dataset_write", "analysis", "notify",
        configs=[{"topic": "pixel prices"}, {"limit": 5}, {"max_pages": 10}, {},
                 {}, {}, {"dataset": "prices"}, {"numeric_fields": ["price"]},
                 {"event": "workflow.done"}],
    )
    run = run_workflow(workflow, deps)
    assert run.state == "ok", run.to_dict()
    assert run.outputs["records"] >= 1
    assert run.outputs["dataset"] == "prices"
    assert "notify:workflow.done" in calls
    assert all(node.status == "ok" for node in run.nodes)


def test_missing_collaborator_skips_instead_of_failing():
    workflow = _chain("topic_discovery", "notify",
                      configs=[{"topic": "x"}, {"event": "e"}])
    run = run_workflow(workflow, WorkflowDeps())
    statuses = {n.node_id: n.status for n in run.nodes}
    assert statuses["n1"] == "skipped"
    assert run.state == "ok"          # a skip is not a failure


def test_failing_node_records_the_error_and_stops_unless_told_otherwise():
    def boom(*args, **kwargs):
        raise ValueError("upstream returned junk")

    workflow = _chain("crawler", "dedupe", "dataset_write",
                      configs=[{"max_pages": 1}, {}, {"dataset": "d"}])
    deps = WorkflowDeps(
        crawl=lambda urls, **kw: [{"url": "https://a.test", "html": "<p>x</p>"}],
        dedupe=boom,
    )
    run = run_workflow(workflow, deps)
    assert run.state in ("failed", "partial")
    failed = next(n for n in run.nodes if n.status == "failed")
    assert "upstream returned junk" in failed.error


def test_skip_on_error_lets_the_run_continue():
    def boom(*args, **kwargs):
        raise ValueError("optional step failed")

    workflow = _chain("crawler", "dedupe", "dataset_write",
                      configs=[{"max_pages": 1}, {}, {"dataset": "d"}])
    workflow.nodes[1].on_error = "skip"
    deps = WorkflowDeps(
        crawl=lambda urls, **kw: [{"url": "https://a.test", "html": "<p>x</p>"}],
        dedupe=boom,
        dataset_write=lambda name, rows, **kw: {"version": {"version": "v1"}},
    )
    run = run_workflow(workflow, deps)
    statuses = [n.status for n in run.nodes]
    assert "failed" in statuses and "ok" in statuses
    assert run.state == "partial"


def test_run_deadline_skips_remaining_nodes():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            self.t += 100.0
            return self.t

    workflow = _chain("crawler", "dedupe", "dataset_write",
                      configs=[{"max_pages": 1}, {}, {"dataset": "d"}])
    deps = WorkflowDeps(
        crawl=lambda urls, **kw: [{"url": "https://a.test", "html": "<p>x</p>"}],
        dedupe=lambda rows, **kw: rows,
        dataset_write=lambda name, rows, **kw: {"version": {"version": "v1"}},
        now=Clock(), max_seconds=150.0,
    )
    run = run_workflow(workflow, deps)
    assert any("deadline" in (n.error or "") for n in run.nodes)


def test_context_snapshot_is_the_run_output():
    context = RunContext(topic="x")
    context.records = [{"a": 1}]
    context.dataset = "d"
    snapshot = context.snapshot()
    assert snapshot["records"] == 1 and snapshot["dataset"] == "d"


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_store_round_trips_workflows_and_runs():
    store = WorkflowStore(fakeredis.FakeStrictRedis(decode_responses=True))
    workflow = Workflow.from_dict(template("dataset_hygiene"))
    workflow.tenant = "t1"
    store.save(workflow)
    assert store.get("t1", workflow.workflow_id).name == workflow.name
    assert len(store.list("t1")) == 1

    run = WorkflowRun(workflow_id=workflow.workflow_id, tenant="t1")
    run.state = "ok"
    run.finished_at = run.started_at + 1
    store.save_run(run)
    assert store.get_run("t1", run.run_id).state == "ok"
    assert len(store.runs("t1", workflow_id=workflow.workflow_id)) == 1
    assert store.stats("t1")["workflows"] == 1

    store.delete("t1", workflow.workflow_id)
    assert store.list("t1") == []
