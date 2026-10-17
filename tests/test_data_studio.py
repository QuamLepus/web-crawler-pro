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
        "limit": 50, "sources": ["wikipedia", "curlie", "wikipedia"]})
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["sources"] == ["wikipedia", "curlie"]
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
    assert client.get("/api/v1/studio/saved-queries").status_code == 401
