"""Tenant isolation for webhook delivery history."""
import json

import fakeredis
from fastapi.testclient import TestClient

from src.api.main import create_api_app


def _key(client: TestClient, email: str) -> dict[str, str]:
    signup = client.post("/auth/signup", json={
        "org_name": "Webhook Test", "email": email,
        "password": "correct-horse-123",
    })
    access_token = signup.json()["access_token"]
    key = client.post("/keys", headers={"Authorization": f"Bearer {access_token}"},
                      json={"name": "webhook-test", "scopes": ["read"]})
    return {"X-API-Key": key.json()["key"]}


def test_delivery_history_is_hidden_from_other_tenants():
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False))
    owner = _key(client, "webhook-owner@example.test")
    other = _key(client, "webhook-other@example.test")
    created = client.post("/api/v1/webhooks", headers=owner, json={
        "url": "https://hooks.example.test/receive",
        "events": ["research.completed"],
    })
    assert created.status_code == 201, created.text
    subscription_id = created.json()["id"]

    client.app.state.services.redis.rpush(
        f"webhook:log:{subscription_id}",
        json.dumps({"delivery_id": "dlv_private", "status": "delivered"}),
    )

    assert client.get(f"/api/v1/webhooks/{subscription_id}/deliveries",
                      headers=other).status_code == 404
    owner_history = client.get(f"/api/v1/webhooks/{subscription_id}/deliveries",
                               headers=owner)
    assert owner_history.status_code == 200
    assert owner_history.json() == [{"delivery_id": "dlv_private", "status": "delivered"}]
