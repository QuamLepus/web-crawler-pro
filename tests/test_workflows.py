"""Tenant isolation, validation and worker behavior for crawl workflows."""
import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from src.api.main import create_api_app
from workers import workflow_tasks


def _client():
    return TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                     enable_graphql=False))


def _key(client, email):
    signup = client.post("/auth/signup", json={"org_name": "Workflow",
                         "email": email, "password": "correct-horse-123"})
    token = signup.json()["access_token"]
    key = client.post("/keys", headers={"Authorization": f"Bearer {token}"},
                      json={"name": "workflow", "scopes": ["read", "crawl"]})
    return {"X-API-Key": key.json()["key"]}


def test_workflow_create_list_run_and_tenant_isolation():
    client = _client()
    one = _key(client, "workflow-one@example.test")
    two = _key(client, "workflow-two@example.test")
    created = client.post("/api/v1/workflows", headers=one, json={
        "name": " News refresh ", "steps": [
            {"url": "https://example.org/news", "max_pages": 2},
            {"url": "https://example.net/updates", "max_pages": 1},
        ]})
    assert created.status_code == 201, created.text
    definition = created.json()
    assert definition["name"] == "News refresh"
    assert definition["max_pages"] == 3
    assert definition["execution_mode"] == "sequential"
    assert [(step["step_id"], step["depends_on"]) for step in definition["steps"]] == [
        ("step_1", []), ("step_2", ["step_1"])]
    assert client.get("/api/v1/workflows", headers=one).json()["total"] == 1
    assert client.get(f"/api/v1/workflows/{definition['workflow_id']}", headers=two).status_code == 404
    run = client.post(f"/api/v1/workflows/{definition['workflow_id']}/runs", headers=one)
    assert run.status_code == 202, run.text
    run_id = run.json()["run_id"]
    state = client.get(f"/api/v1/workflows/runs/{run_id}", headers=one)
    assert state.status_code == 200
    assert state.json()["status"] == "queued"
    assert client.get("/api/v1/workflows/runs", headers=one).json()["total"] == 1
    assert client.get(f"/api/v1/workflows/runs/{run_id}", headers=two).status_code == 404
    cancelled = client.post(f"/api/v1/workflows/runs/{run_id}/cancel", headers=one)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    cancelled_state = client.get(f"/api/v1/workflows/runs/{run_id}", headers=one).json()
    assert cancelled_state["status"] == "cancelled"
    assert [step["status"] for step in cancelled_state["steps"]] == ["cancelled", "cancelled"]
    assert client.post(f"/api/v1/workflows/runs/{run_id}/cancel", headers=one).status_code == 409


def test_workflow_run_history_search_status_and_date_filters():
    client = _client()
    headers = _key(client, "workflow-history-filters@example.test")
    definition = client.post("/api/v1/workflows", headers=headers, json={
        "name": "Alpha refresh", "steps": [{"url": "https://example.org"}],
    }).json()
    redis = client.app.state.services.redis
    run_ids = []
    for index, (status, created_at) in enumerate([
        ("completed", "2026-05-01T12:00:00+00:00"),
        ("failed", "2026-05-02T12:00:00+00:00"),
        ("queued", "2026-05-03T12:00:00+00:00"),
    ]):
        queued = client.post(
            f"/api/v1/workflows/{definition['workflow_id']}/runs", headers=headers)
        assert queued.status_code == 202
        run_id = queued.json()["run_id"]
        run_ids.append(run_id)
        key = f"wcp:v1:workflow_run:{run_id}"
        record = json.loads(redis.get(key))
        record.update(status=status, created_at=created_at)
        redis.set(key, json.dumps(record), ex=3600)

    filtered = client.get("/api/v1/workflows/runs", headers=headers, params={
        "q": "alpha refresh", "status": "failed",
        "date_from": "2026-05-02", "date_to": "2026-05-03", "page_size": 5,
    })
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert [run["run_id"] for run in filtered.json()["items"]] == [run_ids[1]]

    by_id = client.get("/api/v1/workflows/runs", headers=headers,
                       params={"q": run_ids[2]})
    assert by_id.json()["total"] == 1
    assert by_id.json()["items"][0]["run_id"] == run_ids[2]
    other_tenant = _key(client, "workflow-history-other@example.test")
    hidden = client.get("/api/v1/workflows/runs", headers=other_tenant,
                        params={"q": run_ids[2]})
    assert hidden.json()["total"] == 0

    second_page = client.get("/api/v1/workflows/runs", headers=headers,
                             params={"date_from": "2026-05-01", "page": 2,
                                     "page_size": 1})
    assert second_page.json()["total"] == 3
    assert [run["run_id"] for run in second_page.json()["items"]] == [run_ids[1]]

    assert client.get("/api/v1/workflows/runs", headers=headers,
                      params={"status": "unknown"}).status_code == 422
    assert client.get("/api/v1/workflows/runs", headers=headers,
                      params={"date_from": "2026-05-04", "date_to": "2026-05-01"}).status_code == 422


def test_workflow_edit_delete_preserves_tenant_and_run_safety():
    client = _client()
    owner = _key(client, "workflow-edit-owner@example.test")
    other = _key(client, "workflow-edit-other@example.test")
    created = client.post("/api/v1/workflows", headers=owner, json={
        "name": "Original", "steps": [{"url": "https://old.example", "max_pages": 2}],
        "schedule_interval_minutes": 60,
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow_id"]
    redis = client.app.state.services.redis
    assert client.put(f"/api/v1/workflows/{workflow_id}", headers=other,
                      json={"name": "Stolen"}).status_code == 404
    assert client.delete(f"/api/v1/workflows/{workflow_id}", headers=other).status_code == 404
    changed = client.put(f"/api/v1/workflows/{workflow_id}", headers=owner, json={
        "name": "  Revised  recipe ", "steps": [
            {"url": "https://new.example", "max_pages": 4},
            {"url": "https://next.example", "max_pages": 1}],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Revised recipe"
    assert changed.json()["max_pages"] == 5
    assert changed.json()["schedule_interval_minutes"] == 60
    assert redis.zscore("wcp:v1:workflow_schedules", workflow_id) is not None
    assert client.put(f"/api/v1/workflows/{workflow_id}", headers=owner,
                      json={"steps": []}).status_code == 422
    run = client.post(f"/api/v1/workflows/{workflow_id}/runs", headers=owner).json()
    blocked = client.delete(f"/api/v1/workflows/{workflow_id}", headers=owner)
    assert blocked.status_code == 409
    client.post(f"/api/v1/workflows/runs/{run['run_id']}/cancel", headers=owner)
    deleted = client.delete(f"/api/v1/workflows/{workflow_id}", headers=owner)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["historical_runs_retained"] is True
    assert client.get(f"/api/v1/workflows/{workflow_id}", headers=owner).status_code == 404
    assert redis.zscore("wcp:v1:workflow_schedules", workflow_id) is None
    assert client.get(f"/api/v1/workflows/runs/{run['run_id']}", headers=owner).status_code == 200


def test_workflow_validates_url_and_limits():
    client = _client()
    headers = _key(client, "workflow-validation@example.test")
    assert client.post("/api/v1/workflows", headers=headers, json={
        "name": "bad", "steps": [{"url": "file:///etc/passwd"}]}).status_code == 422
    assert client.post("/api/v1/workflows", headers=headers, json={
        "name": "too many", "steps": [{"url": f"https://e{i}.example"} for i in range(11)]}).status_code == 422


def test_workflow_dag_definition_validates_and_preserves_dependencies():
    client = _client()
    headers = _key(client, "workflow-dag@example.test")
    payload = {"name": "Branched recipe", "steps": [
        {"step_id": "seed", "depends_on": [], "url": "https://seed.example"},
        {"step_id": "news", "depends_on": ["seed"], "url": "https://news.example"},
        {"step_id": "research", "depends_on": ["seed"], "url": "https://research.example"},
        {"step_id": "report", "depends_on": ["news", "research"],
         "url": "https://report.example"},
    ]}
    created = client.post("/api/v1/workflows", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    definition = created.json()
    assert definition["execution_mode"] == "dag"
    assert [step["depends_on"] for step in definition["steps"]] == [
        [], ["seed"], ["seed"], ["news", "research"]]
    assert definition["steps"][3]["step_id"] == "report"
    run = client.post(f"/api/v1/workflows/{definition['workflow_id']}/runs", headers=headers)
    assert run.status_code == 202, run.text
    state = client.get(f"/api/v1/workflows/runs/{run.json()['run_id']}", headers=headers).json()
    assert state["execution_mode"] == "dag"
    assert [step["step_id"] for step in state["steps"]] == ["seed", "news", "research", "report"]
    edited = client.put(f"/api/v1/workflows/{definition['workflow_id']}", headers=headers,
                        json={"steps": [
                            {"step_id": "fresh", "depends_on": [], "url": "https://fresh.example"},
                            {"step_id": "summary", "depends_on": [],
                             "url": "https://summary.example"},
                            {"step_id": "join", "depends_on": ["fresh", "summary"],
                             "url": "https://join.example"},
                        ]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["execution_mode"] == "dag"
    assert client.get(f"/api/v1/workflows/runs/{run.json()['run_id']}", headers=headers).json()["steps"][0]["step_id"] == "seed"

    for invalid_steps in [
        [{"step_id": "same", "url": "https://one.example"},
         {"step_id": "same", "url": "https://two.example"}],
        [{"step_id": "one", "depends_on": ["missing"], "url": "https://one.example"}],
        [{"step_id": "one", "depends_on": ["two"], "url": "https://one.example"},
         {"step_id": "two", "depends_on": ["one"], "url": "https://two.example"}],
    ]:
        response = client.post("/api/v1/workflows", headers=headers,
                               json={"name": "Invalid graph", "steps": invalid_steps})
        assert response.status_code == 422, response.text


def test_workflow_worker_runs_dag_branches_in_order_and_joins(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    run_id = "wfrun_eeeeeeeeeeeeeeeeeeee"
    workflow_id = "workflow_eeeeeeeeeeeeeeeeeeee"
    key = f"wcp:v1:workflow_run:{run_id}"
    steps = [
        {"step_id": "seed", "depends_on": [], "url": "https://seed.example"},
        {"step_id": "news", "depends_on": ["seed"], "url": "https://news.example"},
        {"step_id": "research", "depends_on": ["seed"], "url": "https://research.example"},
        {"step_id": "report", "depends_on": ["news", "research"], "url": "https://report.example"},
    ]
    redis.set(key, json.dumps({"run_id": run_id, "workflow_id": workflow_id,
        "tenant": "tenant", "status": "queued", "execution_mode": "dag",
        "step_index": 0, "steps": [{"step_id": step["step_id"],
        "depends_on": step["depends_on"], "url": step["url"], "status": "queued"}
        for step in steps]}))
    monkeypatch.setattr(workflow_tasks, "_redis", lambda: redis)
    seen = []

    def fake_crawl(payload, redis_client=None):
        seen.append(payload["url"])
        return {"failures": [], "pages_done": 1, "items": 1, "collected": [{}]}

    monkeypatch.setattr("workers.crawl_tasks.run_crawl", fake_crawl)
    result = workflow_tasks.run_workflow.run({"run_id": run_id, "tenant": "tenant",
        "execution_mode": "dag", "steps": steps})
    saved = json.loads(redis.get(key))
    assert result["status"] == "completed"
    assert seen == [step["url"] for step in steps]
    assert [step["status"] for step in saved["steps"]] == ["completed"] * 4


def test_workflow_dag_skips_dependants_after_failure_but_runs_independent_branch(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    run_id = "wfrun_ffffffffffffffffffff"
    key = f"wcp:v1:workflow_run:{run_id}"
    steps = [
        {"step_id": "bad", "depends_on": [], "url": "https://bad.example"},
        {"step_id": "blocked", "depends_on": ["bad"], "url": "https://blocked.example"},
        {"step_id": "independent", "depends_on": [], "url": "https://independent.example"},
    ]
    redis.set(key, json.dumps({"run_id": run_id, "workflow_id": "workflow_ffffffffffffffffffff",
        "tenant": "tenant", "status": "queued", "execution_mode": "dag", "step_index": 0,
        "steps": [{**step, "status": "queued"} for step in steps]}))
    monkeypatch.setattr(workflow_tasks, "_redis", lambda: redis)
    seen = []

    def fake_crawl(payload, redis_client=None):
        seen.append(payload["url"])
        return ({"failures": ["crawl failed"], "pages_done": 0, "items": 0, "collected": []}
                if payload["url"] == "https://bad.example" else
                {"failures": [], "pages_done": 1, "items": 1, "collected": [{}]})

    monkeypatch.setattr("workers.crawl_tasks.run_crawl", fake_crawl)
    with pytest.raises(RuntimeError, match="workflow steps failed: bad"):
        workflow_tasks.run_workflow.run({"run_id": run_id, "tenant": "tenant",
            "execution_mode": "dag", "steps": steps})
    saved = json.loads(redis.get(key))
    assert saved["status"] == "failed"
    assert seen == ["https://bad.example", "https://independent.example"]
    assert [step["status"] for step in saved["steps"]] == ["failed", "skipped", "completed"]


def test_sequential_failure_marks_unstarted_steps_skipped(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    run_id = "wfrun_12345678901234567890"
    key = f"wcp:v1:workflow_run:{run_id}"
    redis.set(key, json.dumps({"run_id": run_id, "workflow_id": "workflow_12345678901234567890",
        "tenant": "tenant", "status": "queued", "execution_mode": "sequential",
        "step_index": 0, "steps": [
            {"step_id": "step_1", "depends_on": [], "status": "queued"},
            {"step_id": "step_2", "depends_on": ["step_1"], "status": "queued"},
        ]}))
    monkeypatch.setattr(workflow_tasks, "_redis", lambda: redis)
    monkeypatch.setattr("workers.crawl_tasks.run_crawl", lambda *_args, **_kwargs: {
        "failures": ["crawl failed"], "pages_done": 0, "items": 0, "collected": []})
    with pytest.raises(RuntimeError, match="crawl failed"):
        workflow_tasks.run_workflow.run({"run_id": run_id, "tenant": "tenant", "steps": [
            {"step_id": "step_1", "depends_on": [], "url": "https://one.example"},
            {"step_id": "step_2", "depends_on": ["step_1"], "url": "https://two.example"},
        ]})
    saved = json.loads(redis.get(key))
    assert saved["status"] == "failed"
    assert [step["status"] for step in saved["steps"]] == ["failed", "skipped"]


def test_workflow_worker_runs_steps_sequentially_and_saves_progress(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    run_id = "wfrun_aaaaaaaaaaaaaaaaaaaa"
    key = f"wcp:v1:workflow_run:{run_id}"
    record = {"run_id": run_id, "workflow_id": "workflow_aaaaaaaaaaaaaaaaaaaa",
              "tenant": "tenant", "status": "queued", "step_index": 0, "steps": [
                  {"url": "https://one.example", "status": "queued"},
                  {"url": "https://two.example", "status": "queued"}],
              "updated_at": "now"}
    redis.set(key, json.dumps(record))
    monkeypatch.setattr(workflow_tasks, "_redis", lambda: redis)
    seen = []

    def fake_crawl(payload, redis_client=None):
        seen.append(payload["url"])
        return {"failures": [], "pages_done": 1, "items": 1,
                "data_sha256": "abc", "collected": [{"url": payload["url"]}]}

    monkeypatch.setattr("workers.crawl_tasks.run_crawl", fake_crawl)
    result = workflow_tasks.run_workflow.run({"run_id": run_id, "tenant": "tenant",
        "steps": [{"url": "https://one.example", "max_pages": 1},
                  {"url": "https://two.example", "max_pages": 1}]})
    saved = json.loads(redis.get(key))
    assert result["status"] == "completed"
    assert seen == ["https://one.example", "https://two.example"]
    assert [step["status"] for step in saved["steps"]] == ["completed", "completed"]
    assert redis.zscore("wcp:v1:workflow_active_runs:workflow_aaaaaaaaaaaaaaaaaaaa", run_id) is None


def test_running_workflow_cancel_is_tenant_scoped_and_requested():
    client = _client()
    owner = _key(client, "workflow-cancel-owner@example.test")
    other = _key(client, "workflow-cancel-other@example.test")
    run_id = "wfrun_bbbbbbbbbbbbbbbbbbbb"
    record = {"run_id": run_id, "tenant": "workflow-cancel-owner",
              "status": "running", "cancel_requested": False, "steps": []}
    # Tenant IDs are resolved from API-key records and used by the service.
    tenant = client.app.state.services.api_keys.lookup(owner["X-API-Key"])
    record["tenant"] = tenant.tenant
    client.app.state.services.redis.set(f"wcp:v1:workflow_run:{run_id}", json.dumps(record))
    assert client.post(f"/api/v1/workflows/runs/{run_id}/cancel", headers=other).status_code == 404
    response = client.post(f"/api/v1/workflows/runs/{run_id}/cancel", headers=owner)
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["cancel_requested"] is True
    saved = json.loads(client.app.state.services.redis.get(f"wcp:v1:workflow_run:{run_id}"))
    assert saved["cancel_requested"] is True


def test_workflow_worker_honors_cancellation_between_steps(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    run_id = "wfrun_cccccccccccccccccccc"
    key = f"wcp:v1:workflow_run:{run_id}"
    record = {"run_id": run_id, "tenant": "tenant", "status": "queued",
              "step_index": 0, "cancel_requested": False, "steps": [
                  {"url": "https://one.example", "status": "queued"},
                  {"url": "https://two.example", "status": "queued"}],
              "updated_at": "now"}
    redis.set(key, json.dumps(record))
    monkeypatch.setattr(workflow_tasks, "_redis", lambda: redis)
    seen = []

    def cancel_after_first(payload, redis_client=None):
        seen.append(payload["url"])
        latest = json.loads(redis_client.get(key))
        latest["cancel_requested"] = True
        latest["cancel_requested_at"] = "now"
        redis_client.set(key, json.dumps(latest))
        return {"failures": [], "pages_done": 1, "items": 1,
                "data_sha256": "abc", "collected": [{"url": payload["url"]}]}

    monkeypatch.setattr("workers.crawl_tasks.run_crawl", cancel_after_first)
    result = workflow_tasks.run_workflow.run({"run_id": run_id, "tenant": "tenant",
        "steps": [{"url": "https://one.example", "max_pages": 1},
                  {"url": "https://two.example", "max_pages": 1}]})
    saved = json.loads(redis.get(key))
    assert result["status"] == "cancelled"
    assert seen == ["https://one.example"]
    assert saved["status"] == "cancelled"
    assert saved["steps"][0]["status"] == "completed"
    assert saved["steps"][1]["status"] == "cancelled"


def test_workflow_schedule_can_be_updated_disabled_and_is_tenant_scoped():
    client = _client()
    owner = _key(client, "workflow-schedule-owner@example.test")
    other = _key(client, "workflow-schedule-other@example.test")
    created = client.post("/api/v1/workflows", headers=owner, json={
        "name": "Scheduled crawl", "steps": [{"url": "https://schedule.example"}],
        "schedule_interval_minutes": 60})
    assert created.status_code == 201, created.text
    workflow = created.json()
    workflow_id = workflow["workflow_id"]
    assert workflow["schedule_interval_minutes"] == 60
    redis = client.app.state.services.redis
    assert redis.zscore("wcp:v1:workflow_schedules", workflow_id) is not None
    denied = client.patch(f"/api/v1/workflows/{workflow_id}/schedule", headers=other,
                          json={"interval_minutes": 120})
    assert denied.status_code == 404
    updated = client.patch(f"/api/v1/workflows/{workflow_id}/schedule", headers=owner,
                           json={"interval_minutes": 180})
    assert updated.status_code == 200
    assert updated.json()["schedule_interval_minutes"] == 180
    disabled = client.patch(f"/api/v1/workflows/{workflow_id}/schedule", headers=owner,
                            json={"interval_minutes": None})
    assert disabled.status_code == 200
    assert disabled.json()["schedule_interval_minutes"] is None
    assert redis.zscore("wcp:v1:workflow_schedules", workflow_id) is None


def test_scheduler_tick_queues_due_workflow_once_and_advances_due_time(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    workflow_id = "workflow_dddddddddddddddddddd"
    now = __import__("time").time()
    scheduled_steps = [
        {"step_id": "seed", "depends_on": [], "url": "https://due.example",
         "max_pages": 1, "obey_robots": True},
        {"step_id": "followup", "depends_on": ["seed"],
         "url": "https://followup.example", "max_pages": 1, "obey_robots": True},
    ]
    redis.set(f"wcp:v1:workflow:{workflow_id}", json.dumps({
        "workflow_id": workflow_id, "tenant": "tenant", "name": "Scheduled",
        "steps": scheduled_steps, "execution_mode": "dag",
        "max_pages": 2, "schedule_interval_minutes": 60,
    }))
    redis.zadd("wcp:v1:workflow_schedules", {workflow_id: now - 1})
    monkeypatch.setattr(workflow_tasks, "_redis", lambda: redis)
    submitted = []

    class FakeScheduler:
        def __init__(self, redis_client, celery_app=None):
            assert redis_client is redis

        def submit(self, spec):
            submitted.append(spec)

    monkeypatch.setattr("src.compute.scheduler.SchedulerService", FakeScheduler)
    result = workflow_tasks.schedule_tick.run()
    assert result == {"queued": 1, "due": 1}
    assert len(submitted) == 1
    assert submitted[0].kind == "workflow"
    assert submitted[0].payload["steps"][0]["url"] == "https://due.example"
    assert submitted[0].payload["execution_mode"] == "dag"
    assert submitted[0].payload["steps"][1]["depends_on"] == ["seed"]
    queued = redis.zrevrange("wcp:v1:workflow_runs:tenant", 0, 0)
    assert queued
    run_id = queued[0]
    run = json.loads(redis.get(f"wcp:v1:workflow_run:{run_id}"))
    assert run["scheduled"] is True and run["status"] == "queued"
    assert run["execution_mode"] == "dag"
    assert run["steps"][1]["depends_on"] == ["seed"]
    assert redis.zscore("wcp:v1:workflow_schedules", workflow_id) > now
    assert redis.zscore(f"wcp:v1:workflow_active_runs:{workflow_id}", run_id) is not None
    # The short lock prevents duplicate dispatch if two ticks overlap.
    assert workflow_tasks.schedule_tick.run() == {"queued": 0, "locked": True}
