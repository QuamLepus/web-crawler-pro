"""Pricing tiers, quota definitions and usage-based billing rates.

Single source of truth for the SaaS plans (Free / Starter / Pro / Enterprise).
Quotas are enforced by :mod:`src.gateway.billing.quotas` at request/job time;
usage meters feed the invoice builder here and Stripe sync in
:mod:`src.gateway.billing.stripe_sync`.

All money amounts are integer cents to avoid float rounding bugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PlanName = Literal["free", "starter", "pro", "enterprise"]


@dataclass(frozen=True)
class QuotaLimits:
    """Hard monthly limits per tenant. ``None`` means unlimited."""

    requests_per_month: int | None
    storage_gb: int | None
    tokens_processed_per_month: int | None
    concurrent_jobs: int
    api_rate_per_minute: int          # sustained requests/min per API key
    burst_per_minute: int             # short-term burst allowance
    datasources: int | None           # registered sources in the registry


@dataclass(frozen=True)
class UsageRates:
    """Metered overage / pay-as-you-go rates (per unit, in USD cents)."""

    extra_request_cost: float = 0.0            # per request above quota
    storage_gb_month_cost: float = 0.0         # per GB stored per month
    token_cost_per_million: float = 0.0        # per 1M tokens processed
    gpu_minute_cost: float = 0.0               # RAPIDS/transformer GPU minutes
    proxy_gb_cost: float = 0.0                 # residential proxy traffic GB
    captcha_cost: float = 0.0                  # per solved captcha


@dataclass(frozen=True)
class Plan:
    name: PlanName
    display_name: str
    price_cents_month: int
    quotas: QuotaLimits
    rates: UsageRates
    features: tuple[str, ...] = ()
    stripe_price_id_env: str = ""              # env var holding the Price ID

    @property
    def usd(self) -> str:
        return f"${self.price_cents_month / 100:,.2f}/mo"


_FREE = Plan(
    name="free",
    display_name="Free",
    price_cents_month=0,
    quotas=QuotaLimits(
        requests_per_month=50_000,
        storage_gb=5,
        tokens_processed_per_month=50_000_000,      # 50M tokens
        concurrent_jobs=2,
        api_rate_per_minute=60,
        burst_per_minute=120,
        datasources=3,
    ),
    rates=UsageRates(),                              # no overage: hard stop
    features=("community support", "marketplace read access"),
)

_STARTER = Plan(
    name="starter",
    display_name="Starter",
    price_cents_month=99_00,
    quotas=QuotaLimits(
        requests_per_month=2_000_000,
        storage_gb=250,
        tokens_processed_per_month=5_000_000_000,    # 5B tokens
        concurrent_jobs=8,
        api_rate_per_minute=300,
        burst_per_minute=600,
        datasources=50,
    ),
    rates=UsageRates(
        extra_request_cost=0.0001,                   # $0.0001 / req
        storage_gb_month_cost=0.023,                 # $0.023 / GB-mo (S3 standard)
        token_cost_per_million=0.4,                  # $0.40 / M tokens
    ),
    features=("email support", "webhooks", "RSS/sitemap/API sources"),
    stripe_price_id_env="STRIPE_PRICE_STARTER",
)

_PRO = Plan(
    name="pro",
    display_name="Pro",
    price_cents_month=499_00,
    quotas=QuotaLimits(
        requests_per_month=25_000_000,
        storage_gb=2_000,
        tokens_processed_per_month=250_000_000_000,  # 250B tokens
        concurrent_jobs=32,
        api_rate_per_minute=1_200,
        burst_per_minute=2_400,
        datasources=500,
    ),
    rates=UsageRates(
        extra_request_cost=0.00006,
        storage_gb_month_cost=0.018,
        token_cost_per_million=0.25,
        gpu_minute_cost=0.009,
        proxy_gb_cost=8.0,
        captcha_cost=0.8,
    ),
    features=("priority queues", "Common Crawl ingestion", "Spark/Ray jobs",
              "anti-bot suite", "Slack support"),
    stripe_price_id_env="STRIPE_PRICE_PRO",
)

_ENTERPRISE = Plan(
    name="enterprise",
    display_name="Enterprise",
    price_cents_month=4_999_00,
    quotas=QuotaLimits(
        requests_per_month=None,                     # unlimited (fair use)
        storage_gb=50_000,
        tokens_processed_per_month=None,             # trillion-token contracts
        concurrent_jobs=256,
        api_rate_per_minute=6_000,
        burst_per_minute=12_000,
        datasources=None,
    ),
    rates=UsageRates(
        extra_request_cost=0.00002,
        storage_gb_month_cost=0.01,                  # volume tiering discount
        token_cost_per_million=0.08,
        gpu_minute_cost=0.007,
        proxy_gb_cost=5.5,
        captcha_cost=0.6,
    ),
    features=("dedicated workers", "SSO/OAuth2 enterprise IdP", "SLA 99.95%",
              "BYO-storage connectors", "audit log export", "on-prem MinIO option"),
    stripe_price_id_env="STRIPE_PRICE_ENTERPRISE",
)

PLANS: dict[PlanName, Plan] = {
    p.name: p for p in (_FREE, _STARTER, _PRO, _ENTERPRISE)
}


def get_plan(name: str) -> Plan:
    try:
        return PLANS[name]  # type: ignore[index]
    except KeyError:
        raise KeyError(f"unknown plan '{name}', valid: {sorted(PLANS)}") from None


@dataclass
class InvoiceLine:
    description: str
    quantity: float
    unit: str
    unit_cost_cents: float

    @property
    def total_cents(self) -> int:
        return round(self.quantity * self.unit_cost_cents)


@dataclass
class Invoice:
    tenant: str
    period: str                                  # e.g. "2026-10"
    lines: list[InvoiceLine] = field(default_factory=list)

    @property
    def subtotal_cents(self) -> int:
        return sum(ln.total_cents for ln in self.lines)

    def to_dict(self) -> dict:
        return {
            "tenant": self.tenant,
            "period": self.period,
            "lines": [
                {
                    "description": ln.description,
                    "quantity": ln.quantity,
                    "unit": ln.unit,
                    "unit_cost_cents": ln.unit_cost_cents,
                    "total_cents": ln.total_cents,
                }
                for ln in self.lines
            ],
            "subtotal_cents": self.subtotal_cents,
        }


def build_invoice(plan: Plan, usage: dict[str, float], *,
                  storage_gb_avg: float = 0.0, gpu_minutes: float = 0.0,
                  proxy_gb: float = 0.0, captchas: float = 0.0,
                  period: str = "") -> Invoice:
    """Compute a usage-based invoice from meter totals against plan quota.

    ``usage`` keys: ``requests``, ``tokens_processed`` (meter totals).
    """
    inv = Invoice(tenant=str(usage.get("_tenant", "?")), period=period)
    if plan.price_cents_month:
        inv.lines.append(InvoiceLine(f"{plan.display_name} subscription",
                                     1, "month", plan.price_cents_month))
    q = plan.quotas
    r = plan.rates
    req_over = max(0.0, usage.get("requests", 0) - (q.requests_per_month or 0)) \
        if q.requests_per_month is not None else 0.0
    if req_over > 0:
        inv.lines.append(InvoiceLine("Overage: requests", req_over, "request",
                                     r.extra_request_cost))
    tok_quota = q.tokens_processed_per_month or 0
    tok_over = max(0.0, usage.get("tokens_processed", 0) - tok_quota) \
        if q.tokens_processed_per_month is not None else 0.0
    if tok_over > 0:
        inv.lines.append(InvoiceLine("Overage: tokens processed",
                                     tok_over / 1e6, "M tokens",
                                     r.token_cost_per_million))
    stor_cap = q.storage_gb or 0
    if storage_gb_avg > stor_cap:
        inv.lines.append(InvoiceLine("Overage: storage",
                                     storage_gb_avg - stor_cap, "GB-month",
                                     r.storage_gb_month_cost))
    if gpu_minutes > 0:
        inv.lines.append(InvoiceLine("GPU preprocessing", gpu_minutes, "minute",
                                     r.gpu_minute_cost))
    if proxy_gb > 0:
        inv.lines.append(InvoiceLine("Residential proxy traffic", proxy_gb, "GB",
                                     r.proxy_gb_cost))
    if captchas > 0:
        inv.lines.append(InvoiceLine("CAPTCHA solves", captchas, "solve",
                                     r.captcha_cost))
    return inv
