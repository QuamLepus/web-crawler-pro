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


def test_research_job_list_is_paginated_tenant_scoped_and_omits_reports():
    client = _client()
    first = _key(client, "research-list-one@example.test")
    second = _key(client, "research-list-two@example.test")

    def create(headers, topic):
        response = client.post("/api/v1/research", headers=headers, json={
            "topic": topic, "sources": 1, "max_pages_per_source": 1})
        assert response.status_code == 202, response.text
        return response.json()["research_id"]

    first_ids = [create(first, "First tenant research one"),
                 create(first, "First tenant research two")]
    second_id = create(second, "Second tenant research")

    redis = client.app.state.services.redis
    record_key = f"wcp:v1:research:{first_ids[0]}"
    record = json.loads(redis.get(record_key))
    record["report"] = {"claims": [{"claim": "private report content"}]}
    redis.set(record_key, json.dumps(record))

    page_one = client.get("/api/v1/research?page=1&page_size=1", headers=first)
    page_two = client.get("/api/v1/research?page=2&page_size=1", headers=first)
    second_page = client.get("/api/v1/research", headers=second)

    assert page_one.status_code == page_two.status_code == second_page.status_code == 200
    assert page_one.json()["total"] == page_two.json()["total"] == 2
    first_page_ids = {page_one.json()["items"][0]["research_id"],
                      page_two.json()["items"][0]["research_id"]}
    assert first_page_ids == set(first_ids)
    assert all("report" not in item for page in (page_one, page_two)
               for item in page.json()["items"])
    assert "private report content" not in page_one.text + page_two.text
    assert second_page.json()["total"] == 1
    assert [item["research_id"] for item in second_page.json()["items"]] == [second_id]


def test_research_cancellation_is_cooperative_and_tenant_scoped():
    client = _client()
    first = _key(client, "research-cancel-one@example.test")
    second = _key(client, "research-cancel-two@example.test")
    created = client.post("/api/v1/research", headers=first, json={
        "topic": "Cancelable research job", "sources": 2,
        "max_pages_per_source": 2})
    assert created.status_code == 202, created.text
    research_id = created.json()["research_id"]

    foreign_cancel = client.post(f"/api/v1/research/{research_id}/cancel", headers=second)
    assert foreign_cancel.status_code == 404
    assert client.get(f"/api/v1/research/{research_id}", headers=first).json()["status"] == "queued"

    cancelled = client.post(f"/api/v1/research/{research_id}/cancel", headers=first)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["stage"] == "cancelled"
    assert client.get(f"/api/v1/research/{research_id}", headers=first).json()["status"] == "cancelled"
    assert client.post(f"/api/v1/research/{research_id}/cancel", headers=first).status_code == 409


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


def test_research_task_stops_between_source_crawls_when_cancelled(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    research_id = "research_cccccccccccccccccccc"
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
    monkeypatch.setattr(DiscoveryService, "approve", lambda self, job_id, dispatch=False: (
        [{"url": "https://one.example", "domain": "one.example"},
         {"url": "https://two.example", "domain": "two.example"}], ["crawl_one", "crawl_two"]))
    crawled = []

    def cancel_during_source(payload, redis_client=None):
        crawled.append(payload["url"])
        saved = json.loads(redis.get(key))
        saved.update(status="cancelled", stage="cancelled")
        redis.set(key, json.dumps(saved))
        return {"collected": [], "pages_done": 1}

    monkeypatch.setattr("workers.crawl_tasks.run_crawl", cancel_during_source)
    result = research_tasks.run_research.run({"research_id": research_id,
        "tenant": "tenant", "topic": "AI chips", "count": 2,
        "max_pages_per_site": 1})

    assert result == {"research_id": research_id, "status": "cancelled"}
    assert crawled == ["https://one.example"]
    assert json.loads(redis.get(key))["status"] == "cancelled"


def test_cancelled_research_task_stops_before_start(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    research_id = "research_bbbbbbbbbbbbbbbbbbbb"
    key = f"wcp:v1:research:{research_id}"
    record = {"research_id": research_id, "tenant": "tenant", "topic": "AI chips",
              "status": "cancelled", "stage": "cancelled", "progress": 0,
              "stages": {name: "cancelled" for name in
                         ("planning", "discovery", "crawl_extract", "source_check", "report")}}
    redis.set(key, json.dumps(record))
    monkeypatch.setattr(research_tasks, "_redis", lambda: redis)

    def unexpected_discovery(*_args, **_kwargs):
        raise AssertionError("cancelled research must not start discovery")

    monkeypatch.setattr("src.discovery.service.run_discovery", unexpected_discovery)
    result = research_tasks.run_research.run({"research_id": research_id,
        "tenant": "tenant", "topic": "AI chips", "count": 1,
        "max_pages_per_site": 1})

    assert result == {"research_id": research_id, "status": "cancelled"}
    assert json.loads(redis.get(key))["status"] == "cancelled"
