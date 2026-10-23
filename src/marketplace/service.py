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

from redis.exceptions import WatchError

from src.api.schemas import (BountyOut, DatasetListingOut, DeliveryManifest,
                             ReviewOut, normalize_public_bounty_target_urls,
                             utcnow)

logger = logging.getLogger("wcp.marketplace")

DELIVERY_TTL_DAYS = 7
_SIGNING_SECRET_ENV = "MARKETPLACE_SIGNING_SECRET"


def _signing_key() -> bytes:
    secret = os.getenv(_SIGNING_SECRET_ENV, "")
    key = secret.encode()
    if not secret:
        raise MarketplaceError("marketplace delivery signing is not configured")
    if len(key) < 32:
        raise MarketplaceError("marketplace delivery signing secret must be at least 32 bytes")
    return key


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
    target_urls: list[str] = field(default_factory=list)


class MarketplaceError(Exception):
    pass


class MarketplaceService:
    FEE_BPS = 1000  # 10% platform fee, basis points

    def __init__(self, redis_client, *, clock=None) -> None:
        self.r = redis_client
        self._clock = clock or time.time

    @staticmethod
    def _normalize_tags(tags: Any) -> list[str]:
        if (not isinstance(tags, list) or len(tags) > 15 or
                any(not isinstance(tag, str) for tag in tags)):
            raise MarketplaceError("tags must be a list of at most 15 strings")
        return list(dict.fromkeys(tag.strip().lower() for tag in tags if tag.strip()))

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
            title=title, description=description, tags=self._normalize_tags(tags),
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
        current = self.get_listing(dataset_id)
        if current is None:
            raise MarketplaceError("not found")
        if current.seller_tenant != actor:
            raise MarketplaceError("forbidden: not your listing")
        allowed = {"status", "title", "description", "tags", "price_usd_cents", "license"}
        bad = set(changes) - allowed
        if bad:
            raise MarketplaceError(f"fields not editable: {sorted(bad)}")
        if "status" in changes and changes["status"] not in {
                "draft", "published", "suspended", "archived"}:
            raise MarketplaceError("invalid listing status")
        if "price_usd_cents" in changes:
            price = changes["price_usd_cents"]
            if type(price) is not int or not 0 <= price <= 10_000_000:
                raise MarketplaceError("price_usd_cents must be between 0 and 10000000")
        if "title" in changes:
            title = changes["title"]
            if not isinstance(title, str) or not 3 <= len(title) <= 140:
                raise MarketplaceError("title must contain between 3 and 140 characters")
        if "description" in changes:
            description = changes["description"]
            if not isinstance(description, str) or not 20 <= len(description) <= 5000:
                raise MarketplaceError("description must contain between 20 and 5000 characters")
        if "license" in changes:
            license_name = changes["license"]
            if not isinstance(license_name, str) or not 1 <= len(license_name) <= 80:
                raise MarketplaceError("license must contain between 1 and 80 characters")
        if "tags" in changes:
            changes["tags"] = self._normalize_tags(changes["tags"])

        listing_key = f"mkt:listing:{dataset_id}"
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(listing_key)
                    raw = pipe.hget(listing_key, "data")
                    if not raw:
                        raise MarketplaceError("not found")
                    lst = Listing(**json.loads(raw))
                    if lst.seller_tenant != actor:
                        raise MarketplaceError("forbidden: not your listing")
                    old_tags = list(lst.tags)
                    for key, value in changes.items():
                        setattr(lst, key, value)
                    pipe.multi()
                    if "tags" in changes:
                        updated_tags = changes["tags"]
                        for tag in set(old_tags) | set(updated_tags):
                            pipe.srem(f"mkt:tag:{tag}", dataset_id)
                        for tag in updated_tags:
                            pipe.sadd(f"mkt:tag:{tag}", dataset_id)
                    pipe.hset(listing_key, "data", json.dumps(asdict(lst)))
                    pipe.execute()
                    return lst
                except WatchError:
                    continue

    def search_listings(self, *, query: str = "", tag: str = "",
                        max_price_cents: int | None = None,
                        limit: int = 20, offset: int = 0) -> list[Listing]:
        out = []
        q = query.lower()
        normalized_tag = tag.strip().lower()
        tagged = None
        if normalized_tag:
            tagged = {x.decode() if isinstance(x, bytes) else x
                      for x in self.r.smembers(f"mkt:tag:{normalized_tag}")}

        start = 0
        batch_size = 100
        target = offset + limit
        matched = 0
        while matched < target:
            batch = self.r.zrevrange("mkt:listings:new", start,
                                     start + batch_size - 1)
            if not batch:
                break
            start += len(batch)
            for raw_id in batch:
                dataset_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                if tagged is not None and dataset_id not in tagged:
                    continue
                listing = self.get_listing(dataset_id)
                if not listing or listing.status != "published":
                    continue
                if normalized_tag and normalized_tag not in listing.tags:
                    continue
                if max_price_cents is not None and listing.price_usd_cents > max_price_cents:
                    continue
                if q and q not in (listing.title + " " + listing.description + " " +
                                   " ".join(listing.tags)).lower():
                    continue
                if matched >= offset:
                    out.append(listing)
                matched += 1
                if matched >= target:
                    break
        return out

    def list_seller_listings(self, seller: str, *, status: str | None = None,
                             limit: int = 20, offset: int = 0) -> list[Listing]:
        """List only the authenticated seller's inventory, newest first."""
        allowed_statuses = {"draft", "published", "suspended", "archived"}
        if status is not None and status not in allowed_statuses:
            raise MarketplaceError("invalid listing status")
        out = []
        matched = 0
        start = 0
        batch_size = 100
        target = offset + limit
        while matched < target:
            ids = self.r.zrevrange("mkt:listings:new", start,
                                   start + batch_size - 1)
            if not ids:
                break
            start += len(ids)
            for raw_id in ids:
                dataset_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                listing = self.get_listing(dataset_id)
                if not listing or listing.seller_tenant != seller:
                    continue
                if status is not None and listing.status != status:
                    continue
                if matched >= offset:
                    out.append(listing)
                matched += 1
                if matched >= target:
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
        listing_key = f"mkt:listing:{dataset_id}"
        buyers_key = f"mkt:buyers:{dataset_id}"
        reviewer_key = f"mkt:reviewer:{dataset_id}:{buyer}"
        reviews_key = f"mkt:reviews:{dataset_id}"
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(listing_key, buyers_key, reviewer_key)
                    raw_listing = pipe.hget(listing_key, "data")
                    if not raw_listing:
                        raise MarketplaceError("dataset not found")
                    listing = Listing(**json.loads(raw_listing))
                    if listing.status != "published":
                        raise MarketplaceError("dataset not found")
                    if listing.seller_tenant == buyer:
                        raise MarketplaceError(
                            "sellers cannot review their own dataset")
                    if not pipe.sismember(buyers_key, buyer):
                        raise MarketplaceError("purchase required to review dataset")

                    existing_marker = pipe.get(reviewer_key)
                    if existing_marker:
                        existing_id = (existing_marker.decode()
                                       if isinstance(existing_marker, bytes)
                                       else existing_marker)
                        if pipe.hget(f"mkt:review:{existing_id}", "data"):
                            raise MarketplaceError(
                                "buyer has already reviewed this dataset")
                        # Recover a lock left by older non-atomic writes which
                        # stopped after claiming the marker but before saving.
                        pipe.multi()
                        pipe.delete(reviewer_key)
                        pipe.execute()
                        continue

                    # Pre-existing reviews predate reviewer locks. Backfill the
                    # marker transactionally to preserve one-review semantics.
                    existing_id = None
                    for raw_review_id in self.r.lrange(reviews_key, 0, -1):
                        review_id = (raw_review_id.decode()
                                     if isinstance(raw_review_id, bytes)
                                     else raw_review_id)
                        existing_raw = self.r.hget(
                            f"mkt:review:{review_id}", "data")
                        if existing_raw and json.loads(existing_raw).get(
                                "buyer_tenant") == buyer:
                            existing_id = review_id
                            break
                    if existing_id:
                        pipe.multi()
                        pipe.set(reviewer_key, existing_id)
                        pipe.execute()
                        raise MarketplaceError(
                            "buyer has already reviewed this dataset")

                    review_id = uuid.uuid4().hex[:12]
                    review = {
                        "review_id": review_id, "dataset_id": dataset_id,
                        "buyer_tenant": buyer, "rating": int(rating),
                        "comment": comment, "verified_purchase": True,
                        "created_at": utcnow().isoformat(),
                    }
                    pipe.multi()
                    pipe.set(reviewer_key, review_id)
                    pipe.hset(f"mkt:review:{review_id}",
                              mapping={"data": json.dumps(review)})
                    pipe.rpush(reviews_key, review_id)
                    pipe.hincrby(f"mkt:rating:{dataset_id}", "sum", int(rating))
                    pipe.hincrby(f"mkt:rating:{dataset_id}", "count", 1)
                    pipe.execute()
                    return ReviewOut(**review)
                except WatchError:
                    continue

    def list_reviews(self, dataset_id: str, limit: int = 50,
                     offset: int = 0) -> list[ReviewOut]:
        rids = self.r.lrange(f"mkt:reviews:{dataset_id}", offset,
                             offset + limit - 1)
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
        listing_key = f"mkt:listing:{dataset_id}"
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(listing_key)
                    raw_listing = pipe.hget(listing_key, "data")
                    if not raw_listing:
                        raise MarketplaceError("dataset not found")
                    listing = Listing(**json.loads(raw_listing))
                    if listing.status != "published":
                        raise MarketplaceError("dataset not available")
                    if listing.seller_tenant == buyer:
                        raise MarketplaceError("cannot purchase your own dataset")
                    if listing.price_usd_cents > 0:
                        raise MarketplaceError(
                            "paid marketplace settlement is not configured")
                    fee = (listing.price_usd_cents * self.FEE_BPS) // 10_000
                    purchase = Purchase(
                        purchase_id=f"pur_{uuid.uuid4().hex[:12]}",
                        dataset_id=dataset_id, buyer_tenant=buyer,
                        seller_tenant=listing.seller_tenant,
                        price_usd_cents=listing.price_usd_cents,
                        platform_fee_cents=fee)
                    manifest = self.issue_delivery(buyer, listing, persist=False)
                    pipe.multi()
                    pipe.hset(f"mkt:purchase:{purchase.purchase_id}",
                              mapping={"data": json.dumps(asdict(purchase))})
                    pipe.sadd(f"mkt:purchase-index:{buyer}", purchase.purchase_id)
                    pipe.sadd(f"mkt:buyers:{dataset_id}", buyer)
                    pipe.setex(f"mkt:delivery:{manifest.delivery_id}",
                               DELIVERY_TTL_DAYS * 86400,
                               json.dumps(manifest.model_dump(mode="json")))
                    pipe.execute()
                    logger.info("purchase %s: %s bought %s (%d cents, fee %d)",
                                purchase.purchase_id, buyer, dataset_id,
                                listing.price_usd_cents, fee)
                    return purchase, manifest
                except WatchError:
                    continue

    def list_purchases(self, buyer: str, limit: int = 50,
                       offset: int = 0) -> list[Purchase]:
        """Return a page of recent purchases indexed to this buyer only."""
        purchase_ids = self.r.smembers(f"mkt:purchase-index:{buyer}")
        out = []
        for raw_id in purchase_ids:
            purchase_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            raw = self.r.hget(f"mkt:purchase:{purchase_id}", "data")
            if raw:
                purchase = Purchase(**json.loads(raw))
                if purchase.buyer_tenant == buyer:
                    out.append(purchase)
        out.sort(key=lambda purchase: purchase.created_at, reverse=True)
        return out[offset:offset + limit]

    # --------------------- signed secure delivery ----------------------- #
    def issue_delivery(self, buyer: str, lst: Listing, *,
                       persist: bool = True) -> DeliveryManifest:
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
        if persist:
            self.r.setex(f"mkt:delivery:{m.delivery_id}",
                         DELIVERY_TTL_DAYS * 86400,
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
        try:
            target_urls = normalize_public_bounty_target_urls(target_urls)
        except ValueError as exc:
            raise MarketplaceError(str(exc)) from exc
        b = Bounty(bounty_id=f"bty_{uuid.uuid4().hex[:12]}", poster_tenant=poster,
                   title=title, spec=spec, reward_usd_cents=reward_usd_cents,
                   deadline=(utcnow() + timedelta(days=deadline_days)).isoformat(),
                   target_urls=target_urls)
        pipe = self.r.pipeline()
        pipe.hset(f"mkt:bounty:{b.bounty_id}", mapping={"data": json.dumps(asdict(b))})
        pipe.zadd("mkt:bounties:open", {b.bounty_id: self._clock()})
        if target_urls:
            pipe.hset(f"mkt:bounty:{b.bounty_id}", "targets", json.dumps(target_urls))
        pipe.execute()
        return b

    def get_bounty(self, bounty_id: str) -> Optional[Bounty]:
        key = f"mkt:bounty:{bounty_id}"
        raw = self.r.hget(key, "data")
        if not raw:
            return None
        bounty = Bounty(**json.loads(raw))
        if not bounty.target_urls:
            raw_targets = self.r.hget(key, "targets")
            if raw_targets:
                bounty.target_urls = json.loads(raw_targets)
        bounty.target_urls = normalize_public_bounty_target_urls(
            bounty.target_urls, drop_invalid=True)
        return bounty

    def submit_for_bounty(self, worker: str, bounty_id: str,
                          dataset_id: str) -> dict:
        bounty_key = f"mkt:bounty:{bounty_id}"
        submissions_key = f"{bounty_key}:submissions"
        listing_key = f"mkt:listing:{dataset_id}"
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(bounty_key, submissions_key, listing_key)
                    raw = pipe.hget(bounty_key, "data")
                    if not raw:
                        raise MarketplaceError("bounty not found")
                    b = Bounty(**json.loads(raw))
                    if b.status != "open":
                        raise MarketplaceError(f"bounty is {b.status}")
                    if datetime.fromisoformat(b.deadline) < utcnow():
                        b.status = "expired"
                        pipe.multi()
                        pipe.hset(bounty_key, "data", json.dumps(asdict(b)))
                        pipe.zrem("mkt:bounties:open", bounty_id)
                        pipe.execute()
                        raise MarketplaceError("bounty expired")
                    listing = self.get_listing(dataset_id)
                    if listing is None or listing.status != "published":
                        raise MarketplaceError(
                            "submission must reference a published listing")
                    if listing.seller_tenant != worker:
                        raise MarketplaceError(
                            "only the listing owner may submit it")
                    if pipe.sismember(submissions_key, dataset_id):
                        raise MarketplaceError(
                            "dataset already submitted for this bounty")
                    b.submissions = pipe.scard(submissions_key) + 1
                    pipe.multi()
                    pipe.sadd(submissions_key, dataset_id)
                    pipe.hset(bounty_key, "data", json.dumps(asdict(b)))
                    pipe.execute()
                    return {"bounty_id": bounty_id, "dataset_id": dataset_id,
                            "queued_for_review": True}
                except WatchError:
                    continue

    def award_bounty(self, poster: str, bounty_id: str, dataset_id: str) -> dict:
        bounty_key = f"mkt:bounty:{bounty_id}"
        submissions_key = f"{bounty_key}:submissions"
        listing_key = f"mkt:listing:{dataset_id}"
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(bounty_key, submissions_key, listing_key)
                    raw = pipe.hget(bounty_key, "data")
                    if not raw:
                        raise MarketplaceError("bounty not found")
                    b = Bounty(**json.loads(raw))
                    if b.poster_tenant != poster:
                        raise MarketplaceError("only the poster may award a bounty")
                    if b.status != "open":
                        raise MarketplaceError(f"bounty is {b.status}")
                    if datetime.fromisoformat(b.deadline) < utcnow():
                        b.status = "expired"
                        pipe.multi()
                        pipe.hset(bounty_key, "data", json.dumps(asdict(b)))
                        pipe.zrem("mkt:bounties:open", bounty_id)
                        pipe.execute()
                        raise MarketplaceError("bounty expired")
                    if not pipe.sismember(submissions_key, dataset_id):
                        raise MarketplaceError(
                            "dataset was not submitted for this bounty")
                    listing = self.get_listing(dataset_id)
                    if listing is None or listing.status != "published":
                        raise MarketplaceError(
                            "submitted dataset is no longer published")
                    b.status = "awarded"
                    b.assigned_to = dataset_id
                    pipe.multi()
                    pipe.hset(bounty_key, "data", json.dumps(asdict(b)))
                    pipe.zrem("mkt:bounties:open", bounty_id)
                    pipe.execute()
                    return {"bounty_id": bounty_id,
                            "awarded_to_dataset": dataset_id,
                            "reward_usd_cents": b.reward_usd_cents}
                except WatchError:
                    continue

    def _set_bounty_status(self, b: Bounty, status: str, **extra: Any) -> None:
        b.status = status
        for k, v in extra.items():
            setattr(b, k, v)
        self.r.hset(f"mkt:bounty:{b.bounty_id}", "data", json.dumps(asdict(b)))

    def list_open_bounties(self, limit: int = 50,
                           offset: int = 0) -> list[BountyOut]:
        out = []
        expired_ids = []
        start = 0
        target = offset + limit
        batch_size = max(limit, 50)
        matched = 0
        now = utcnow()
        while matched < target:
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
                if matched >= offset:
                    out.append(BountyOut(
                        bounty_id=b.bounty_id, poster_tenant=b.poster_tenant,
                        title=b.title, spec=b.spec,
                        reward_usd_cents=b.reward_usd_cents,
                        deadline=datetime.fromisoformat(b.deadline),
                        status=b.status, submissions=b.submissions,
                        target_urls=b.target_urls))
                matched += 1
                if matched >= target:
                    break
        if expired_ids:
            self.r.zrem("mkt:bounties:open", *expired_ids)
        return out

    def list_bounty_submissions(self, poster: str, bounty_id: str, *,
                                limit: int = 50, offset: int = 0) -> list[str]:
        """List submitted dataset IDs for the bounty poster only."""
        bounty = self.get_bounty(bounty_id)
        if bounty is None:
            raise MarketplaceError("bounty not found")
        if bounty.poster_tenant != poster:
            raise MarketplaceError("only the poster may inspect submissions")
        ids = {item.decode() if isinstance(item, bytes) else item
               for item in self.r.smembers(f"mkt:bounty:{bounty_id}:submissions")}
        return sorted(ids)[offset:offset + limit]

    @staticmethod
    def hash_records(records: list[dict]) -> str:
        h = hashlib.sha256()
        for rec in records:
            h.update(json.dumps(rec, sort_keys=True).encode())
        return h.hexdigest()
