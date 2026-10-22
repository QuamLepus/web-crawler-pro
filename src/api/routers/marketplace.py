"""v1 router: Data Marketplace (listings, previews, reviews, bounties, purchases)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from src.api.routers.jobs import CtxDep
from src.api.schemas import (BountyCreate, BountyOut, DatasetListingCreate,
                             DatasetListingOut, DeliveryManifest, ReviewCreate,
                             ReviewOut)
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
    # In production the source job's output object carries the digest; here we
    # require it via metadata on the job record and fail clearly when absent.
    sha = ""
    rec: dict = {}
    if body.source_job_id:
        raw = mkt.r.hget(f"wcp:v1:job:{body.source_job_id}", "data_sha256")
        sha = raw.decode() if isinstance(raw, bytes) else (raw or "")
    if not sha:
        raise HTTPException(422, "dataset must be hashed before listing "
                                 "(publish via worker pipeline or set data_sha256)")
    try:
        lst = mkt.create_listing(
            ctx["tenant"], title=body.title, description=body.description,
            tags=body.tags, price_usd_cents=body.price_usd_cents,
            license=body.license, content_sha256=sha,
            record_count=int(rec.get("items_collected", 0) or 0),
            token_count=int(rec.get("tokens_generated", 0) or 0),
            storage_uri=f"s3://wcp-tenants/{ctx['tenant']}/{body.source_job_id}")
    except MarketplaceError as exc:
        raise HTTPException(422, str(exc))
    return mkt.to_out(lst)


@router.get("/datasets", response_model=list[DatasetListingOut],
            summary="Search published datasets")
async def search_datasets(ctx: CtxDep, request: Request,
                          q: str = "", tag: str = "",
                          max_price_cents: int | None = Query(None, ge=0),
                          limit: int = Query(20, ge=1, le=100)):
    mkt = _mkt(request)
    found = mkt.search_listings(query=q, tag=tag,
                                max_price_cents=max_price_cents, limit=limit)
    return [mkt.to_out(l) for l in found]


@router.get("/datasets/{dataset_id}", response_model=DatasetListingOut)
async def get_dataset(dataset_id: str, ctx: CtxDep, request: Request):
    mkt = _mkt(request)
    lst = mkt.get_listing(dataset_id)
    if lst is None or (lst.status != "published" and lst.seller_tenant != ctx["tenant"]):
        # Draft/suspended inventory is visible only to its owning tenant.
        raise HTTPException(404, "dataset not found")
    return mkt.to_out(lst)


@router.get("/datasets/{dataset_id}/preview", summary="Sample rows (PII-safe)")
async def dataset_preview(dataset_id: str, ctx: CtxDep, request: Request,
                          rows: int = Query(50, ge=10, le=500)) -> list[dict]:
    mkt = _mkt(request)
    if mkt.get_listing(dataset_id) is None:
        raise HTTPException(404, "dataset not found")
    # pull sample from tenant object store when configured, else deterministic stub
    try:
        from src.pipeline.storage.json_exporter import load_sample

        records = load_sample(dataset_id, n=rows)
    except Exception:
        records = [{"content": f"sample row {i} of {dataset_id}",
                    "url": "https://example.com"} for i in range(rows)]
    return mkt.build_preview(dataset_id, records, rows=rows)


@router.patch("/datasets/{dataset_id}", response_model=DatasetListingOut)
async def update_dataset(dataset_id: str, changes: dict, ctx: CtxDep,
                         request: Request) -> DatasetListingOut:
    mkt = _mkt(request)
    try:
        lst = mkt.update_listing(dataset_id, ctx["tenant"], **changes)
    except MarketplaceError as exc:
        msg = str(exc)
        code = 404 if msg == "not found" else 403 if "forbidden" in msg else 422
        raise HTTPException(code, msg)
    return mkt.to_out(lst)


# ------------------------------ reviews ----------------------------------- #
@router.post("/datasets/{dataset_id}/reviews", response_model=ReviewOut, status_code=201)
async def add_review(dataset_id: str, body: ReviewCreate, ctx: CtxDep,
                     request: Request) -> ReviewOut:
    try:
        return _mkt(request).add_review(ctx["tenant"], dataset_id,
                                        body.rating, body.comment)
    except MarketplaceError as exc:
        raise HTTPException(400, str(exc))


@router.get("/datasets/{dataset_id}/reviews", response_model=list[ReviewOut])
async def get_reviews(dataset_id: str, ctx: CtxDep, request: Request,
                      limit: int = Query(50, ge=1, le=200)) -> list[ReviewOut]:
    return _mkt(request).list_reviews(dataset_id, limit=limit)


# ------------------------------ purchase / delivery ----------------------- #
@router.post("/datasets/{dataset_id}/purchase",
             summary="Buy a dataset; returns signed delivery manifest")
async def purchase(dataset_id: str, ctx: CtxDep, request: Request) -> dict:
    mkt = _mkt(request)
    try:
        pur, manifest = mkt.purchase(ctx["tenant"], dataset_id)
    except MarketplaceError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg else 409 if "own" in msg or "not available" in msg else 400
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
            "verify": "check HMAC signature + sha256 of downloaded object"}


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
                     status=b.status, submissions=b.submissions)


@router.get("/bounties", response_model=list[BountyOut])
async def open_bounties(ctx: CtxDep, request: Request,
                        limit: int = Query(50, ge=1, le=200)) -> list[BountyOut]:
    return _mkt(request).list_open_bounties(limit=limit)


@router.post("/bounties/{bounty_id}/submissions", status_code=202)
async def submit_bounty(bounty_id: str, dataset_id: str, ctx: CtxDep,
                        request: Request) -> dict:
    try:
        return _mkt(request).submit_for_bounty(ctx["tenant"], bounty_id, dataset_id)
    except MarketplaceError as exc:
        raise HTTPException(400, str(exc))


@router.post("/bounties/{bounty_id}/award")
async def award_bounty(bounty_id: str, dataset_id: str, ctx: CtxDep,
                       request: Request) -> dict:
    try:
        return _mkt(request).award_bounty(ctx["tenant"], bounty_id, dataset_id)
    except MarketplaceError as exc:
        msg = str(exc)
        raise HTTPException(404 if "not found" in msg else 403 if "poster" in msg else 400, msg)
