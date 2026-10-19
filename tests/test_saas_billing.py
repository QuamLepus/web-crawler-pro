"""Billing + marketplace regressions for the hosted API surface.

Every endpoint here returned 5xx on a freshly self-hosted box before the
accompanying fixes, so these are pinned as end-to-end tests rather than unit
tests of the helpers.
"""

from __future__ import annotations

import fakeredis
import pytest
import redis as real_redis
from fastapi.testclient import TestClient


@pytest.fixture()
def fake_server():
    return fakeredis.FakeServer()


@pytest.fixture(autouse=True)
def patched_redis(monkeypatch, fake_server):
    def _from_url(url, **kw):
        return fakeredis.FakeStrictRedis(
            server=fake_server,
            decode_responses=kw.get("decode_responses", True))

    monkeypatch.setattr(real_redis, "from_url", _from_url)
    monkeypatch.setattr(real_redis.Redis, "from_url",
                        classmethod(lambda cls, url, **kw: _from_url(url, **kw)))
    yield fake_server


@pytest.fixture()
def client(patched_redis):
    from src.api.main import create_api_app

    redis_client = fakeredis.FakeStrictRedis(server=patched_redis,
                                             decode_responses=False)
    app = create_api_app(redis_client=redis_client)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def api_key(client):
    tok = client.post("/auth/signup", json={
        "org_name": "Acme Labs", "email": "ada@example.com",
        "password": "s3cret-passw0rd!"}).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    return client.post("/keys", headers=auth, json={"name": "k"}).json()["key"]


def _headers(api_key: str) -> dict:
    return {"X-API-Key": api_key}


SAMPLE_HTML = (
    "<html><title>Example</title><body>hello world, this page is long "
    "enough for the cleaning pipeline to keep it.</body></html>")


# --------------------------------------------------------------------------- #
# billing
# --------------------------------------------------------------------------- #
def test_usage_endpoint_returns_200(client, api_key):
    """/v1/billing/usage 500'd: QuotaStatus.pct is a property, not a method."""
    r = client.get("/v1/billing/usage", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"] in ("free", "starter", "pro", "enterprise")
    assert 0.0 <= body["quota_remaining_pct"] <= 100.0


def test_usage_quota_remaining_reflects_consumption(client, api_key,
                                                    monkeypatch):
    from src.gateway.billing.quotas import QuotaStatus

    snap = {"requests": QuotaStatus(metric="requests", period="2026-10",
                                    used=25.0, limit=100.0, remaining=75.0)}
    monkeypatch.setattr(
        "src.gateway.billing.quotas.QuotaService.snapshot",
        lambda self, tenant: snap)

    body = client.get("/v1/billing/usage",
                      headers=_headers(api_key)).json()
    # pct is already 0-100; headroom must be what is left below the tightest
    # quota (25% used -> 75% remaining), not min(pct) * 100.
    assert body["quota_remaining_pct"] == 75.0


def test_invoice_endpoint_returns_200(client, api_key):
    """/v1/billing/invoice 500'd: `_usage_totals` float()'d the tenant id."""
    r = client.get("/v1/billing/invoice", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json()["tenant"]


def test_checkout_works_without_stripe_credentials(client, api_key,
                                                   monkeypatch):
    """Unconfigured billing must use the dev shim, not answer 502."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    r = client.post("/v1/billing/checkout", headers=_headers(api_key), json={
        "plan": "pro",
        "success_url": "https://app.example/ok",
        "cancel_url": "https://app.example/cancel"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"]
    assert body["url"].startswith("https://app.example/ok")


# --------------------------------------------------------------------------- #
# crawl -> marketplace, the loop that makes the product a marketplace
# --------------------------------------------------------------------------- #
def test_dataset_can_be_published_after_a_crawl(client, api_key, fake_server,
                                                monkeypatch, tmp_path):
    """The marketplace needs `data_sha256` on the source job record.

    Nothing ever wrote that field, so /v1/marketplace/datasets rejected every
    listing with a 422 and the marketplace had no way to acquire inventory.
    """
    import workers.crawl_tasks as wt

    monkeypatch.setattr(wt, "CRAWL_OUTPUT_DIR", str(tmp_path))
    job_r = fakeredis.FakeStrictRedis(server=fake_server, decode_responses=True)

    payload = {
        "url": "https://example.com/", "max_pages": 1, "tenant": "t1",
        "job_id": "job_mkt", "obey_robots": False,
    }
    wt.run_crawl(payload, redis_client=job_r,
                 fetcher=lambda url: (SAMPLE_HTML, 200))
    assert job_r.hget("wcp:v1:job:job_mkt", "data_sha256")

    r = client.post("/v1/marketplace/datasets", headers=_headers(api_key),
                    json={"title": "Example Corpus",
                          "description": "Crawled pages from example.com, "
                                         "cleaned and deduplicated.",
                          "price_usd_cents": 500, "tags": ["news"],
                          "source_job_id": "job_mkt"})
    assert r.status_code == 201, r.text
    assert r.json()["dataset_id"]


def test_dataset_publish_still_rejects_unhashed_jobs(client, api_key):
    r = client.post("/v1/marketplace/datasets", headers=_headers(api_key),
                    json={"title": "No Digest",
                          "description": ("This job was never crawled so it "
                                          "has no content digest at all."),
                          "price_usd_cents": 100,
                          "source_job_id": "job_never_ran"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# inbound Stripe webhook endpoint
# --------------------------------------------------------------------------- #
def test_signed_stripe_webhook_updates_tenant_plan(client, api_key, monkeypatch):
    import json

    from src.gateway.billing import stripe_sync

    secret = "whsec_unit_test_only"
    monkeypatch.setattr(stripe_sync, "STRIPE_WEBHOOK_SECRET", secret)
    org = client.app.state.services.users.list_orgs()[0]
    event = {
        "id": "evt_checkout_completed_unit",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_unit",
            "metadata": {"tenant": org.id, "plan": "pro"},
            "subscription": None,
        }},
    }
    payload = json.dumps(event, separators=(",", ":")).encode()
    signature = stripe_sync.sign_for_tests(payload, secret)

    response = client.post(
        "/api/v1/billing/stripe/webhook",
        content=payload,
        headers={"stripe-signature": signature},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert client.app.state.services.users.get_org(org.id).plan == "pro"


def test_stripe_webhook_rejects_invalid_signature(client, monkeypatch):
    import time

    from src.gateway.billing import stripe_sync

    monkeypatch.setattr(stripe_sync, "STRIPE_WEBHOOK_SECRET", "whsec_unit_test_only")
    response = client.post(
        "/api/v1/billing/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": f"t={int(time.time())},v1=invalid"},
    )
    assert response.status_code == 400
