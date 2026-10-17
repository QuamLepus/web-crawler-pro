"""Contract-level coverage for the authenticated tenant provenance ledger."""
import os

import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app


def _client():
    return TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                     enable_graphql=False))


def _auth(client, email):
    signup = client.post("/auth/signup", json={"email": email, "password": "valid-password-123",
                                               "org_name": "Test"})
    assert signup.status_code in (200, 201), signup.text
    access = signup.json()["access_token"]
    key = client.post("/keys", headers={"Authorization": f"Bearer {access}"},
                      json={"name": "test", "scopes": ["read", "crawl"]})
    assert key.status_code == 200, key.text
    return {"X-API-Key": key.json()["key"]}


def test_provenance_store_get_and_isolate_tenants(monkeypatch):
    monkeypatch.setenv("WCP_PROVENANCE_SIGNING_KEY", "test-only-signing-secret")
    client = _client()
    headers = _auth(client, "ledger-a@example.test")
    payload = {"record_id": "item-1", "source_url": "https://example.org/a",
               "fetched_at": "2026-10-18T12:00:00Z", "content_sha256": "a" * 64,
               "license": "unknown", "robots_verdict": "allowed",
               "transformations": ["cleaned"], "confidence": 0.8}
    saved = client.post("/api/v1/provenance/records", headers=headers, json=payload)
    assert saved.status_code == 201, saved.text
    assert client.get("/api/v1/provenance/records/item-1", headers=headers).json()["license"] == "unknown"
    other = _auth(client, "ledger-b@example.test")
    assert client.get("/api/v1/provenance/records/item-1", headers=other).status_code == 404
    manifest = client.post("/api/v1/provenance/manifest", headers=headers,
                           json={"dataset_id": "demo"})
    assert manifest.status_code == 200, manifest.text
    assert len(manifest.json()["signature"]["value"]) == 64


def test_graph_resolves_aliases_relations_and_isolates_tenants():
    client = _client()
    owner = _auth(client, "graph-a@example.test")
    company = client.post("/api/v1/graph/entities", headers=owner,
                          json={"name": "Example Corporation", "entity_type": "Company",
                                "aliases": ["Example Corp"]})
    assert company.status_code == 200, company.text
    product = client.post("/api/v1/graph/entities", headers=owner,
                          json={"name": "Widget 1", "entity_type": "Product"})
    assert product.status_code == 200, product.text
    resolved = client.post("/api/v1/graph/entities", headers=owner,
                           json={"name": "  EXAMPLE CORP  ", "entity_type": "Company"})
    assert resolved.json()["entity_id"] == company.json()["entity_id"]
    relation = client.post("/api/v1/graph/relations", headers=owner,
                           json={"source_id": company.json()["entity_id"],
                                 "target_id": product.json()["entity_id"],
                                 "relation": "makes"})
    assert relation.status_code == 201
    assert len(client.get("/api/v1/graph/entities/" + company.json()["entity_id"],
                          headers=owner).json()["relations"]) == 1
    graph = client.get("/api/v1/graph", headers=owner)
    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) == 2 and len(graph.json()["edges"]) == 1
    other = _auth(client, "graph-b@example.test")
    assert client.get("/api/v1/graph", headers=other).json()["nodes"] == []
    assert client.get("/api/v1/graph/entities/" + company.json()["entity_id"],
                      headers=other).status_code == 404


def test_manifest_refuses_implicit_unsigned_export(monkeypatch):
    monkeypatch.delenv("WCP_PROVENANCE_SIGNING_KEY", raising=False)
    client = _client()
    headers = _auth(client, "ledger-c@example.test")
    result = client.post("/api/v1/provenance/manifest", headers=headers,
                         json={"dataset_id": "demo"})
    assert result.status_code == 503
