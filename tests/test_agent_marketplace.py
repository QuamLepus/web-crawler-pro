"""Catalog-only agent marketplace tests: publication and tenant isolation."""
import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app


def _client():
    return TestClient(create_api_app(
        fakeredis.FakeStrictRedis(decode_responses=True), enable_graphql=False))


def _key(client, email):
    signup = client.post("/auth/signup", json={
        "org_name": "Agent Catalog", "email": email,
        "password": "correct-horse-123"})
    assert signup.status_code == 200, signup.text
    token = signup.json()["access_token"]
    response = client.post("/keys", headers={"Authorization": f"Bearer {token}"},
                           json={"name": "agent-marketplace", "scopes": ["read"]})
    assert response.status_code == 200, response.text
    return {"X-API-Key": response.json()["key"]}


def _card(name="Research Summarizer"):
    return {
        "manifest": {
            "name": name,
            "version": "1.2.0",
            "summary": "Summarizes a supplied set of research evidence.",
            "capabilities": ["summarize", "evidence-review", "summarize"],
            "input_schema": {"type": "object", "properties": {
                "evidence": {"type": "array", "items": {"type": "string"}}}},
            "output_schema": {"type": "object", "properties": {
                "summary": {"type": "string"}}},
        },
        "tags": ["Research", "summarization", "research"],
        "license": "CC-BY-4.0",
    }


def test_agent_catalog_publication_and_tenant_visibility():
    client = _client()
    owner = _key(client, "agent-owner@example.test")
    other = _key(client, "agent-other@example.test")

    created = client.post("/api/v1/agent-marketplace/agents", headers=owner,
                          json=_card())
    assert created.status_code == 201, created.text
    card = created.json()
    agent_id = card["agent_id"]
    assert card["status"] == "draft"
    assert card["tags"] == ["research", "summarization"]
    assert card["manifest"]["capabilities"] == ["summarize", "evidence-review"]
    assert client.get("/api/v1/agent-marketplace/agents", headers=other).json() == []
    assert client.get(f"/api/v1/agent-marketplace/agents/{agent_id}",
                      headers=other).status_code == 404
    assert client.get("/api/v1/agent-marketplace/my/agents", headers=owner).json()[0]["agent_id"] == agent_id

    published = client.patch(
        f"/api/v1/agent-marketplace/agents/{agent_id}", headers=owner,
        json={"status": "published"})
    assert published.status_code == 200, published.text
    public = client.get("/api/v1/agent-marketplace/agents", headers=other,
                        params={"q": "summarizer", "tag": "research"})
    assert public.status_code == 200, public.text
    assert [item["agent_id"] for item in public.json()] == [agent_id]
    assert client.get(f"/api/v1/agent-marketplace/agents/{agent_id}",
                      headers=other).status_code == 200

    foreign_update = client.patch(
        f"/api/v1/agent-marketplace/agents/{agent_id}", headers=other,
        json={"status": "archived"})
    assert foreign_update.status_code == 404
    assert client.get(f"/api/v1/agent-marketplace/agents/{agent_id}",
                      headers=other).json()["status"] == "published"


def test_managed_agent_executes_allowlisted_heuristic_without_persisting_input(monkeypatch):
    client = _client()
    headers = _key(client, "managed-agent@example.test")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-but-must-not-be-used")

    listed = client.get("/api/v1/agent-marketplace/managed/agents", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [item["agent_id"] for item in listed.json()] == ["data-curator"]
    assert listed.json()[0]["execution_engine"] == "wcp-data-curator-heuristic-v1"

    records = [
        ("Renewable energy research describes how solar panels convert sunlight "
         "into electricity and how wind turbines generate clean power for homes."),
        ("This complete research note repeats enough detail for review and includes "
         "contact alice@example.test for sensitive personal information."),
    ]
    result = client.post(
        "/api/v1/agent-marketplace/managed/agents/data-curator/runs",
        headers=headers, json={"records": records})
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["agent_id"] == "data-curator"
    assert payload["execution_engine"] == "wcp-data-curator-heuristic-v1"
    assert payload["persisted"] is False
    assert payload["total"] == 2
    assert payload["dropped"] >= 1
    assert all("record" not in item for item in payload["results"])
    assert "alice@example.test" not in result.text

    unknown = client.post(
        "/api/v1/agent-marketplace/managed/agents/publisher-code/runs",
        headers=headers, json={"records": ["input"]})
    assert unknown.status_code == 404
    assert client.post(
        "/api/v1/agent-marketplace/managed/agents/data-curator/runs",
        json={"records": ["input"]}).status_code == 401


def test_managed_agent_request_is_bounded():
    client = _client()
    headers = _key(client, "managed-agent-bounds@example.test")
    route = "/api/v1/agent-marketplace/managed/agents/data-curator/runs"
    assert client.post(route, headers=headers,
                       json={"records": ["x" * 9000]}).status_code == 422
    assert client.post(route, headers=headers,
                       json={"records": ["x"] * 21}).status_code == 422
    assert client.post(route, headers=headers,
                       json={"records": [None]}).status_code == 422


def test_agent_manifest_is_bounded_and_rejects_unmodeled_runtime_fields():
    client = _client()
    owner = _key(client, "agent-validation@example.test")
    invalid_version = _card()
    invalid_version["manifest"]["version"] = "latest"
    assert client.post("/api/v1/agent-marketplace/agents", headers=owner,
                       json=invalid_version).status_code == 422

    executable = _card()
    executable["manifest"]["python_code"] = "print('unsafe')"
    assert client.post("/api/v1/agent-marketplace/agents", headers=owner,
                       json=executable).status_code == 422

    oversized = _card()
    oversized["manifest"]["input_schema"] = {"description": "x" * 9000}
    assert client.post("/api/v1/agent-marketplace/agents", headers=owner,
                       json=oversized).status_code == 422

    assert client.post("/api/v1/agent-marketplace/agents", json=_card()).status_code == 401
