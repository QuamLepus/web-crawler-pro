"""Tenant isolation, validation and worker behavior for crawl workflows."""
import json

import fakeredis
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


def test_workflow_validates_url_and_limits():
    client = _client()
    headers = _key(client, "workflow-validation@example.test")
    assert client.post("/api/v1/workflows", headers=headers, json={
        "name": "bad", "steps": [{"url": "file:///etc/passwd"}]}).status_code == 422
    assert client.post("/api/v1/workflows", headers=headers, json={
        "name": "too many", "steps": [{"url": f"https://e{i}.example"} for i in range(11)]}).status_code == 422


def test_workflow_worker_runs_steps_sequentially_and_saves_progress(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    run_id = "wfrun_aaaaaaaaaaaaaaaaaaaa"
    key = f"wcp:v1:workflow_run:{run_id}"
    record = {"run_id": run_id, "tenant": "tenant", "status": "queued",
              "step_index": 0, "steps": [
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
