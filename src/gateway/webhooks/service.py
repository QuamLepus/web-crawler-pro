"""Outbound webhooks: signed, retried, tenant-scoped job notifications.

Event types include ``job.completed``, ``job.failed``, ``dataset.ready``,
``quota.warning``, ``marketplace.purchase``, ``bounty.fulfilled``, and
``research.completed``, ``research.failed``, and ``research.cancelled``.

Security: every delivery carries an HMAC-SHA256 signature over
``{timestamp}.{body}`` using the subscription's secret
(``Stripe-style`` headers: ``X-WCP-Signature: t=...,v1=...``). Consumers can
verify with :func:`verify_signature`. Deliveries retry with exponential
backoff + jitter and are recorded per-subscription for replay/debugging.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    # internal engine events
    "job.completed", "job.failed", "dataset.ready",
    "quota.warning", "marketplace.purchase", "bounty.fulfilled",
    # public v1 aliases (src/api/schemas.WebhookEvent)
    "crawl.completed", "crawl.failed", "crawl.cancelled", "data.ready",
    "export.ready", "dataset.sold", "web.changed",
    "research.completed", "research.failed", "research.cancelled",
)

MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 2.0
TIMEOUT_S = 10


@dataclass
class WebhookSubscription:
    id: str
    tenant: str
    url: str
    secret: str
    events: list[str]
    active: bool = True
    created_at: float = field(default_factory=time.time)


@dataclass
class DeliveryAttempt:
    delivery_id: str
    subscription_id: str
    event_type: str
    attempt: int
    status: str                    # delivered | failed | exhausted
    status_code: int | None = None
    error: str | None = None
    ts: float = field(default_factory=time.time)


def sign_body(secret: str, body: bytes, ts: int | None = None) -> tuple[str, int]:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}", ts


def verify_signature(secret: str, body: bytes, header: str,
                     tolerance_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts = int(parts.get("t", "0"))
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


class WebhookService:
    """Manage subscriptions and dispatch events.

    Transport is pluggable: default uses :mod:`urllib` in a thread pool so we
    add no hard dependency; production deployments can inject an aiohttp/httpx
    async sender via ``sender`` callable ``(url, body, headers) -> (status, err)``.
    """

    def __init__(self, redis_client, sender=None) -> None:
        self.r = redis_client
        self._sender = sender or self._default_send
        try:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=8,
                                            thread_name_prefix="webhook")
        except Exception:  # pragma: no cover
            self._pool = None

    # ------------------------------------------------------------------ #
    # subscription CRUD
    # ------------------------------------------------------------------ #
    def subscribe(self, tenant: str, url: str,
                  events: Iterable[str]) -> WebhookSubscription:
        unknown = set(events) - set(EVENT_TYPES)
        if unknown:
            raise ValueError(f"unknown events: {sorted(unknown)}")
        if not url.startswith(("https://", "http://")):
            raise ValueError("url must be http(s)")
        sub = WebhookSubscription(
            id=uuid.uuid4().hex[:12], tenant=tenant, url=url,
            secret="whsec_" + secrets.token_hex(24), events=list(events),
        )
        self.r.set(f"webhook:sub:{sub.id}", json.dumps(asdict(sub)))
        self.r.sadd(f"webhook:subs:tenant:{tenant}", sub.id)
        return sub

    def unsubscribe(self, tenant: str, sub_id: str) -> bool:
        raw = self.r.get(f"webhook:sub:{sub_id}")
        if not raw:
            return False
        sub = WebhookSubscription(**json.loads(raw))
        if sub.tenant != tenant:
            return False
        self.r.delete(f"webhook:sub:{sub_id}")
        self.r.srem(f"webhook:subs:tenant:{tenant}", sub_id)
        return True

    def list_for_tenant(self, tenant: str) -> list[WebhookSubscription]:
        out = []
        for sid in self.r.smembers(f"webhook:subs:tenant:{tenant}"):
            sid_s = sid.decode() if isinstance(sid, bytes) else sid
            raw = self.r.get(f"webhook:sub:{sid_s}")
            if raw:
                out.append(WebhookSubscription(**json.loads(raw)))
        return out

    def subs_for_event(self, tenant: str, event_type: str) -> list[WebhookSubscription]:
        return [s for s in self.list_for_tenant(tenant)
                if s.active and event_type in s.events]

    def get_sub(self, tenant: str, sub_id: str) -> WebhookSubscription | None:
        raw = self.r.get(f"webhook:sub:{sub_id}")
        if not raw:
            return None
        sub = WebhookSubscription(**json.loads(raw))
        return sub if sub.tenant == tenant else None

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def emit(self, tenant: str, event_type: str, data: dict[str, Any],
             *, synchronous: bool = False,
             only_sub: str | None = None) -> list[str]:
        """Queue a signed delivery to all matching subscriptions.

        Returns delivery ids. With ``synchronous=True`` (tests/CLI) performs
        inline with retries; otherwise offloads to the thread pool.
        ``only_sub`` restricts delivery to one subscription id (test endpoint).
        """
        payload = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": event_type,
            "tenant": tenant,
            "created": int(time.time()),
            "data": data,
        }
        body = json.dumps(payload).encode()
        ids = []
        if only_sub is not None:
            sub = self.get_sub(tenant, only_sub)
            targets = [sub] if sub and sub.active else []
        else:
            targets = self.subs_for_event(tenant, event_type)
        for sub in targets:
            did = f"dlv_{uuid.uuid4().hex[:10]}"
            ids.append(did)
            if synchronous:
                self._deliver(did, sub, event_type, body)
            elif self._pool is not None:
                self._pool.submit(self._deliver, did, sub, event_type, body)
            else:  # pragma: no cover
                self._deliver(did, sub, event_type, body)
        return ids

    def _deliver(self, delivery_id: str, sub: WebhookSubscription,
                 event_type: str, body: bytes) -> DeliveryAttempt:
        last: DeliveryAttempt | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            sig_header, _ts = sign_body(sub.secret, body)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "WebCrawlerPro-Webhooks/1.0",
                "X-WCP-Delivery": delivery_id,
                "X-WCP-Event": event_type,
                "X-WCP-Signature": sig_header,
            }
            status_code, err = self._sender(sub.url, body, headers)
            ok = status_code is not None and 200 <= status_code < 300
            last = DeliveryAttempt(
                delivery_id=delivery_id, subscription_id=sub.id,
                event_type=event_type, attempt=attempt,
                status="delivered" if ok else "failed",
                status_code=status_code, error=err,
            )
            self._record(last)
            if ok:
                return last
            if status_code is not None and 400 <= status_code < 500 \
                    and status_code != 429:
                break  # client error: retrying won't help
            time.sleep(min(60.0, BACKOFF_BASE_S ** attempt) * (0.5 + (attempt % 2)))
        last = DeliveryAttempt(
            delivery_id=delivery_id, subscription_id=sub.id,
            event_type=event_type, attempt=last.attempt if last else MAX_ATTEMPTS,
            status="exhausted",
            status_code=last.status_code if last else None,
            error=last.error if last else "no attempts",
        )
        self._record(last)
        logger.warning("webhook delivery exhausted sub=%s event=%s", sub.id, event_type)
        return last

    def _record(self, att: DeliveryAttempt) -> None:
        self.r.rpush(f"webhook:log:{att.subscription_id}",
                     json.dumps(asdict(att)))
        self.r.ltrim(f"webhook:log:{att.subscription_id}", -500, -1)

    def recent_deliveries(self, sub_id: str, limit: int = 50) -> list[dict]:
        raw = self.r.lrange(f"webhook:log:{sub_id}", -limit, -1)
        return [json.loads(x.decode() if isinstance(x, bytes) else x) for x in raw]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_send(url: str, body: bytes,
                      headers: dict[str, str]) -> tuple[int | None, str | None]:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        req = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 (tenant url)
                return resp.status, None
        except HTTPError as e:
            return e.code, str(e)
        except (URLError, OSError) as e:
            return None, str(e)
