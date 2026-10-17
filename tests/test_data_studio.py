"""Persistence and tenant isolation for saved Data Studio analyses."""
import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app


def _client():
    return TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                     enable_graphql=False))


def _key(client, email):
    signup = client.post("/auth/signup", json={"org_name": "Studio",
                         "email": email, "password": "correct-horse-123"})
    token = signup.json()["access_token"]
    key = client.post("/keys", headers={"Authorization": f"Bearer {token}"},
                      json={"name": "studio", "scopes": ["read"]})
    return {"X-API-Key": key.json()["key"]}


def test_saved_queries_are_tenant_scoped_and_deletable():
    client = _client()
    one = _key(client, "studio-one@example.test")
    two = _key(client, "studio-two@example.test")
    response = client.post("/api/v1/studio/saved-queries", headers=one, json={
        "name": "Solar research", "q": '"solar panel" site:.edu',
        "limit": 50, "sources": ["wikipedia", "curlie", "wikipedia"],
        "group_by": "source", "metric": "avg_score"})
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["sources"] == ["wikipedia", "curlie"]
    assert saved["group_by"] == "source"
    assert saved["metric"] == "avg_score"
    assert client.get("/api/v1/studio/saved-queries", headers=one).json()["items"][0]["saved_id"] == saved["saved_id"]
    assert client.get("/api/v1/studio/saved-queries", headers=two).json()["total"] == 0
    assert client.delete(f"/api/v1/studio/saved-queries/{saved['saved_id']}", headers=two).status_code == 404
    assert client.delete(f"/api/v1/studio/saved-queries/{saved['saved_id']}", headers=one).status_code == 204
    assert client.get("/api/v1/studio/saved-queries", headers=one).json()["total"] == 0


def test_saved_query_validation_and_authentication():
    client = _client()
    headers = _key(client, "studio-validation@example.test")
    assert client.post("/api/v1/studio/saved-queries", headers=headers,
                       json={"name": "  ", "q": "test"}).status_code == 422
    assert client.post("/api/v1/studio/saved-queries", headers=headers,
                       json={"name": "bad chart", "q": "test",
                             "group_by": "tenant", "metric": "sum"}).status_code == 422
    assert client.get("/api/v1/studio/saved-queries").status_code == 401


def test_result_snapshots_are_immutable_bounded_and_tenant_scoped():
    client = _client()
    one = _key(client, "snapshot-one@example.test")
    two = _key(client, "snapshot-two@example.test")
    created = client.post("/api/v1/studio/snapshots", headers=one, json={
        "name": "Solar result sample", "q": "solar panel", "sources": ["wiki"],
        "group_by": "source", "metric": "avg_score", "rows": [{
            "rank": 1, "url": "https://example.org/paper", "title": "Solar paper",
            "host": "example.org", "snippet": "short evidence", "score": 0.84,
            "source": "wiki"}]})
    assert created.status_code == 201, created.text
    snapshot = created.json()
    assert snapshot["row_count"] == 1
    snapshot_id = snapshot["snapshot_id"]
    assert "rows" not in snapshot
    listed = client.get("/api/v1/studio/snapshots", headers=one).json()
    assert listed["total"] == 1
    assert listed["items"][0]["snapshot_id"] == snapshot_id
    assert client.get(f"/api/v1/studio/snapshots/{snapshot_id}", headers=one).json()["rows"][0]["score"] == 0.84
    assert client.get(f"/api/v1/studio/snapshots/{snapshot_id}", headers=two).status_code == 404
    assert client.delete(f"/api/v1/studio/snapshots/{snapshot_id}", headers=two).status_code == 404
    assert client.delete(f"/api/v1/studio/snapshots/{snapshot_id}", headers=one).status_code == 204
    assert client.get("/api/v1/studio/snapshots", headers=one).json()["total"] == 0


def test_snapshot_rejects_unsafe_urls_and_oversized_result_sets():
    client = _client()
    headers = _key(client, "snapshot-validation@example.test")
    base = {"name": "bad", "q": "query", "rows": [{"rank": 1,
            "url": "https://good.example", "score": 1}]}
    invalid_url = {**base, "rows": [{**base["rows"][0], "url": "javascript:alert(1)"}]}
    assert client.post("/api/v1/studio/snapshots", headers=headers,
                       json=invalid_url).status_code == 422
    too_many = {**base, "rows": [{**base["rows"][0], "rank": i + 1}
                                  for i in range(101)]}
    assert client.post("/api/v1/studio/snapshots", headers=headers,
                       json=too_many).status_code == 422
