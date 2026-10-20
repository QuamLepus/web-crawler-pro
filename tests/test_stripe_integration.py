"""Opt-in Stripe test-mode Checkout integration test.

The test is skipped unless both STRIPE_TEST_SECRET_KEY and
STRIPE_TEST_PRICE_ID are provided. It refuses live keys and creates no charge.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.stripe_integration


@pytest.fixture()
def stripe_test_runtime(monkeypatch):
    secret_key = os.getenv("STRIPE_TEST_SECRET_KEY", "")
    price_id = os.getenv("STRIPE_TEST_PRICE_ID", "")
    if not secret_key and not price_id:
        pytest.skip(
            "set STRIPE_TEST_SECRET_KEY and STRIPE_TEST_PRICE_ID for Stripe test mode"
        )
    if not secret_key or not price_id:
        pytest.fail(
            "both STRIPE_TEST_SECRET_KEY and STRIPE_TEST_PRICE_ID are required"
        )
    if not secret_key.startswith("sk_test_"):
        pytest.fail("refusing Stripe integration run: key must start with sk_test_")

    import stripe

    monkeypatch.setattr(stripe, "api_key", secret_key)
    price = stripe.Price.retrieve(price_id)
    if not price.get("active") or not price.get("recurring"):
        pytest.fail(
            "STRIPE_TEST_PRICE_ID must be an active recurring test-mode price"
        )

    monkeypatch.setenv("STRIPE_SECRET_KEY", secret_key)
    monkeypatch.setenv("STRIPE_PRICE_PRO", price_id)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_local_integration_test")

    from src.gateway.billing import stripe_sync

    monkeypatch.setattr(
        stripe_sync, "STRIPE_WEBHOOK_SECRET", "whsec_local_integration_test")

    fake_server = fakeredis.FakeServer()
    redis_client = fakeredis.FakeStrictRedis(
        server=fake_server, decode_responses=False)
    from src.api.main import create_api_app

    app = create_api_app(
        redis_client=redis_client,
        enable_graphql=False,
        enable_rate_limit=False,
        enable_webui=False,
        enable_celery=False,
    )
    with TestClient(app) as client:
        yield client, app, stripe, price


def test_checkout_and_signed_subscription_activation_in_stripe_test_mode(
        stripe_test_runtime):
    """Create a real test-mode Checkout Session, then apply a signed completion."""
    client, app, stripe, price = stripe_test_runtime
    suffix = uuid.uuid4().hex[:12]
    signup = client.post("/auth/signup", json={
        "org_name": f"Stripe Integration {suffix}",
        "email": f"stripe-e2e-{suffix}@example.invalid",
        "password": f"T3st-{suffix}-Password!",
    })
    assert signup.status_code == 200, signup.text
    tenant = signup.json()["org_id"]
    token = signup.json()["access_token"]
    key_response = client.post(
        "/keys", headers={"Authorization": f"Bearer {token}"},
        json={"name": "Stripe integration"})
    assert key_response.status_code == 200, key_response.text
    api_key = key_response.json()["key"]
    headers = {"X-API-Key": api_key}

    session_id = None
    customer_id = None
    try:
        checkout = client.post("/v1/billing/checkout", headers=headers, json={
            "plan": "pro",
            "success_url": "https://example.com/stripe-success",
            "cancel_url": "https://example.com/stripe-cancel",
        })
        assert checkout.status_code == 200, checkout.text
        result = checkout.json()
        session_id = result["session_id"]
        org = app.state.services.users.get_org(tenant)
        customer_id = org.stripe_customer_id
        assert result["url"].startswith("https://checkout.stripe.com/")

        remote_session = stripe.checkout.Session.retrieve(
            session_id, expand=["line_items"])
        assert remote_session["mode"] == "subscription"
        assert remote_session["metadata"]["tenant"] == tenant
        assert remote_session["metadata"]["plan"] == "pro"
        customer_id = remote_session["customer"]
        assert customer_id
        line_items = remote_session["line_items"]["data"]
        assert line_items[0]["price"]["id"] == price["id"]
        assert price["id"] == os.environ["STRIPE_PRICE_PRO"]

        # Stripe has created the real test-mode session; no payment is taken.
        # Exercise the API's signature-verified webhook route with a compact
        # signed event payload. Stripe cannot post to this private CI process.
        event = {
            "id": f"evt_local_{suffix}",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": session_id,
                "customer": customer_id,
                "metadata": {"tenant": tenant, "plan": "pro"},
                "subscription": None,
            }},
        }
        payload = json.dumps(event, separators=(",", ":")).encode()
        signing_secret = "whsec_local_integration_test"
        signature = stripe_sync_signature(payload, signing_secret)
        webhook = client.post(
            "/api/v1/billing/stripe/webhook",
            content=payload,
            headers={"stripe-signature": signature},
        )
        assert webhook.status_code == 200, webhook.text
        assert webhook.json()["applied"] is True
        assert webhook.json()["tenant"] == tenant

        usage = client.get("/v1/billing/usage", headers=headers)
        assert usage.status_code == 200, usage.text
        assert usage.json()["plan"] == "pro"
    finally:
        if customer_id is None:
            org = app.state.services.users.get_org(tenant)
            customer_id = org.stripe_customer_id if org else None
        if session_id:
            try:
                stripe.checkout.Session.expire(session_id)
            except stripe.StripeError as exc:  # best-effort cleanup
                logger.warning(
                    "could not expire test checkout session (%s)", type(exc).__name__
                )
        if customer_id:
            try:
                stripe.Customer.delete(customer_id)
            except stripe.StripeError as exc:  # never log customer payloads
                logger.warning(
                    "could not delete Stripe test customer (%s)", type(exc).__name__
                )


def stripe_sync_signature(payload: bytes, secret: str) -> str:
    from src.gateway.billing.stripe_sync import sign_for_tests

    return sign_for_tests(payload, secret)
