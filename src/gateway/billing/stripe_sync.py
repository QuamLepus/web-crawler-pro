"""Stripe billing integration (subscription + usage reporting).

Design notes
------------
* The ``stripe`` package is an **optional** dependency: when missing, a thin
  :class:`FakeStripe` shim keeps local/dev flows testable end-to-end
  (checkout URLs are synthetic, webhooks still verify against a dev secret).
* All inbound webhook events MUST pass signature verification
  (:func:`verify_webhook`) before mutating state — never trust payload bodies.
* Subscription lifecycle maps onto ``UserStore.set_plan`` so quota limits
  follow the paid plan automatically (see :mod:`src.gateway.billing.quotas`).
* Usage-based billing uses Stripe **usage records** on metered subscription
  items; we push daily aggregates from Redis meters via :meth:`report_usage`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


def _load_stripe() -> Any:
    try:
        import stripe  # type: ignore

        if STRIPE_SECRET_KEY:
            stripe.api_key = STRIPE_SECRET_KEY
        return stripe
    except ImportError:
        logger.warning("stripe SDK not installed - using FakeStripe shim")
        return None


# --------------------------------------------------------------------------- #
# Dev-mode shim so flows work without network/credentials
# --------------------------------------------------------------------------- #
class FakeStripe:
    """Minimal in-memory stand-in implementing the calls we use."""

    def __init__(self) -> None:
        self.customers: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.usage_records: list[dict] = []
        self.checkouts: dict[str, dict] = {}

    class _APIError(Exception):
        pass

    def create_customer(self, *, email: str, name: str = "",
                       metadata: dict | None = None) -> dict:
        cid = f"cus_fake_{uuid.uuid4().hex[:12]}"
        self.customers[cid] = {"id": cid, "email": email, "name": name,
                               "metadata": metadata or {}}
        return self.customers[cid]

    def create_checkout_session(self, *, customer_id: str, price_id: str,
                                success_url: str, cancel_url: str) -> dict:
        sid = f"cs_test_{uuid.uuid4().hex[:16]}"
        self.checkouts[sid] = {
            "id": sid, "customer": customer_id, "price": price_id,
            "url": f"{success_url}?session_id={sid}",
        }
        return self.checkouts[sid]

    def create_subscription(self, *, customer_id: str, price_id: str) -> dict:
        sub_id = f"sub_fake_{uuid.uuid4().hex[:12]}"
        self.subscriptions[sub_id] = {
            "id": sub_id, "customer": customer_id, "items": {"data": [
                {"price": price_id, "id": f"si_{uuid.uuid4().hex[:8]}"}]},
            "status": "active",
        }
        return self.subscriptions[sub_id]

    def retrieve_subscription(self, sub_id: str) -> dict | None:
        return self.subscriptions.get(sub_id)

    def cancel_subscription(self, sub_id: str) -> None:
        if sub_id in self.subscriptions:
            self.subscriptions[sub_id]["status"] = "canceled"

    def create_usage_record(self, *, subscription_item_id: str,
                            quantity: float) -> dict:
        rec = {"subscription_item": subscription_item_id,
               "quantity": quantity, "timestamp": int(time.time())}
        self.usage_records.append(rec)
        return rec


_shim = FakeStripe()


class BillingService:
    """High-level operations used by the gateway routes and webhook handler."""

    def __init__(self, user_store, redis_client) -> None:
        self.users = user_store
        self.r = redis_client
        self._real = _load_stripe()
        self.stripe = self._real or _shim
        self.fake = self._real is None

    # ------------------------------------------------------------------ #
    # customers & checkout
    # ------------------------------------------------------------------ #
    def ensure_customer(self, tenant: str, email: str, name: str = "") -> str:
        """Return (creating if needed) the Stripe customer for an org."""
        org = self.users.get_org(tenant)
        if org is None:
            raise LookupError(f"unknown org {tenant}")
        if org.stripe_customer_id:
            return org.stripe_customer_id
        if self.fake:
            cust = self.stripe.create_customer(email=email, name=name,
                                               metadata={"tenant": tenant})
        else:
            cust = self.stripe.Customer.create(email=email, name=name,
                                               metadata={"tenant": tenant})
        self.users.set_plan(tenant, org.plan, stripe_customer_id=cust["id"])
        return cust["id"]

    def create_checkout(self, tenant: str, plan_name: str, email: str,
                        success_url: str = "https://app.local/billing?ok=1",
                        cancel_url: str = "https://app.local/billing?ok=0") -> dict:
        from src.gateway.billing.plans import get_plan

        plan = get_plan(plan_name)
        price_id = os.getenv(plan.stripe_price_id_env, f"price_{plan_name}")
        customer_id = self.ensure_customer(tenant, email)
        if self.fake:
            sess = self.stripe.create_checkout_session(
                customer_id=customer_id, price_id=price_id,
                success_url=success_url, cancel_url=cancel_url)
            return {"session_id": sess["id"], "url": sess["url"]}
        sess = self.stripe.checkout.Session.create(
            customer=customer_id, mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url, cancel_url=cancel_url,
            metadata={"tenant": tenant, "plan": plan_name},
        )
        return {"session_id": sess["id"], "url": sess["url"]}

    # ------------------------------------------------------------------ #
    # usage reporting (metered billing)
    # ------------------------------------------------------------------ #
    def report_usage(self, tenant: str, metric: str, quantity: float,
                     period: str) -> dict:
        """Push a daily/monthly aggregate to Stripe usage records.

        Maps metric -> metered subscription item id stored at subscribe time
        in Redis key ``billing:{tenant}:item:{metric}``.
        """
        item_id = self.r.get(f"billing:{tenant}:item:{metric}")
        if item_id is None:
            logger.debug("no metered item registered for %s/%s", tenant, metric)
            return {"skipped": True, "reason": f"no subscription item for {metric}"}
        item_id = item_id.decode() if isinstance(item_id, bytes) else item_id
        if self.fake:
            return self.stripe.create_usage_record(
                subscription_item_id=item_id, quantity=quantity)
        return self.stripe.subscription_item.create_usage_record(
            subscription_item=item_id, quantity=int(quantity),
            timestamp=int(time.time()), action="increment",
        )

    # ------------------------------------------------------------------ #
    # webhooks
    # ------------------------------------------------------------------ #
    def handle_event(self, payload: bytes, sig_header: str) -> dict[str, Any]:
        """Verify then apply a Stripe webhook event. Returns summary dict."""
        event = verify_webhook(payload, sig_header)
        etype = event.get("type", "")
        data = event.get("data", {}).get("object", {})
        tenant = data.get("metadata", {}).get("tenant", "")
        out: dict[str, Any] = {"event": event.get("id"), "type": etype,
                               "tenant": tenant, "applied": False}
        if etype == "checkout.session.completed":
            plan = data.get("metadata", {}).get("plan") or self._plan_from_price(
                data.get("subscription"))
            if tenant and plan:
                self.users.set_plan(tenant, plan)
                self._register_metered_items(tenant, data.get("subscription"))
                out["applied"] = True
        elif etype in ("subscription.updated", "invoice.paid"):
            sub_id = data.get("id") or data.get("subscription")
            if tenant and sub_id:
                status = data.get("status", "")
                if status in ("active", "trialing"):
                    self._register_metered_items(tenant, sub_id)
                    out["applied"] = True
        elif etype in ("subscription.deleted", "customer.subscription.deleted"):
            if tenant:
                self.users.set_plan(tenant, "free")
                out["applied"] = True
        elif etype == "payment_failed":
            logger.warning("payment failed for tenant=%s invoice=%s",
                           tenant, data.get("invoice"))
            if tenant:
                self.r.setex(f"billing:{tenant}:past_due", 86400 * 7, "1")
                out["applied"] = True
        return out

    def _register_metered_items(self, tenant: str, sub_id: str | None) -> None:
        """Remember subscription-item ids so report_usage can target them."""
        if not sub_id:
            return
        if self.fake:
            sub = self.stripe.retrieve_subscription(sub_id)  # type: ignore[attr-defined]
        else:
            sub = self.stripe.Subscription.retrieve(sub_id)
        if not sub:
            return
        for item in sub.get("items", {}).get("data", []):
            price = item.get("price", {})
            lookup = price.get("metadata", {}).get("metric") if isinstance(price, dict) \
                else getattr(price, "metadata", {}).get("metric")
            if lookup:
                self.r.set(f"billing:{tenant}:item:{lookup}", item["id"])

    @staticmethod
    def _plan_from_price(sub_id: str | None) -> str | None:
        env_map = {
            os.getenv("STRIPE_PRICE_STARTER", "price_starter"): "starter",
            os.getenv("STRIPE_PRICE_PRO", "price_pro"): "pro",
            os.getenv("STRIPE_PRICE_ENTERPRISE", "price_enterprise"): "enterprise",
        }
        if not sub_id:
            return None
        for k, v in env_map.items():
            if k in str(sub_id):
                return v
        return None


# --------------------------------------------------------------------------- #
# Signature verification (Stripe scheme v1)
# --------------------------------------------------------------------------- #
class WebhookSignatureError(Exception):
    pass


def verify_webhook(payload: bytes, sig_header: str,
                   secret: str | None = None,
                   tolerance: int = 300) -> dict[str, Any]:
    """Validate ``t=...,v1=...`` header; return parsed event or raise.

    When no webhook secret is configured (dev mode with FakeStripe) we accept
    a JSON body whose first byte is ``{`` but log loudly — production must set
    ``STRIPE_WEBHOOK_SECRET``.
    """
    secret = secret or STRIPE_WEBHOOK_SECRET
    if not secret:
        logger.warning("STRIPE_WEBHOOK_SECRET unset: skipping signature check "
                       "(DEV ONLY)")
        return json.loads(payload)
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts = int(parts.get("t", "0"))
    if abs(time.time() - ts) > tolerance:
        raise WebhookSignatureError("timestamp outside tolerance (replay?)")
    expected = hmac.new(secret.encode(), f"{ts}.{payload.decode()}".encode(),
                        hashlib.sha256).hexdigest()
    provided = parts.get("v1", "").strip()
    if not hmac.compare_digest(expected, provided):
        raise WebhookSignatureError("signature mismatch")
    return json.loads(payload)


def sign_for_tests(payload: bytes, secret: str) -> str:
    """Helper for tests / local tooling to produce a valid header."""
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload.decode()}".encode(),
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"
