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


def test_full_corpus_analytics_uses_safe_filters_and_paginated_aggregates(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.api.routers import search_index as search_router
    from src.search_index.models import Doc
    from src.search_index.store import DocStore

    path = tmp_path / "docs.sqlite3"
    store = DocStore(path)
    docs = [Doc(url=f"https://host-{i}.example/page", title=f"Page {i}",
                source="wiki" if i < 120 else "curlie", authority=i / 100,
                quality=0.9 if i < 120 else 0.7, lang="en" if i % 2 else "fr",
                body="text " * (i % 5 + 1)) for i in range(150)]
    store.add_batch(docs)
    store.conn.execute("UPDATE docs SET indexed_at=CASE WHEN id<=75 "
                        "THEN '2026-05-01T00:00:00Z' ELSE '2026-06-01T00:00:00Z' END")
    store.conn.commit()
    store.close()

    class AnalyticsIndex:
        def analytics(self, **filters):
            return DocStore(path, readonly=True).analytics(**filters)

    monkeypatch.setattr(search_router, "_index", lambda _request: AnalyticsIndex())
    client = _client()
    headers = _key(client, "studio-corpus@example.test")
    first_page = client.get("/api/v1/studio/analytics", headers=headers, params={
        "group_by": "host", "metric": "count", "limit": 20, "offset": 0})
    assert first_page.status_code == 200, first_page.text
    payload = first_page.json()
    assert payload["matched_documents"] == 150
    assert payload["group_count"] == 150
    assert len(payload["rows"]) == 20
    assert payload["rows"][0]["value"] == 1
    last_page = client.get("/api/v1/studio/analytics", headers=headers, params={
        "group_by": "host", "metric": "avg_authority", "limit": 20, "offset": 140})
    assert last_page.status_code == 200
    assert len(last_page.json()["rows"]) == 10
    filtered = client.get("/api/v1/studio/analytics", headers=headers, params={
        "group_by": "host", "source": "wiki", "lang": "en", "limit": 100})
    assert filtered.status_code == 200
    assert filtered.json()["matched_documents"] == 60
    assert filtered.json()["group_count"] == 60
    quality = client.get("/api/v1/studio/analytics", headers=headers, params={
        "group_by": "source", "metric": "avg_quality", "source": "wiki"})
    assert quality.status_code == 200
    assert quality.json()["rows"] == [{"label": "wiki", "value": 0.9}]
    date_filter = client.get("/api/v1/studio/analytics", headers=headers, params={
        "indexed_from": "2026-06-01", "indexed_to": "2026-06-01"})
    assert date_filter.status_code == 200
    assert date_filter.json()["matched_documents"] == 75
    injection = client.get("/api/v1/studio/analytics", headers=headers, params={
        "source": "wiki' OR 1=1 --"})
    assert injection.status_code == 200
    assert injection.json()["matched_documents"] == 0
    assert client.get("/api/v1/studio/analytics", params={
        "group_by": "host"}).status_code == 401
    assert client.get("/api/v1/studio/analytics", headers=headers,
                      params={"offset": 5001}).status_code == 422
    reversed_dates = client.get("/api/v1/studio/analytics", headers=headers, params={
        "indexed_from": "2026-10-18", "indexed_to": "2026-10-17"})
    assert reversed_dates.status_code == 422
