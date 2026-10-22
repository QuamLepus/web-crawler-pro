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
    tenant = client.app.state.services.users.list_orgs()[0].id

    payload = {
        "url": "https://example.com/", "max_pages": 1, "tenant": tenant,
        "job_id": "job_mkt", "obey_robots": False,
    }
    job_r.hset("wcp:v1:job:job_mkt", mapping={
        "tenant": tenant, "state": "queued", "items_collected": 0,
        "tokens_generated": 0,
    })
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


def test_marketplace_publish_requires_own_completed_crawl_and_uses_its_counters(client, api_key):
    tenant = client.app.state.services.users.list_orgs()[0].id
    redis = client.app.state.services.redis
    digest = "a" * 64
    payload = {
        "title": "Verified corpus",
        "description": "A dataset backed by a completed crawl job with recorded counters.",
        "price_usd_cents": 250,
    }

    redis.hset("wcp:v1:job:job_foreign", mapping={
        "tenant": "another-tenant", "state": "completed", "data_sha256": digest,
        "items_collected": 99, "tokens_generated": 777,
    })
    foreign = client.post("/v1/marketplace/datasets", headers=_headers(api_key),
                          json={**payload, "source_job_id": "job_foreign"})
    assert foreign.status_code == 404

    redis.hset("wcp:v1:job:job_running", mapping={
        "tenant": tenant, "state": "running", "data_sha256": digest,
        "items_collected": 3, "tokens_generated": 24,
    })
    running = client.post("/v1/marketplace/datasets", headers=_headers(api_key),
                          json={**payload, "source_job_id": "job_running"})
    assert running.status_code == 422

    redis.hset("wcp:v1:job:job_owned", mapping={
        "tenant": tenant, "state": "completed", "data_sha256": digest,
        "items_collected": 7, "tokens_generated": 31,
    })
    published = client.post("/v1/marketplace/datasets", headers=_headers(api_key),
                            json={**payload, "source_job_id": "job_owned"})
    assert published.status_code == 201, published.text
    from src.marketplace.service import MarketplaceService
    listing = MarketplaceService(redis).get_listing(published.json()["dataset_id"])
    assert listing.record_count == 7
    assert listing.token_count == 31


def test_marketplace_listing_reads_keep_unpublished_inventory_private(client, api_key):
    from src.marketplace.service import MarketplaceService

    tenant = client.app.state.services.users.list_orgs()[0].id
    marketplace = MarketplaceService(client.app.state.services.redis)

    def listing(owner, title, digest):
        return marketplace.create_listing(owner, title=title,
            description="A published corpus used to verify tenant visibility rules.",
            tags=["visibility"], price_usd_cents=100, license="CC-BY-4.0",
            content_sha256=digest * 64)

    public = listing("seller-public", "Public dataset", "a")
    private = listing("seller-private", "Private draft", "b")
    marketplace.update_listing(private.dataset_id, "seller-private", status="draft")
    own_draft = listing(tenant, "Tenant draft", "c")
    marketplace.update_listing(own_draft.dataset_id, tenant, status="draft")

    headers = _headers(api_key)
    assert client.get(f"/v1/marketplace/datasets/{public.dataset_id}",
                      headers=headers).status_code == 200
    assert client.get(f"/v1/marketplace/datasets/{private.dataset_id}",
                      headers=headers).status_code == 404
    assert client.get(f"/v1/marketplace/datasets/{own_draft.dataset_id}",
                      headers=headers).status_code == 200
    search = client.get("/v1/marketplace/datasets", headers=headers).json()
    visible_ids = {item["dataset_id"] for item in search}
    assert public.dataset_id in visible_ids
    assert private.dataset_id not in visible_ids
    assert own_draft.dataset_id not in visible_ids


def test_marketplace_seller_inventory_is_scoped_and_filters_status(client, api_key):
    from src.marketplace.service import MarketplaceService

    seller = client.app.state.services.users.list_orgs()[0].id
    marketplace = MarketplaceService(client.app.state.services.redis)

    def create(owner, title, digest):
        return marketplace.create_listing(
            owner, title=title,
            description="Listing used to test seller inventory privacy and filters.",
            tags=["inventory"], price_usd_cents=0, license="CC-BY-4.0",
            content_sha256=digest * 64)

    own_published = create(seller, "Seller published item", "a")
    own_draft = create(seller, "Seller draft item", "b")
    marketplace.update_listing(own_draft.dataset_id, seller, status="draft")
    foreign_draft = create("another-seller", "Foreign draft item", "c")
    marketplace.update_listing(foreign_draft.dataset_id, "another-seller", status="draft")

    headers = _headers(api_key)
    inventory = client.get("/v1/marketplace/my/datasets", headers=headers)
    assert inventory.status_code == 200, inventory.text
    ids = {item["dataset_id"] for item in inventory.json()}
    assert ids == {own_published.dataset_id, own_draft.dataset_id}

    drafts = client.get("/v1/marketplace/my/datasets", headers=headers,
                        params={"status": "draft"})
    assert {item["dataset_id"] for item in drafts.json()} == {own_draft.dataset_id}
    invalid = client.get("/v1/marketplace/my/datasets", headers=headers,
                         params={"status": "deleted"})
    assert invalid.status_code == 422

    foreign_patch = client.patch(
        f"/v1/marketplace/datasets/{foreign_draft.dataset_id}", headers=headers,
        json={"title": "Attempted foreign edit"})
    assert foreign_patch.status_code == 404
    assert foreign_patch.json()["detail"] == "dataset not found"
    assert marketplace.get_listing(foreign_draft.dataset_id).title == "Foreign draft item"


def test_marketplace_listing_patch_rejects_invalid_values_and_unknown_fields(client, api_key):
    from src.marketplace.service import MarketplaceService

    seller = client.app.state.services.users.list_orgs()[0].id
    marketplace = MarketplaceService(client.app.state.services.redis)
    listing = marketplace.create_listing(
        seller, title="Patch validation target",
        description="Dataset used to test marketplace listing patch validation.",
        tags=["validation"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="a" * 64)
    url = f"/v1/marketplace/datasets/{listing.dataset_id}"
    headers = _headers(api_key)

    for payload in (
        {"price_usd_cents": -1},
        {"status": "hidden"},
        {"seller_tenant": "attacker"},
        {"title": None},
        {},
    ):
        response = client.patch(url, headers=headers, json=payload)
        assert response.status_code == 422, (payload, response.text)
    assert marketplace.get_listing(listing.dataset_id).price_usd_cents == 0

    valid = client.patch(url, headers=headers,
                         json={"status": "draft", "price_usd_cents": 0})
    assert valid.status_code == 200, valid.text
    assert valid.json()["status"] == "draft"


def test_marketplace_tag_updates_normalize_and_refresh_search_indexes(client):
    from src.marketplace.service import MarketplaceError, MarketplaceService

    marketplace = MarketplaceService(client.app.state.services.redis)
    listing = marketplace.create_listing(
        "seller", title="Tag index test dataset",
        description="Dataset used to validate marketplace tag-index updates.",
        tags=[" Old Tag ", "old tag"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="a" * 64)
    assert listing.tags == ["old tag"]
    assert listing.dataset_id in {
        item.dataset_id for item in marketplace.search_listings(tag="OLD TAG")}

    updated = marketplace.update_listing(
        listing.dataset_id, "seller", tags=[" New Tag ", "FRESH", "fresh"])

    assert updated.tags == ["new tag", "fresh"]
    assert listing.dataset_id not in {
        item.dataset_id for item in marketplace.search_listings(tag="old tag")}
    assert listing.dataset_id in {
        item.dataset_id for item in marketplace.search_listings(tag="NEW TAG")}
    assert listing.dataset_id in {
        item.dataset_id for item in marketplace.search_listings(tag="fresh")}
    with pytest.raises(MarketplaceError, match="at most 15 strings"):
        marketplace.update_listing(listing.dataset_id, "seller", tags="invalid")
    assert marketplace.get_listing(listing.dataset_id).tags == ["new tag", "fresh"]


def test_marketplace_search_scans_past_the_first_501_listings(client):
    from src.marketplace.service import MarketplaceService

    marketplace = MarketplaceService(client.app.state.services.redis)
    oldest = marketplace.create_listing(
        "seller", title="Rare marker collection",
        description="A dataset deliberately placed beyond the original search cap.",
        tags=["catalog"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="a" * 64)
    newer_ids = []
    for i in range(501):
        listing = marketplace.create_listing(
            "seller", title=f"Common collection {i}",
            description="A recent dataset without the target terms.",
            tags=["catalog"], price_usd_cents=0, license="CC-BY-4.0",
            content_sha256=f"{i + 1:064x}")
        newer_ids.append(listing.dataset_id)
    marketplace.r.zadd("mkt:listings:new", {oldest.dataset_id: 0, **{
        dataset_id: float(rank) for rank, dataset_id in enumerate(newer_ids, start=1)}})

    results = marketplace.search_listings(query="rare marker", limit=1)

    assert [listing.dataset_id for listing in results] == [oldest.dataset_id]


def test_marketplace_purchase_fails_closed_without_strong_signing_secret(
        client, api_key, monkeypatch):
    from src.marketplace.service import MarketplaceService

    marketplace = MarketplaceService(client.app.state.services.redis)
    listing = marketplace.create_listing(
        "seller", title="Signing test dataset",
        description="Dataset used to check fail-closed marketplace signatures.",
        tags=["signing"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="a" * 64)
    paid = marketplace.create_listing(
        "seller", title="Paid test dataset",
        description="Dataset used to verify unavailable paid checkout is refused.",
        tags=["payment"], price_usd_cents=100, license="CC-BY-4.0",
        content_sha256="b" * 64)
    headers = _headers(api_key)
    monkeypatch.delenv("MARKETPLACE_SIGNING_SECRET", raising=False)

    unpaid_checkout = client.post(
        f"/v1/marketplace/datasets/{paid.dataset_id}/purchase", headers=headers)
    assert unpaid_checkout.status_code == 503
    assert "settlement" in unpaid_checkout.json()["detail"]

    missing = client.post(
        f"/v1/marketplace/datasets/{listing.dataset_id}/purchase", headers=headers)
    assert missing.status_code == 503
    assert "signing" in missing.json()["detail"]
    assert client.get("/v1/marketplace/purchases", headers=headers).json() == []

    monkeypatch.setenv("MARKETPLACE_SIGNING_SECRET", "short-secret")
    weak = client.post(
        f"/v1/marketplace/datasets/{listing.dataset_id}/purchase", headers=headers)
    assert weak.status_code == 503
    assert "short-secret" not in weak.text


def test_marketplace_reviews_omit_buyer_identity_and_hide_unpublished_listings(
        client, api_key, monkeypatch):
    from src.marketplace.service import MarketplaceService

    monkeypatch.setenv("MARKETPLACE_SIGNING_SECRET",
                       "marketplace-test-signing-secret-0001")
    marketplace = MarketplaceService(client.app.state.services.redis)
    public = marketplace.create_listing(
        "seller-public", title="Public review target",
        description="A public dataset used to verify review privacy behavior.",
        tags=["reviews"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="f" * 64)
    draft = marketplace.create_listing(
        "seller-private", title="Private review target",
        description="An unpublished dataset hidden from other marketplace tenants.",
        tags=["reviews"], price_usd_cents=100, license="CC-BY-4.0",
        content_sha256="e" * 64)
    marketplace.update_listing(draft.dataset_id, "seller-private", status="draft")
    headers = _headers(api_key)

    review_url = f"/v1/marketplace/datasets/{public.dataset_id}/reviews"
    marketplace.purchase("different-buyer", public.dataset_id)
    unpurchased = client.post(review_url, headers=headers,
                              json={"rating": 5, "comment": "Useful data."})
    assert unpurchased.status_code == 403
    purchase = client.post(
        f"/v1/marketplace/datasets/{public.dataset_id}/purchase", headers=headers)
    assert purchase.status_code == 200, purchase.text
    history = client.get("/v1/marketplace/purchases", headers=headers,
                         params={"limit": 10})
    assert history.status_code == 200, history.text
    assert len(history.json()) == 1
    assert history.json()[0]["purchase_id"] == purchase.json()["purchase"]["purchase_id"]
    assert "buyer_tenant" not in history.json()[0]
    posted = client.post(review_url, headers=headers,
                         json={"rating": 5, "comment": "Useful data."})
    assert posted.status_code == 201, posted.text
    assert posted.json()["verified_purchase"] is True
    duplicate = client.post(review_url, headers=headers,
                            json={"rating": 4, "comment": "Another useful review."})
    assert duplicate.status_code == 409
    reviews = client.get(f"/v1/marketplace/datasets/{public.dataset_id}/reviews",
                         headers=headers, params={"limit": 10})
    assert reviews.status_code == 200, reviews.text
    assert len(reviews.json()) == 1
    assert reviews.json()[0]["rating"] == 5
    assert "buyer_tenant" not in reviews.json()[0]

    hidden = client.get(f"/v1/marketplace/datasets/{draft.dataset_id}/reviews",
                        headers=headers)
    assert hidden.status_code == 404
    hidden_post = client.post(
        f"/v1/marketplace/datasets/{draft.dataset_id}/reviews", headers=headers,
        json={"rating": 5, "comment": "Useful data."})
    assert hidden_post.status_code == 404
    assert hidden_post.json()["detail"] == "dataset not found"


def test_marketplace_review_uniqueness_backfills_preexisting_reviews(client):
    import json

    from src.marketplace.service import MarketplaceError, MarketplaceService

    marketplace = MarketplaceService(client.app.state.services.redis)
    listing = marketplace.create_listing(
        "seller", title="Legacy review target",
        description="Dataset used to test review migration and uniqueness rules.",
        tags=["review"], price_usd_cents=0, license="CC-BY-4.0",
        content_sha256="d" * 64)
    buyer = "existing-buyer"
    marketplace.r.sadd(f"mkt:buyers:{listing.dataset_id}", buyer)
    legacy = {
        "review_id": "legacyreview1", "dataset_id": listing.dataset_id,
        "buyer_tenant": buyer, "rating": 5, "comment": "Legacy review.",
        "verified_purchase": True, "created_at": "2025-01-01T00:00:00+00:00",
    }
    marketplace.r.hset("mkt:review:legacyreview1", "data", json.dumps(legacy))
    marketplace.r.rpush(f"mkt:reviews:{listing.dataset_id}", "legacyreview1")

    with pytest.raises(MarketplaceError, match="already reviewed"):
        marketplace.add_review(buyer, listing.dataset_id, 4, "Duplicate attempt.")

    marker = marketplace.r.get(f"mkt:reviewer:{listing.dataset_id}:{buyer}")
    assert marker in (b"legacyreview1", "legacyreview1")
    assert marketplace.r.llen(f"mkt:reviews:{listing.dataset_id}") == 1


def test_marketplace_bounty_submissions_require_owned_published_unique_listings(client):
    from src.marketplace.service import MarketplaceError, MarketplaceService

    marketplace = MarketplaceService(client.app.state.services.redis)
    bounty = marketplace.create_bounty(
        "poster", title="Collect public records",
        spec="Collect and curate a set of public records from approved sources.",
        reward_usd_cents=5000, deadline_days=10, target_urls=[])

    def listing(owner, title, digest):
        return marketplace.create_listing(
            owner, title=title,
            description="Dataset used to verify bounty submission ownership rules.",
            tags=["bounty"], price_usd_cents=100, license="CC-BY-4.0",
            content_sha256=digest * 64)

    draft = listing("worker", "Draft dataset", "a")
    marketplace.update_listing(draft.dataset_id, "worker", status="draft")
    published = listing("other-worker", "Another worker's dataset", "b")
    accepted = listing("worker", "Worker's public dataset", "c")

    with pytest.raises(MarketplaceError, match="published listing"):
        marketplace.submit_for_bounty("worker", bounty.bounty_id, draft.dataset_id)
    with pytest.raises(MarketplaceError, match="listing owner"):
        marketplace.submit_for_bounty("worker", bounty.bounty_id, published.dataset_id)

    result = marketplace.submit_for_bounty("worker", bounty.bounty_id,
                                           accepted.dataset_id)
    assert result["queued_for_review"] is True
    with pytest.raises(MarketplaceError, match="already submitted"):
        marketplace.submit_for_bounty("worker", bounty.bounty_id,
                                      accepted.dataset_id)
    assert marketplace.get_bounty(bounty.bounty_id).submissions == 1

    awarded = marketplace.award_bounty("poster", bounty.bounty_id,
                                       accepted.dataset_id)
    assert awarded["awarded_to_dataset"] == accepted.dataset_id
    with pytest.raises(MarketplaceError, match="bounty is awarded"):
        marketplace.award_bounty("poster", bounty.bounty_id, accepted.dataset_id)

    expired = marketplace.create_bounty(
        "poster", title="Expired public records",
        spec="Collect and curate a set of public records from approved sources.",
        reward_usd_cents=5000, deadline_days=0, target_urls=[])
    with pytest.raises(MarketplaceError, match="bounty expired"):
        marketplace.award_bounty("poster", expired.bounty_id, accepted.dataset_id)
    assert marketplace.get_bounty(expired.bounty_id).status == "expired"


def test_open_bounty_listing_expires_deadlines_and_fills_result_limit(client):
    from src.marketplace.service import MarketplaceService

    marketplace = MarketplaceService(client.app.state.services.redis)
    expired = marketplace.create_bounty(
        "poster", title="Past deadline bounty",
        spec="Collect and curate a set of public records from approved sources.",
        reward_usd_cents=5000, deadline_days=0, target_urls=[])
    current = marketplace.create_bounty(
        "poster", title="Current public bounty",
        spec="Collect and curate a set of public records from approved sources.",
        reward_usd_cents=5000, deadline_days=10, target_urls=[])

    listed = marketplace.list_open_bounties(limit=2)

    assert [bounty.bounty_id for bounty in listed] == [current.bounty_id]
    assert marketplace.get_bounty(expired.bounty_id).status == "expired"
    assert marketplace.r.zscore("mkt:bounties:open", expired.bounty_id) is None


def test_marketplace_preview_hides_other_tenants_drafts_and_never_fabricates_rows(client, api_key):
    from src.marketplace.service import MarketplaceService

    tenant = client.app.state.services.users.list_orgs()[0].id
    marketplace = MarketplaceService(client.app.state.services.redis)

    def listing(owner, title, digest):
        return marketplace.create_listing(owner, title=title,
            description="A corpus used to test preview access and availability.",
            tags=["preview"], price_usd_cents=100, license="CC-BY-4.0",
            content_sha256=digest * 64)

    private_draft = listing("seller-private", "Private draft", "d")
    marketplace.update_listing(private_draft.dataset_id, "seller-private", status="draft")
    own_draft = listing(tenant, "Own draft", "e")
    marketplace.update_listing(own_draft.dataset_id, tenant, status="draft")
    headers = _headers(api_key)

    hidden = client.get(
        f"/v1/marketplace/datasets/{private_draft.dataset_id}/preview?rows=10",
        headers=headers)
    unavailable = client.get(
        f"/v1/marketplace/datasets/{own_draft.dataset_id}/preview?rows=10",
        headers=headers)
    assert hidden.status_code == 404
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "dataset preview is unavailable"
    assert "sample row" not in unavailable.text


def test_dataset_publish_still_rejects_unhashed_jobs(client, api_key):
    r = client.post("/v1/marketplace/datasets", headers=_headers(api_key),
                    json={"title": "No Digest",
                          "description": ("This job was never crawled so it "
                                          "has no content digest at all."),
                          "price_usd_cents": 100,
                          "source_job_id": "job_never_ran"})
    assert r.status_code == 404


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
