"""v1 router: billing (plans, checkout, usage, invoices) — Stripe-backed."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from src.api.routers.jobs import CtxDep
from src.api.schemas import (CheckoutSessionOut, CheckoutSessionRequest,
                              PlanTier, UsageSummary)
from src.gateway.billing.plans import PLANS, build_invoice
from src.gateway.billing.quotas import QuotaStatus, current_period

logger = logging.getLogger("wcp.api.billing")
router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", summary="Public pricing tiers")
async def list_plans() -> dict:
    from dataclasses import asdict

    return {"tiers": {name: asdict(p) for name, p in PLANS.items()},
            "usage_based": {"requests_overage": "per plan UsageRates.extra_request_cost",
                            "tokens_per_million_usd": 0.35,
                            "storage_gb_month_usd": 0.02},
            "currency": "USD"}


def _usage_totals(svc, tenant: str, period: str | None = None) -> dict[str, float]:
    """Meter totals as a plain dict for :func:`build_invoice`."""
    try:
        totals = dict(svc.quotas.month_totals(tenant, period or current_period()))
    except Exception:
        totals = {}
    try:
        totals["storage_gb"] = svc.quotas.storage_gb(tenant)
    except Exception:
        pass
    return {k: float(v or 0) for k, v in totals.items()}


@router.post("/checkout", response_model=CheckoutSessionOut,
             summary="Create a Stripe Checkout session")
async def checkout(body: CheckoutSessionRequest, ctx: CtxDep,
                   request: Request) -> CheckoutSessionOut:
    svc = request.app.state.services
    user = ctx.get("user")
    email = getattr(user, "email", None) or f"{ctx['tenant']}@tenant.local"
    try:
        session = svc.billing.create_checkout(
            tenant=ctx["tenant"], plan_name=body.plan.value, email=email,
            success_url=body.success_url, cancel_url=body.cancel_url)
    except Exception as exc:
        logger.exception("stripe checkout failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"billing provider error: {exc}")
    sid = session.get("session_id") or session.get("id", "")
    url = session.get("url", "")
    return CheckoutSessionOut(session_id=sid, url=url, plan=body.plan)


@router.get("/usage", response_model=UsageSummary, summary="Current period usage")
async def usage(ctx: CtxDep, request: Request) -> UsageSummary:
    svc = request.app.state.services
    snap = svc.quotas.snapshot(ctx["tenant"])
    plan = svc.plan_for(ctx["tenant"])

    def _used(metric: str) -> float:
        st: QuotaStatus | None = snap.get(metric)
        return float(st.used) if st else 0.0

    pcts = [st.pct() for st in snap.values() if st is not None and st.pct() is not None]
    remaining = min(pcts) * 100 if pcts else 100.0
    invoice = build_invoice(plan, {**_usage_totals(svc, ctx["tenant"]),
                                   "_tenant": ctx["tenant"]},
                            storage_gb_avg=_used("storage_gb"),
                            period=current_period())
    return UsageSummary(
        tenant=ctx["tenant"], period=current_period(),
        requests=int(_used("requests")), pages_crawled=int(_used("requests")),
        tokens_generated=int(_used("tokens_processed")),
        storage_bytes=int(_used("storage_gb") * 1e9),
        plan=PlanTier(plan.name),
        overage_cost_usd_cents=int(invoice.subtotal_cents),
        quota_remaining_pct=round(remaining, 1))


@router.get("/invoice", summary="Detailed invoice for a period")
async def invoice(ctx: CtxDep, request: Request,
                 period: str | None = None) -> dict:
    from dataclasses import asdict

    svc = request.app.state.services
    plan = svc.plan_for(ctx["tenant"])
    per = period or current_period()
    totals = {**_usage_totals(svc, ctx["tenant"], per), "_tenant": ctx["tenant"]}
    inv = build_invoice(plan, totals,
                        storage_gb_avg=float(totals.get("storage_gb", 0)),
                        period=per)
    return asdict(inv)
