import json

import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app
from src.research.report import make_report
from workers import research_tasks


def _client():
    return TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                     enable_graphql=False))


def _key(client, email):
    signup = client.post("/auth/signup", json={"org_name": "Research",
                         "email": email, "password": "correct-horse-123"})
    token = signup.json()["access_token"]
    key = client.post("/keys", headers={"Authorization": f"Bearer {token}"},
                      json={"name": "research", "scopes": ["read", "crawl"]})
    return {"X-API-Key": key.json()["key"]}


def test_research_job_tenant_isolation_and_budget():
    client = _client()
    first = _key(client, "research-one@example.test")
    created = client.post("/api/v1/research", headers=first, json={
        "topic": "AI chip market 2026", "sources": 3,
        "max_pages_per_source": 2})
    assert created.status_code == 202, created.text
    job_id = created.json()["research_id"]
    status = client.get(f"/api/v1/research/{job_id}", headers=first)
    assert status.status_code == 200
    assert status.json()["stage"] == "queued"
    assert status.json()["stages"]["planning"] == "queued"
    second = _key(client, "research-two@example.test")
    assert client.get(f"/api/v1/research/{job_id}", headers=second).status_code == 404
    assert client.post("/api/v1/research", headers=first,
                       json={"topic": "AI", "sources": 50}).status_code == 422


def test_research_report_console_is_authenticated():
    client = _client()
    assert client.get("/app/research/agent", follow_redirects=False).status_code == 303
    signup = client.post("/auth/signup", json={"org_name": "Research UI",
                         "email": "research-ui@example.test", "password": "correct-horse-123"})
    client.cookies.set("wcp_session", signup.json()["access_token"])
    response = client.get("/app/research/agent")
    assert response.status_code == 200
    assert "Topic → cited report" in response.text
    assert "Start research" in response.text


def test_report_claims_are_exact_cited_source_sentences():
    item = {"url": "https://example.org/article", "fetched_at": "2026-10-18T00:00:00Z",
            "text": "AI chips saw strong demand in 2026. The market is expected to grow quickly."}
    report = make_report("AI chip market", [item])
    assert report["claims"]
    claim = report["claims"][0]
    assert claim["claim"] in item["text"]
    citation = claim["citations"][0]
    assert citation["url"] == item["url"]
    assert citation["collected_at"] == item["fetched_at"]
    assert report["verification"]["independent_fact_check"] is False


def test_research_task_updates_progress_and_persists_report(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    research_id = "research_aaaaaaaaaaaaaaaaaaaa"
    key = f"wcp:v1:research:{research_id}"
    record = {"research_id": research_id, "tenant": "tenant", "topic": "AI chips",
              "status": "queued", "stage": "queued", "progress": 0,
              "stages": {name: "queued" for name in
                         ("planning", "discovery", "crawl_extract", "source_check", "report")}}
    redis.set(key, json.dumps(record))
    monkeypatch.setattr(research_tasks, "_redis", lambda: redis)
    from src.discovery.service import DiscoveryService, DiscoveryState
    monkeypatch.setattr("src.discovery.service.run_discovery", lambda payload, redis_client=None: {
        "state": DiscoveryState.awaiting_approval.value, "job_id": payload["job_id"]})
    monkeypatch.setattr(DiscoveryService, "approve", lambda self, job_id, dispatch=False:
        ([{"url": "https://source.example/a", "domain": "source.example"}], ["crawl_x"]))
    monkeypatch.setattr("workers.crawl_tasks.run_crawl", lambda payload, redis_client=None: {
        "collected": [{"url": "https://source.example/a", "fetched_at": "2026-10-18T00:00:00Z",
                       "text": "AI chips are a major market in 2026."}],
        "pages_done": 1})
    result = research_tasks.run_research.run({"research_id": research_id,
        "tenant": "tenant", "topic": "AI chips", "count": 1,
        "max_pages_per_site": 1})
    saved = json.loads(redis.get(key))
    assert result["status"] == "completed"
    assert saved["stages"]["report"] == "completed"
    assert saved["report"]["claims"][0]["citations"][0]["url"] == "https://source.example/a"
