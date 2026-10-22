"""Data Marketplace service: listings, previews, reviews, bounties, purchases.

Storage backend is Redis (swappable): listings/reviews/bounties/purchases are
kept in hashes + sorted-set indexes so the marketplace can be queried without
a relational DB in single-node deployments.  Purchases produce **signed
delivery manifests** (HMAC-SHA256 over a canonical JSON form) with an
embedded SHA-256 content digest so buyers can verify dataset integrity and
provenance offline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from src.api.schemas import (BountyOut, DatasetListingOut, DeliveryManifest,
                             ReviewOut, utcnow)

logger = logging.getLogger("wcp.marketplace")

DELIVERY_TTL_DAYS = 7
_SIGNING_SECRET_ENV = "MARKETPLACE_SIGNING_SECRET"


def _signing_key() -> bytes:
    secret = os.getenv(_SIGNING_SECRET_ENV, "")
    if not secret:
        # dev fallback — deterministic enough for tests, logged loudly
        logger.warning("%s not set; using ephemeral dev signing key",
                       _SIGNING_SECRET_ENV)
        secret = "dev-insecure-marketplace-key"
    return secret.encode()


@dataclass
class Listing:
    dataset_id: str
    seller_tenant: str
    title: str
    description: str
    tags: list[str]
    price_usd_cents: int
    license: str
    status: str = "published"          # draft|published|suspended|archived
    token_count: int = 0
    record_count: int = 0
    size_bytes: int = 0
    content_sha256: str = ""
    storage_uri: str = ""
    preview_uri: str = ""
    created_at: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass
class Purchase:
    purchase_id: str
    dataset_id: str
    buyer_tenant: str
    seller_tenant: str
    price_usd_cents: int
    platform_fee_cents: int
    created_at: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass
class Bounty:
    bounty_id: str
    poster_tenant: str
    title: str
    spec: str
    reward_usd_cents: int
    deadline: str
    status: str = "open"               # open|claimed|submitted|awarded|expired
    assigned_to: str | None = None
    submissions: int = 0


class MarketplaceError(Exception):
    pass


class MarketplaceService:
    FEE_BPS = 1000  # 10% platform fee, basis points

    def __init__(self, redis_client, *, clock=None) -> None:
        self.r = redis_client
        self._clock = clock or time.time

    # ------------------------------ listings ---------------------------- #
    def create_listing(self, seller: str, *, title: str, description: str,
                       tags: list[str], price_usd_cents: int, license: str,
                       token_count: int = 0, record_count: int = 0,
                       size_bytes: int = 0, content_sha256: str = "",
                       storage_uri: str = "") -> Listing:
        if not content_sha256:
            raise MarketplaceError("dataset must be hashed (content_sha256) before listing")
        lst = Listing(
            dataset_id=f"ds_{uuid.uuid4().hex[:16]}", seller_tenant=seller,
            title=title, description=description, tags=[t.lower() for t in tags],
            price_usd_cents=price_usd_cents, license=license,
            token_count=token_count, record_count=record_count,
            size_bytes=size_bytes, content_sha256=content_sha256,
            storage_uri=storage_uri,
        )
        lst.preview_uri = f"preview://{lst.dataset_id}"
        pipe = self.r.pipeline()
        pipe.hset(f"mkt:listing:{lst.dataset_id}", mapping={"data": json.dumps(asdict(lst))})
        pipe.zadd("mkt:listings:new", {lst.dataset_id: self._clock()})
        for t in lst.tags:
            pipe.sadd(f"mkt:tag:{t}", lst.dataset_id)
        pipe.execute()
        return lst

    def get_listing(self, dataset_id: str) -> Optional[Listing]:
        raw = self.r.hget(f"mkt:listing:{dataset_id}", "data")
        if not raw:
            return None
        return Listing(**json.loads(raw))

    def update_listing(self, dataset_id: str, actor: str, **changes: Any) -> Listing:
        lst = self.get_listing(dataset_id)
        if lst is None:
            raise MarketplaceError("not found")
        if lst.seller_tenant != actor:
            raise MarketplaceError("forbidden: not your listing")
        allowed = {"status", "title", "description", "tags", "price_usd_cents", "license"}
        bad = set(changes) - allowed
        if bad:
            raise MarketplaceError(f"fields not editable: {sorted(bad)}")
        for k, v in changes.items():
            setattr(lst, k, v)
        self.r.hset(f"mkt:listing:{dataset_id}", "data", json.dumps(asdict(lst)))
        return lst

    def search_listings(self, *, query: str = "", tag: str = "",
                        max_price_cents: int | None = None,
                        limit: int = 20) -> list[Listing]:
        ids = [i.decode() if isinstance(i, bytes) else i
               for i in self.r.zrevrange("mkt:listings:new", 0, 500)]
        if tag:
            tagged = {x.decode() if isinstance(x, bytes) else x
                      for x in self.r.smembers(f"mkt:tag:{tag.lower()}")}
            ids = [i for i in ids if i in tagged]
        out = []
        q = query.lower()
        for i in ids:
            lst = self.get_listing(i)
            if not lst or lst.status != "published":
                continue
            if max_price_cents is not None and lst.price_usd_cents > max_price_cents:
                continue
            if q and q not in (lst.title + " " + lst.description + " " +
                               " ".join(lst.tags)).lower():
                continue
            out.append(lst)
            if len(out) >= limit:
                break
        return out

    def build_preview(self, dataset_id: str, records: Iterable[dict],
                      rows: int = 50) -> list[dict]:
        """Truncated, PII-safe sample of the first ``rows`` records."""
        lst = self.get_listing(dataset_id)
        if lst is None:
            raise MarketplaceError("not found")
        preview = []
        for i, rec in enumerate(records):
            if i >= rows:
                break
            safe = {k: v for k, v in rec.items()
                    if k not in {"email", "phone", "ssn", "ip", "raw_html"}}
            text = str(safe.get("content", safe.get("text", "")))
            safe["content"] = text[:280] + ("…" if len(text) > 280 else "")
            preview.append(safe)
        return preview

    # ------------------------------ ratings ----------------------------- #
    def add_review(self, buyer: str, dataset_id: str, rating: int,
                   comment: str) -> ReviewOut:
        lst = self.get_listing(dataset_id)
        if lst is None:
            raise MarketplaceError("dataset not found")
        if lst.seller_tenant == buyer:
            raise MarketplaceError("sellers cannot review their own dataset")
        buyers = {x.decode() if isinstance(x, bytes) else x
                  for x in self.r.smembers(f"mkt:buyers:{dataset_id}")}
        if buyer not in buyers:
            raise MarketplaceError("purchase required to review dataset")

        rid = uuid.uuid4().hex[:12]
        reviewer_key = f"mkt:reviewer:{dataset_id}:{buyer}"
        if not self.r.set(reviewer_key, rid, nx=True):
            raise MarketplaceError("buyer has already reviewed this dataset")
        rev = {"review_id": rid, "dataset_id": dataset_id, "buyer_tenant": buyer,
               "rating": int(rating), "comment": comment,
               "verified_purchase": True,
               "created_at": utcnow().isoformat()}
        try:
            pipe = self.r.pipeline()
            pipe.hset(f"mkt:review:{rid}", mapping={"data": json.dumps(rev)})
            pipe.rpush(f"mkt:reviews:{dataset_id}", rid)
            pipe.hincrby(f"mkt:rating:{dataset_id}", "sum", int(rating))
            pipe.hincrby(f"mkt:rating:{dataset_id}", "count", 1)
            pipe.execute()
        except Exception:
            self.r.delete(reviewer_key)
            raise
        return ReviewOut(**rev)

    def list_reviews(self, dataset_id: str, limit: int = 50) -> list[ReviewOut]:
        rids = self.r.lrange(f"mkt:reviews:{dataset_id}", 0, limit - 1)
        out = []
        for rid in rids:
            rid_s = rid.decode() if isinstance(rid, bytes) else rid
            raw = self.r.hget(f"mkt:review:{rid_s}", "data")
            if raw:
                out.append(ReviewOut(**json.loads(raw)))
        return out

    def rating_summary(self, dataset_id: str) -> tuple[float, int]:
        raw = self.r.hgetall(f"mkt:rating:{dataset_id}")
        if not raw:
            return 0.0, 0
        d = {(k.decode() if isinstance(k, bytes) else k):
             (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        count = int(d.get("count", 0))
        if not count:
            return 0.0, 0
        return round(int(d.get("sum", 0)) / count, 2), count

    def to_out(self, lst: Listing) -> DatasetListingOut:
        avg, cnt = self.rating_summary(lst.dataset_id)
        return DatasetListingOut(
            dataset_id=lst.dataset_id, seller_tenant=lst.seller_tenant,
            title=lst.title, description=lst.description, tags=lst.tags,
            price_usd_cents=lst.price_usd_cents, license=lst.license,
            status=lst.status, token_count=lst.token_count,
            preview_uri=lst.preview_uri, rating_avg=avg, rating_count=cnt,
            created_at=datetime.fromisoformat(lst.created_at))

    # ------------------------------ purchases --------------------------- #
    def purchase(self, buyer: str, dataset_id: str) -> tuple[Purchase, DeliveryManifest]:
        lst = self.get_listing(dataset_id)
        if lst is None:
            raise MarketplaceError("dataset not found")
        if lst.status != "published":
            raise MarketplaceError("dataset not available")
        if lst.seller_tenant == buyer:
            raise MarketplaceError("cannot purchase your own dataset")
        fee = (lst.price_usd_cents * self.FEE_BPS) // 10_000
        pur = Purchase(purchase_id=f"pur_{uuid.uuid4().hex[:12]}",
                       dataset_id=dataset_id, buyer_tenant=buyer,
                       seller_tenant=lst.seller_tenant,
                       price_usd_cents=lst.price_usd_cents, platform_fee_cents=fee)
        manifest = self.issue_delivery(buyer, lst)
        pipe = self.r.pipeline()
        pipe.hset(f"mkt:purchase:{pur.purchase_id}", mapping={"data": json.dumps(asdict(pur))})
        pipe.sadd(f"mkt:purchase-index:{buyer}", pur.purchase_id)
        pipe.sadd(f"mkt:buyers:{dataset_id}", buyer)
        pipe.execute()
        logger.info("purchase %s: %s bought %s (%d cents, fee %d)",
                    pur.purchase_id, buyer, dataset_id, lst.price_usd_cents, fee)
        return pur, manifest

    # --------------------- signed secure delivery ----------------------- #
    def issue_delivery(self, buyer: str, lst: Listing) -> DeliveryManifest:
        now = utcnow()
        m = DeliveryManifest(
            delivery_id=f"dlv_{uuid.uuid4().hex[:12]}",
            dataset_id=lst.dataset_id, buyer_tenant=buyer,
            uri=f"s3://wcp-marketplace/{lst.dataset_id}/{buyer}/{lst.dataset_id}.parquet",
            sha256=lst.content_sha256, size_bytes=lst.size_bytes,
            record_count=lst.record_count, issued_at=now,
            expires_at=now + timedelta(days=DELIVERY_TTL_DAYS),
            signature="")
        m.signature = self._sign(m)
        self.r.setex(f"mkt:delivery:{m.delivery_id}", DELIVERY_TTL_DAYS * 86400,
                     json.dumps(m.model_dump(mode="json")))
        return m

    @staticmethod
    def canonical(manifest: DeliveryManifest) -> bytes:
        core = {k: manifest.model_dump(mode="json")[k] for k in
                ("delivery_id", "dataset_id", "buyer_tenant", "uri", "sha256",
                 "size_bytes", "record_count", "issued_at", "expires_at")}
        return json.dumps(core, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _sign(manifest: DeliveryManifest) -> str:
        return hmac.new(_signing_key(), MarketplaceService.canonical(manifest),
                        hashlib.sha256).hexdigest()

    @staticmethod
    def verify_manifest(manifest: DeliveryManifest, *, body: bytes | None = None) -> bool:
        ok = hmac.compare_digest(manifest.signature,
                                 MarketplaceService._sign(manifest))
        if body is not None and ok:
            ok = hashlib.sha256(body).hexdigest() == manifest.sha256
        return ok

    def get_delivery(self, delivery_id: str, tenant: str) -> Optional[DeliveryManifest]:
        raw = self.r.get(f"mkt:delivery:{delivery_id}")
        if not raw:
            return None
        m = DeliveryManifest(**json.loads(raw))
        if m.buyer_tenant != tenant:
            return None
        if m.expires_at < utcnow():
            return None
        return m

    # ------------------------------ bounties ---------------------------- #
    def create_bounty(self, poster: str, *, title: str, spec: str,
                      reward_usd_cents: int, deadline_days: int,
                      target_urls: list[str]) -> Bounty:
        b = Bounty(bounty_id=f"bty_{uuid.uuid4().hex[:12]}", poster_tenant=poster,
                   title=title, spec=spec, reward_usd_cents=reward_usd_cents,
                   deadline=(utcnow() + timedelta(days=deadline_days)).isoformat())
        pipe = self.r.pipeline()
        pipe.hset(f"mkt:bounty:{b.bounty_id}", mapping={"data": json.dumps(asdict(b))})
        pipe.zadd("mkt:bounties:open", {b.bounty_id: self._clock()})
        if target_urls:
            pipe.hset(f"mkt:bounty:{b.bounty_id}", "targets", json.dumps(target_urls))
        pipe.execute()
        return b

    def get_bounty(self, bounty_id: str) -> Optional[Bounty]:
        raw = self.r.hget(f"mkt:bounty:{bounty_id}", "data")
        return Bounty(**json.loads(raw)) if raw else None

    def submit_for_bounty(self, worker: str, bounty_id: str,
                          dataset_id: str) -> dict:
        b = self.get_bounty(bounty_id)
        if b is None:
            raise MarketplaceError("bounty not found")
        if b.status != "open":
            raise MarketplaceError(f"bounty is {b.status}")
        if datetime.fromisoformat(b.deadline) < utcnow():
            self._set_bounty_status(b, "expired")
            raise MarketplaceError("bounty expired")
        listing = self.get_listing(dataset_id)
        if listing is None or listing.status != "published":
            raise MarketplaceError("submission must reference a published listing")
        if listing.seller_tenant != worker:
            raise MarketplaceError("only the listing owner may submit it")
        submissions_key = f"mkt:bounty:{bounty_id}:submissions"
        if not self.r.sadd(submissions_key, dataset_id):
            raise MarketplaceError("dataset already submitted for this bounty")
        b.submissions = self.r.scard(submissions_key)
        self._set_bounty_status(b, b.status, submissions=b.submissions)
        return {"bounty_id": bounty_id, "dataset_id": dataset_id, "queued_for_review": True}

    def award_bounty(self, poster: str, bounty_id: str, dataset_id: str) -> dict:
        b = self.get_bounty(bounty_id)
        if b is None:
            raise MarketplaceError("bounty not found")
        if b.poster_tenant != poster:
            raise MarketplaceError("only the poster may award a bounty")
        if b.status != "open":
            raise MarketplaceError(f"bounty is {b.status}")
        if datetime.fromisoformat(b.deadline) < utcnow():
            self._set_bounty_status(b, "expired")
            self.r.zrem("mkt:bounties:open", bounty_id)
            raise MarketplaceError("bounty expired")
        subs = {x.decode() if isinstance(x, bytes) else x
                for x in self.r.smembers(f"mkt:bounty:{bounty_id}:submissions")}
        if dataset_id not in subs:
            raise MarketplaceError("dataset was not submitted for this bounty")
        self._set_bounty_status(b, "awarded", assigned_to=dataset_id)
        self.r.zrem("mkt:bounties:open", bounty_id)
        return {"bounty_id": bounty_id, "awarded_to_dataset": dataset_id,
                "reward_usd_cents": b.reward_usd_cents}

    def _set_bounty_status(self, b: Bounty, status: str, **extra: Any) -> None:
        b.status = status
        for k, v in extra.items():
            setattr(b, k, v)
        self.r.hset(f"mkt:bounty:{b.bounty_id}", "data", json.dumps(asdict(b)))

    def list_open_bounties(self, limit: int = 50) -> list[BountyOut]:
        out = []
        expired_ids = []
        start = 0
        batch_size = max(limit, 50)
        now = utcnow()
        while len(out) < limit:
            ids = self.r.zrevrange("mkt:bounties:open", start,
                                   start + batch_size - 1)
            if not ids:
                break
            start += len(ids)
            for raw_id in ids:
                bounty_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                b = self.get_bounty(bounty_id)
                if not b or b.status != "open":
                    expired_ids.append(bounty_id)
                    continue
                if datetime.fromisoformat(b.deadline) < now:
                    self._set_bounty_status(b, "expired")
                    expired_ids.append(bounty_id)
                    continue
                out.append(BountyOut(
                    bounty_id=b.bounty_id, poster_tenant=b.poster_tenant,
                    title=b.title, spec=b.spec, reward_usd_cents=b.reward_usd_cents,
                    deadline=datetime.fromisoformat(b.deadline), status=b.status,
                    submissions=b.submissions))
                if len(out) >= limit:
                    break
        if expired_ids:
            self.r.zrem("mkt:bounties:open", *expired_ids)
        return out

    @staticmethod
    def hash_records(records: list[dict]) -> str:
        h = hashlib.sha256()
        for rec in records:
            h.update(json.dumps(rec, sort_keys=True).encode())
        return h.hexdigest()
