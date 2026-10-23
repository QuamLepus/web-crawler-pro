"""v1 router: Data Marketplace (listings, previews, reviews, bounties, purchases)."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re

from fastapi import APIRouter, HTTPException, Query, Request

from src.api.routers.jobs import JOB_KEY, CtxDep
from src.api.schemas import (BountyCreate, BountyOut, DatasetListingCreate,
                             DatasetListingOut, DatasetListingUpdate, DeliveryManifest,
                             MarketplacePurchaseOut, MarketplaceReviewPublicOut,
                             ReviewCreate, ReviewOut)
from src.marketplace.service import MarketplaceError, MarketplaceService

logger = logging.getLogger("wcp.api.marketplace")
router = APIRouter(prefix="/marketplace", tags=["marketplace"])


def _mkt(request: Request) -> MarketplaceService:
    svc = getattr(request.app.state.services, "marketplace", None)
    if svc is None:
        svc = MarketplaceService(request.app.state.services.redis)
        request.app.state.services.marketplace = svc
    return svc


# ------------------------------ listings ---------------------------------- #
@router.post("/datasets", response_model=DatasetListingOut, status_code=201,
             summary="Publish a dataset listing")
async def create_listing(body: DatasetListingCreate, ctx: CtxDep,
                         request: Request) -> DatasetListingOut:
    mkt = _mkt(request)
    if not body.source_job_id:
        raise HTTPException(422, "source_job_id is required to publish a dataset")
    raw_source = mkt.r.hgetall(JOB_KEY.format(job_id=body.source_job_id))
    source = {
        (key.decode() if isinstance(key, bytes) else str(key)):
        (value.decode() if isinstance(value, bytes) else str(value))
        for key, value in raw_source.items()
    }
    if not source or source.get("tenant") != str(ctx["tenant"]):
        # Deliberately make a foreign-tenant job indistinguishable from an
        # absent job so callers cannot probe another tenant's crawl IDs.
        raise HTTPException(404, "source crawl not found")
    if source.get("state") != "completed":
        raise HTTPException(422, "source crawl must be completed before listing")
    sha = source.get("data_sha256", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
        raise HTTPException(422, "source crawl has no valid content digest")
    try:
        record_count = int(source.get("items_collected", "0"))
        token_count = int(source.get("tokens_generated", "0"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "source crawl counters are invalid") from exc
    if record_count < 0 or token_count < 0:
        raise HTTPException(422, "source crawl counters are invalid")

    artifact_fields = {}
    if source.get("artifact_status") == "available":
        expiry_raw = source.get("artifact_expires_at", "")
        try:
            artifact_expires = datetime.fromisoformat(expiry_raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "source crawl artifact metadata is invalid") from exc
        if artifact_expires.tzinfo is None:
            raise HTTPException(422, "source crawl artifact expiry has no timezone")
        if artifact_expires.astimezone(timezone.utc) > datetime.now(timezone.utc):
            try:
                artifact_size_bytes = int(source.get("artifact_size_bytes", "0"))
                artifact_record_count = int(
                    source.get("artifact_record_count", "0"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, "source crawl artifact metadata is invalid") from exc
            artifact_bucket = source.get("artifact_bucket", "")
            artifact_object_key = source.get("artifact_object_key", "")
            if (not artifact_bucket or not artifact_object_key or
                    source.get("artifact_uri") !=
                    f"s3://{artifact_bucket}/{artifact_object_key}"):
                raise HTTPException(422, "source crawl artifact metadata is invalid")
            artifact_fields = {
                "source_job_id": body.source_job_id,
                "artifact_bucket": artifact_bucket,
                "artifact_object_key": artifact_object_key,
                "artifact_sha256": source.get("artifact_sha256", ""),
                "artifact_size_bytes": artifact_size_bytes,
                "artifact_record_count": artifact_record_count,
                "artifact_format": source.get("artifact_format", ""),
                "artifact_expires_at": expiry_raw,
            }
    try:
        lst = mkt.create_listing(
            ctx["tenant"], title=body.title, description=body.description,
            tags=body.tags, price_usd_cents=body.price_usd_cents,
            license=body.license, content_sha256=sha,
            record_count=record_count,
            token_count=token_count, **artifact_fields)
    except MarketplaceError as exc:
        raise HTTPException(422, str(exc))
    mkt.copy_job_preview(body.source_job_id, lst.dataset_id, ctx["tenant"])
    return mkt.to_out(lst)


@router.get("/my/datasets", response_model=list[DatasetListingOut],
            summary="List the authenticated seller's marketplace inventory")
async def my_datasets(ctx: CtxDep, request: Request,
                      status: str | None = Query(
                          None, pattern="^(draft|published|suspended|archived)$"),
                      limit: int = Query(20, ge=1, le=100),
                      offset: int = Query(0, ge=0, le=10_000)) -> list[DatasetListingOut]:
    marketplace = _mkt(request)
    listings = marketplace.list_seller_listings(
        ctx["tenant"], status=status, limit=limit, offset=offset)
    return [marketplace.to_out(listing) for listing in listings]


@router.get("/datasets", response_model=list[DatasetListingOut],
            summary="Search published datasets")
async def search_datasets(ctx: CtxDep, request: Request,
                          q: str = "", tag: str = "",
                          max_price_cents: int | None = Query(None, ge=0),
                          limit: int = Query(20, ge=1, le=100),
                          offset: int = Query(0, ge=0, le=10_000)):
    mkt = _mkt(request)
    found = mkt.search_listings(query=q, tag=tag,
                                max_price_cents=max_price_cents,
                                limit=limit, offset=offset)
    return [mkt.to_out(l) for l in found]


@router.get("/datasets/{dataset_id}", response_model=DatasetListingOut)
async def get_dataset(dataset_id: str, ctx: CtxDep, request: Request):
    mkt = _mkt(request)
    lst = mkt.get_listing_for_viewer(dataset_id, ctx["tenant"])
    if lst is None:
        # Draft/suspended inventory is visible only to its owning tenant.
        raise HTTPException(404, "dataset not found")
    return mkt.to_out(lst)


@router.get("/datasets/{dataset_id}/preview",
            summary="Sample rows with common identifiers redacted")
async def dataset_preview(dataset_id: str, ctx: CtxDep, request: Request,
                          rows: int = Query(50, ge=10, le=500)) -> list[dict]:
    mkt = _mkt(request)
    listing = mkt.get_listing_for_viewer(dataset_id, ctx["tenant"])
    if listing is None:
        # Match listing visibility; never disclose preview data for another
        # tenant's draft, suspended or archived dataset.
        raise HTTPException(404, "dataset not found")
    records = mkt.load_preview_records(dataset_id, rows=rows)
    if records is None:
        # Samples are retained only when a completed crawl stored an eligible
        # bounded preview. Never synthesize rows when there is no real sample.
        raise HTTPException(503, "dataset preview is unavailable")
    preview = mkt.build_preview(dataset_id, records, rows=rows)
    if mkt.get_listing_for_viewer(dataset_id, ctx["tenant"]) is None:
        raise HTTPException(404, "dataset not found")
    return preview


@router.patch("/datasets/{dataset_id}", response_model=DatasetListingOut)
async def update_dataset(dataset_id: str, changes: DatasetListingUpdate,
                         ctx: CtxDep, request: Request) -> DatasetListingOut:
    mkt = _mkt(request)
    try:
        lst = mkt.update_listing(
            dataset_id, ctx["tenant"], **changes.model_dump(exclude_unset=True))
    except MarketplaceError as exc:
        msg = str(exc)
        if msg == "not found" or "forbidden" in msg:
            # Hide the existence of another seller's private inventory.
            raise HTTPException(404, "dataset not found") from exc
        raise HTTPException(422, msg) from exc
    return mkt.to_out(lst)


# ------------------------------ reviews ----------------------------------- #
@router.post("/datasets/{dataset_id}/reviews", response_model=ReviewOut, status_code=201)
async def add_review(dataset_id: str, body: ReviewCreate, ctx: CtxDep,
                     request: Request) -> ReviewOut:
    try:
        return _mkt(request).add_review(ctx["tenant"], dataset_id,
                                        body.rating, body.comment)
    except MarketplaceError as exc:
        message = str(exc)
        code = (404 if "not found" in message else
                403 if "purchase required" in message else
                409 if "already reviewed" in message else 400)
        raise HTTPException(code, "dataset not found" if code == 404 else message)


@router.get("/datasets/{dataset_id}/reviews",
            response_model=list[MarketplaceReviewPublicOut])
async def get_reviews(dataset_id: str, ctx: CtxDep, request: Request,
                      limit: int = Query(50, ge=1, le=200),
                      offset: int = Query(0, ge=0, le=10_000)) -> list[MarketplaceReviewPublicOut]:
    marketplace = _mkt(request)
    reviews = marketplace.list_reviews_for_viewer(
        dataset_id, ctx["tenant"], limit=limit, offset=offset)
    if reviews is None:
        raise HTTPException(404, "dataset not found")
    return [
        MarketplaceReviewPublicOut(**review.model_dump(exclude={"buyer_tenant"}))
        for review in reviews
    ]


# ------------------------------ purchase / delivery ----------------------- #
@router.post("/datasets/{dataset_id}/purchase",
             summary="Record a free purchase and issue signed manifest metadata; dataset bytes are unavailable")
async def purchase(dataset_id: str, ctx: CtxDep, request: Request) -> dict:
    mkt = _mkt(request)
    try:
        pur, manifest = mkt.purchase(ctx["tenant"], dataset_id)
    except MarketplaceError as exc:
        msg = str(exc)
        code = (503 if "signing" in msg or "settlement" in msg else
                404 if "not found" in msg else
                409 if "own" in msg or "not available" in msg else 400)
        raise HTTPException(code, msg)
    # emit webhook to both parties
    try:
        svc = request.app.state.services
        svc.webhooks.emit(ctx["tenant"], "dataset.sold",
                          {"purchase_id": pur.purchase_id,
                           "dataset_id": dataset_id})
    except Exception:
        logger.debug("webhook emit failed", exc_info=True)
    return {"purchase": pur.__dict__, "delivery": manifest.model_dump(mode="json"),
            "verify": "check the HMAC signature; dataset bytes are not available for download"}


@router.get("/purchases", response_model=list[MarketplacePurchaseOut],
            summary="List purchases belonging to the authenticated tenant")
async def list_purchases(ctx: CtxDep, request: Request,
                         limit: int = Query(50, ge=1, le=200),
                         offset: int = Query(0, ge=0, le=10_000)) -> list[MarketplacePurchaseOut]:
    return _mkt(request).list_purchases(ctx["tenant"], limit=limit,
                                        offset=offset)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryManifest)
async def get_delivery(delivery_id: str, ctx: CtxDep, request: Request):
    m = _mkt(request).get_delivery(delivery_id, ctx["tenant"])
    if m is None:
        raise HTTPException(404, "delivery not found or expired")
    return m


# ------------------------------ bounties ---------------------------------- #
@router.post("/bounties", response_model=BountyOut, status_code=201)
async def create_bounty(body: BountyCreate, ctx: CtxDep,
                        request: Request) -> BountyOut:
    b = _mkt(request).create_bounty(
        ctx["tenant"], title=body.title, spec=body.spec,
        reward_usd_cents=body.reward_usd_cents, deadline_days=body.deadline_days,
        target_urls=body.target_urls)
    return BountyOut(bounty_id=b.bounty_id, poster_tenant=b.poster_tenant,
                     title=b.title, spec=b.spec,
                     reward_usd_cents=b.reward_usd_cents,
                     deadline=__import__("datetime").datetime.fromisoformat(b.deadline),
                     status=b.status, submissions=b.submissions,
                     target_urls=b.target_urls)


@router.post("/bounties/{bounty_id}/cancel")
async def cancel_bounty(bounty_id: str, ctx: CtxDep,
                        request: Request) -> dict:
    try:
        return _mkt(request).cancel_bounty(ctx["tenant"], bounty_id)
    except MarketplaceError as exc:
        message = str(exc)
        code = 404 if message == "bounty not found" else (
            409 if "bounty is" in message or "bounty expired" in message else 400)
        raise HTTPException(code, message) from exc


@router.get("/bounties/{bounty_id}", response_model=BountyOut,
            summary="Read an open bounty or one owned by the authenticated poster")
async def get_bounty(bounty_id: str, ctx: CtxDep,
                     request: Request) -> BountyOut:
    bounty = _mkt(request).get_bounty_for_viewer(bounty_id, ctx["tenant"])
    if bounty is None:
        raise HTTPException(404, "bounty not found")
    return bounty


@router.get("/bounties", response_model=list[BountyOut])
async def open_bounties(ctx: CtxDep, request: Request,
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0, le=10_000)) -> list[BountyOut]:
    return _mkt(request).list_open_bounties(limit=limit, offset=offset)


@router.get("/bounties/{bounty_id}/submissions", response_model=list[str],
            summary="List submissions for a bounty owned by the authenticated poster")
async def list_bounty_submissions(
        bounty_id: str, ctx: CtxDep, request: Request,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=10_000)) -> list[str]:
    try:
        return _mkt(request).list_bounty_submissions(
            ctx["tenant"], bounty_id, limit=limit, offset=offset)
    except MarketplaceError as exc:
        message = str(exc)
        if "not found" in message or "poster" in message:
            raise HTTPException(404, "bounty not found") from exc
        raise HTTPException(400, message) from exc


@router.post("/bounties/{bounty_id}/submissions", status_code=202)
async def submit_bounty(bounty_id: str, dataset_id: str, ctx: CtxDep,
                        request: Request) -> dict:
    try:
        return _mkt(request).submit_for_bounty(ctx["tenant"], bounty_id, dataset_id)
    except MarketplaceError as exc:
        message = str(exc)
        code = (404 if message == "bounty not found" else
                403 if "listing owner" in message else
                409 if ("bounty is" in message or "bounty expired" in message or
                        "already submitted" in message or
                        "published listing" in message) else 400)
        raise HTTPException(code, message) from exc


@router.post("/bounties/{bounty_id}/award")
async def award_bounty(bounty_id: str, dataset_id: str, ctx: CtxDep,
                       request: Request) -> dict:
    try:
        return _mkt(request).award_bounty(ctx["tenant"], bounty_id, dataset_id)
    except MarketplaceError as exc:
        msg = str(exc)
        code = (404 if "not found" in msg else
                403 if "poster" in msg else
                409 if ("bounty is" in msg or "bounty expired" in msg or
                        "no longer published" in msg) else 400)
        raise HTTPException(code, msg)
