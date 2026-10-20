import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app
from src.intelligence.watch import create_watch, observe, list_events


def _auth(client):
    signup = client.post("/auth/signup", json={"email": "watch@example.test",
                         "password": "valid-password-123", "org_name": "Watch Org"})
    access = signup.json()["access_token"]
    key = client.post("/keys", headers={"Authorization": f"Bearer {access}"},
                      json={"name": "watch", "scopes": ["read", "crawl"]})
    return {"X-API-Key": key.json()["key"]}


def test_watch_api_is_tenant_scoped_and_manual_run_queues_crawl():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False))
    headers = _auth(client)
    made = client.post("/api/v1/watch", headers=headers,
                       json={"url": "https://example.org/pricing", "interval_minutes": 30})
    assert made.status_code == 201, made.text
    watch = made.json()
    assert watch["interval_minutes"] == 30
    run = client.post(f"/api/v1/watch/{watch['watch_id']}/run", headers=headers)
    assert run.status_code == 202, run.text
    assert run.json()["status"] == "queued"
    assert client.get(f"/api/v1/watch/{watch['watch_id']}/changes", headers=headers).json()["items"] == []
    assert client.delete(f"/api/v1/watch/{watch['watch_id']}", headers=headers).status_code == 204
    assert client.post(f"/api/v1/watch/{watch['watch_id']}/run", headers=headers).status_code == 404


def test_watch_rejects_non_http_and_credential_urls():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False))
    headers = _auth(client)
    for url in ("file:///etc/passwd", "https://user:pass@example.org/",
                "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/",
                "http://0.0.0.0/", "http://[::1]/", "http://service.local/"):
        assert client.post("/api/v1/watch", headers=headers,
                           json={"url": url}).status_code == 422


def test_watch_console_renders_scheduler_dependencies():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False), follow_redirects=False)
    signup = client.post("/auth/signup", json={"email": "watch-ui@example.test",
                         "password": "valid-password-123", "org_name": "Watch UI"})
    client.cookies.set("wcp_session", signup.json()["access_token"])
    response = client.get("/app/watch")
    assert response.status_code == 200
    assert "Web monitoring" in response.text
    assert "Celery worker and Beat" in response.text


def test_observation_tracks_snapshot_history_and_price_mentions():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    create_watch(r, tenant="tenant-a", watch_id="watch_abc", url="https://example.org/p",
                 interval_minutes=60)
    first = {"url": "https://example.org/p", "title": "Phone", "text": "Price ৳100"}
    assert observe(r, tenant="tenant-a", watch_id="watch_abc", item=first) is None
    second = {"url": "https://example.org/p", "title": "Phone", "text": "Price ৳90"}
    event = observe(r, tenant="tenant-a", watch_id="watch_abc", item=second)
    assert event["event_type"] == "page.changed"
    assert event["price_mentions_before"] == ["৳100"]
    assert event["price_mentions_after"] == ["৳90"]
    assert len(list_events(r, "watch_abc")) == 1
    assert observe(r, tenant="tenant-b", watch_id="watch_abc", item=first) is None
