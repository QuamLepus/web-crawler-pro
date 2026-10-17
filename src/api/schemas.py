"""Pydantic v2 schemas for the versioned (v1) public API."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# --------------------------------------------------------------------------- #
# Crawl jobs
# --------------------------------------------------------------------------- #
class StealthProfile(str, Enum):
    chrome = "chrome"
    firefox = "firefox"
    safari = "safari"
    edge = "edge"


class ProxyStrategy(str, Enum):
    auto = "auto"
    datacenter = "datacenter"
    residential = "residential"
    mobile = "mobile"
    none = "none"


class CrawlJobCreate(BaseModel):
    """Body for POST /v1/jobs/crawl."""

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "url": "https://example.com/products",
            "max_pages": 50,
            "stealth": True,
            "proxy_strategy": "residential",
            "webhook_url": "https://client.example/hooks/wcp",
        }
    })

    url: str = Field(pattern=r"^https?://[^\s]+$")
    max_pages: int = Field(default=10, ge=1, le=1_000_000)
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")
    obey_robots: bool = True
    stealth: bool = False
    stealth_profile: StealthProfile = StealthProfile.chrome
    proxy_strategy: ProxyStrategy = ProxyStrategy.auto
    extraction_schema: Optional[dict[str, Any]] = None  # JSON-schema for LLM parser
    webhook_url: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _no_js(cls, v: str) -> str:
        if v.lower().startswith(("javascript:", "data:", "file:")):
            raise ValueError("scheme not allowed")
        return v


class CrawlJobOut(BaseModel):
    job_id: str
    tenant: str
    state: JobState
    url: str
    max_pages: int
    pages_done: int = 0
    items_collected: int = 0
    tokens_generated: int = 0
    created_at: datetime
    updated_at: datetime
    error: Optional[str] = None


class JobListOut(BaseModel):
    items: list[CrawlJobOut]
    total: int
    page: int
    page_size: int


# --------------------------------------------------------------------------- #
# Data & export
# --------------------------------------------------------------------------- #
class DataRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    url: str
    title: Optional[str] = None
    content: str
    language: Optional[str] = None
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=utcnow)


class ExportFormat(str, Enum):
    jsonl = "jsonl"
    csv = "csv"
    parquet = "parquet"


class ExportRequest(BaseModel):
    job_id: Optional[str] = None
    format: ExportFormat = ExportFormat.jsonl
    include_metadata: bool = True
    max_records: int = Field(default=100_000, ge=1, le=10_000_000)


class ExportTicket(BaseModel):
    export_id: str
    download_url: str
    expires_at: datetime
    format: ExportFormat
    record_count: int


# --------------------------------------------------------------------------- #
# Semantic search
# --------------------------------------------------------------------------- #
class SearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(default=10, ge=1, le=100)
    filter: dict[str, Any] = Field(default_factory=dict)
    include_scores: bool = True


class SearchResult(BaseModel):
    doc_id: str
    score: float
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    results: list[SearchResult]
    took_ms: int
    embedding_backend: str


# --------------------------------------------------------------------------- #
# Topic discovery — "give me a topic and N, go find the sites and crawl them"
# --------------------------------------------------------------------------- #
class DiscoveryState(str, Enum):
    queued = "queued"
    discovering = "discovering"
    awaiting_approval = "awaiting_approval"
    crawling = "crawling"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class DiscoveryCreate(BaseModel):
    """Body for ``POST /v1/discovery``.

    Give a **topic** and how many websites you want instead of a list of URLs.
    Optionally pin a couple of ``seeds`` you already trust; they are always
    kept and ranked above pure search results.
    """

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "topic": "best budget laptops 2026",
            "count": 25,
            "seeds": ["https://www.rtings.com"],
            "language": "en",
            "auto": False,
            "max_pages_per_site": 5,
        }
    })

    topic: str = Field(min_length=2, max_length=400)
    count: int = Field(default=10, ge=1, le=500,
                       description="How many websites to shortlist")
    seeds: list[str] = Field(default_factory=list, max_length=50)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    auto: bool = Field(
        default=False,
        description=("true = crawl straight after ranking; false = stop at "
                     "awaiting_approval so a human can review the shortlist"))
    max_pages_per_site: int = Field(default=5, ge=1, le=10_000)
    per_domain: int = Field(default=1, ge=1, le=25,
                            description="Max entry points per website")
    obey_robots: bool = True
    stealth: bool = False
    webhook_url: Optional[str] = None

    @field_validator("seeds")
    @classmethod
    def _http_only(cls, v: list[str]) -> list[str]:
        for url in v:
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError(f"seed must be an http(s) URL: {url!r}")
        return v

    @field_validator("topic")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("topic is too short")
        return v


class CandidateOut(BaseModel):
    """One proposed website, with the reasoning behind its score."""

    url: str
    domain: str
    title: str = ""
    snippet: str = ""
    score: float = Field(ge=0.0, le=1.0)
    providers: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    seeded: bool = False
    rejected: bool = False
    selected: bool = False


class DiscoveryOut(BaseModel):
    job_id: str
    tenant: str
    topic: str
    count: int
    state: DiscoveryState
    language: str = "en"
    auto: bool = False
    max_pages_per_site: int = 5
    per_domain: int = 1
    seeds: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    candidates: int = 0
    selected: int = 0
    pages: int = 0
    items: int = 0
    sites_total: int = 0
    sites_succeeded: int = 0
    sites_failed: int = 0
    created_at: datetime
    updated_at: datetime
    error: Optional[str] = None


class DiscoveryListOut(BaseModel):
    items: list[DiscoveryOut]
    total: int
    page: int
    page_size: int


class DiscoveryApprove(BaseModel):
    """Body for ``POST /v1/discovery/{job_id}/approve``."""

    domains: Optional[list[str]] = Field(
        default=None,
        description=("Restrict the crawl to these domains. Omit to crawl the "
                     "auto-selected shortlist."))


class SiteResultOut(BaseModel):
    domain: str
    state: str
    pages: int = 0
    items: int = 0
    data_sha256: str = ""
    error: str = ""


class DiscoveryResultsOut(BaseModel):
    job_id: str
    topic: str
    state: str
    sites_total: int = 0
    sites_succeeded: int = 0
    sites_failed: int = 0
    pages: int = 0
    items: int = 0
    per_site: list[SiteResultOut] = Field(default_factory=list)
    data_sha256: str = ""
    candidates: list[CandidateOut] = Field(default_factory=list)


class DiscoveryPreviewOut(BaseModel):
    """Synchronous dry-run: what would be crawled, without crawling it."""

    topic: str
    queries: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)
    total_found: int = 0


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
class WebhookEvent(str, Enum):
    crawl_completed = "crawl.completed"
    crawl_failed = "crawl.failed"
    data_ready = "data.ready"
    export_ready = "export.ready"
    quota_warning = "quota.warning"
    dataset_sold = "dataset.sold"
    discovery_completed = "discovery.completed"
    discovery_approved = "discovery.approved"
    web_changed = "web.changed"


class WebhookCreate(BaseModel):
    url: str = Field(pattern=r"^https://")
    events: list[WebhookEvent] = Field(min_length=1)
    description: str = ""


class WebhookPayload(BaseModel):
    """Signed envelope delivered to customer endpoints."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event: WebhookEvent
    tenant: str
    occurred_at: datetime = Field(default_factory=utcnow)
    data: dict[str, Any]


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
class PlanTier(str, Enum):
    free = "free"
    starter = "starter"
    pro = "pro"
    enterprise = "enterprise"


class UsageSummary(BaseModel):
    tenant: str
    period: str
    requests: int
    pages_crawled: int
    tokens_generated: int
    storage_bytes: int
    plan: PlanTier
    overage_cost_usd_cents: int
    quota_remaining_pct: float


class CheckoutSessionRequest(BaseModel):
    plan: PlanTier
    success_url: str
    cancel_url: str

    @field_validator("plan")
    @classmethod
    def _paid(cls, v: PlanTier) -> PlanTier:
        if v == PlanTier.free:
            raise ValueError("use /billing/plans; free tier needs no checkout")
        return v


class CheckoutSessionOut(BaseModel):
    session_id: str
    url: str
    plan: PlanTier


# --------------------------------------------------------------------------- #
# Marketplace
# --------------------------------------------------------------------------- #
class DatasetListingCreate(BaseModel):
    title: str = Field(min_length=3, max_length=140)
    description: str = Field(min_length=20, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=15)
    price_usd_cents: int = Field(ge=0, le=10_000_000)
    license: str = "CC-BY-4.0"
    source_job_id: Optional[str] = None
    sample_rows: int = Field(default=50, ge=10, le=500)


class DatasetListingOut(BaseModel):
    dataset_id: str
    seller_tenant: str
    title: str
    description: str
    tags: list[str]
    price_usd_cents: int
    license: str
    status: str
    token_count: int
    preview_uri: Optional[str] = None
    rating_avg: float = 0.0
    rating_count: int = 0
    created_at: datetime


class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(min_length=5, max_length=2000)

    @field_validator("comment")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5:
            raise ValueError("comment too short")
        return v


class ReviewOut(BaseModel):
    review_id: str
    dataset_id: str
    buyer_tenant: str
    rating: int
    comment: str
    verified_purchase: bool
    created_at: datetime


class BountyCreate(BaseModel):
    title: str = Field(min_length=5, max_length=140)
    spec: str = Field(min_length=20, max_length=8000)
    reward_usd_cents: int = Field(ge=100, le=10_000_000)
    deadline_days: int = Field(default=30, ge=1, le=365)
    target_urls: list[str] = Field(default_factory=list)


class BountyOut(BaseModel):
    bounty_id: str
    poster_tenant: str
    title: str
    spec: str
    reward_usd_cents: int
    deadline: datetime
    status: str
    submissions: int = 0


class DeliveryManifest(BaseModel):
    """Signed, verifiable delivery of a purchased dataset."""

    delivery_id: str
    dataset_id: str
    buyer_tenant: str
    uri: str
    sha256: str
    size_bytes: int
    record_count: int
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    signature: str = Field(description="HMAC-SHA256 signature over canonical manifest")


# --------------------------------------------------------------------------- #
# Common
# --------------------------------------------------------------------------- #
class ApiError(BaseModel):
    code: str
    message: str
    detail: Any | None = None
    request_id: str = ""


class HealthOut(BaseModel):
    status: str
    version: str
    checks: dict[str, str] = Field(default_factory=dict)
