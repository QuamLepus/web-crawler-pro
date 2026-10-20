import json
import time

import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app
from src.intelligence import watch as watch_store
from src.intelligence.watch import create_watch, observe, list_events, list_snapshots


def _auth(client, email="watch@example.test"):
    signup = client.post("/auth/signup", json={"email": email,
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


def test_watch_snapshots_are_bounded_and_tenant_scoped():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False))
    owner = _auth(client)
    made = client.post("/api/v1/watch", headers=owner,
                       json={"url": "https://example.org/archive"})
    watch = made.json()
    redis = client.app.state.services.redis
    observe(redis, tenant=watch["tenant"], watch_id=watch["watch_id"],
            item={"url": watch["url"], "title": "Baseline", "text": "x" * 6000})
    observe(redis, tenant=watch["tenant"], watch_id=watch["watch_id"],
            item={"url": watch["url"], "title": "Updated", "text": "current"})

    response = client.get(f"/api/v1/watch/{watch['watch_id']}/snapshots", headers=owner)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["text"] == "current"
    assert items[0]["baseline"] is False
    assert len(items[1]["text"]) == 5000
    assert items[1]["truncated"] is True
    assert response.json()["retention_days"] == 90

    other = _auth(client, email="other-watch@example.test")
    assert client.get(f"/api/v1/watch/{watch['watch_id']}/snapshots",
                      headers=other).status_code == 404


def test_watch_rejects_non_http_and_credential_urls():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False))
    headers = _auth(client)
    for url in ("file:///etc/passwd", "https://user:pass@example.org/",
                "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/",
                "http://0.0.0.0/", "http://[::1]/", "http://service.local/"):
        assert client.post("/api/v1/watch", headers=headers,
                           json={"url": url}).status_code == 422


def test_watch_snapshot_history_prunes_expired_versions():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    watch_id = "watch_12121212121212121212"
    key = watch_store.SNAPSHOTS.format(watch_id=watch_id)
    expired = {"snapshot_id": "expired", "captured_at": time.time() -
               watch_store.SNAPSHOT_RETENTION_SECONDS - 1, "text": "old"}
    redis.rpush(key, json.dumps(expired))
    create_watch(redis, tenant="tenant", watch_id=watch_id,
                 url="https://example.org/history", interval_minutes=60)
    observe(redis, tenant="tenant", watch_id=watch_id,
            item={"url": "https://example.org/history", "title": "Current", "text": "now"})
    snapshots = list_snapshots(redis, watch_id)
    assert len(snapshots) == 1
    assert snapshots[0]["text"] == "now"


def test_watch_snapshot_history_is_capped_per_watch():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    watch_id = "watch_ffffffffffffffffffff"
    url = "https://example.org/history"
    create_watch(redis, tenant="tenant", watch_id=watch_id, url=url, interval_minutes=60)
    for version in range(25):
        observe(redis, tenant="tenant", watch_id=watch_id,
                item={"url": url, "title": "Page", "text": f"version {version}"})
    snapshots = list_snapshots(redis, watch_id)
    assert len(snapshots) == 20
    assert snapshots[0]["text"] == "version 24"
    assert snapshots[-1]["text"] == "version 5"


def test_title_only_watch_change_does_not_create_text_excerpt():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    watch_id = "watch_eeeeeeeeeeeeeeeeeeee"
    create_watch(redis, tenant="tenant", watch_id=watch_id,
                 url="https://example.org/about", interval_minutes=60)
    observe(redis, tenant="tenant", watch_id=watch_id,
            item={"url": "https://example.org/about", "title": "Old", "text": "Same body"})
    event = observe(redis, tenant="tenant", watch_id=watch_id,
                    item={"url": "https://example.org/about", "title": "New", "text": "Same body"})
    assert event["title_changed"] is True
    assert event["text_changed"] is False
    assert event["old_text_excerpt"] == ""
    assert event["new_text_excerpt"] == ""


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
    assert event["text_changed"] is True
    assert "৳100" in event["old_text_excerpt"]
    assert "৳90" in event["new_text_excerpt"]
    assert len(event["old_text_excerpt"]) <= 502
    assert len(event["new_text_excerpt"]) <= 502
    assert len(list_events(r, "watch_abc")) == 1
    assert observe(r, tenant="tenant-b", watch_id="watch_abc", item=first) is None
