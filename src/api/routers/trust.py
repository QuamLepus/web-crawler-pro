"""v1 router: trust — provenance, manifests, audit, retention, RBAC, SSO.

    GET  /v1/provenance/recent        latest records with their passports
    GET  /v1/provenance/{record_id}   one record + replay + lineage explanation
    GET  /v1/provenance/verify        re-walk the hash chain (tamper check)
    POST /v1/provenance/manifest      build (and sign) a dataset passport
    POST /v1/provenance/verify-manifest
    GET  /v1/audit                    audit log (chain-verifiable)
    GET  /v1/audit/verify             verify the audit chain
    GET  /v1/audit/export             compliance export
    GET/PUT /v1/retention             retention policy
    POST /v1/retention/plan           what a retention run would delete
    POST /v1/retention/run            do it, with a receipt
    POST /v1/erasure/plan | /apply    subject erasure with a receipt
    GET  /v1/rbac                     the permission matrix
    POST /v1/sso/oidc/verify          validate an ID token (no user session needed)
    GET  /v1/sso/saml                 the honest SAML answer
    GET  /v1/system/health            one call, everything the ops page shows
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.api.routers.jobs import CtxDep
from src.enterprise.governance import RetentionPolicy, plan_erasure
from src.enterprise.sso import OIDCConfig, SSOError, JWKSCache, verify_id_token

logger = logging.getLogger("wcp.api.trust")
router = APIRouter(tags=["trust"])


class ManifestBody(BaseModel):
    dataset: str = Field(..., min_length=1, max_length=200)
    version: str = ""
    sign: bool = True
    markdown: bool = False


class VerifyManifestBody(BaseModel):
    manifest: dict[str, Any]


class RetentionBody(BaseModel):
    snapshots_days: int | None = Field(None, ge=0, le=3650)
    datasets_days: int | None = Field(None, ge=0, le=3650)
    jobs_days: int | None = Field(None, ge=0, le=3650)
    audit_days: int | None = Field(None, ge=0, le=3650)
    spans_days: int | None = Field(None, ge=0, le=3650)
    ledger_days: int | None = Field(None, ge=0, le=3650)
    research_days: int | None = Field(None, ge=0, le=3650)
    documents_days: int | None = Field(None, ge=0, le=3650)


class ErasureBody(BaseModel):
    subject: str = Field(..., min_length=2, max_length=200)
    basis: str = "GDPR Art. 17 request"
    apply: bool = False


class OIDCVerifyBody(BaseModel):
    token: str = Field(..., min_length=10)
    issuer: str
    client_id: str
    audience: str = ""
    jwks_uri: str = ""
    jwks: dict[str, Any] | None = None
    role_claim: str = "roles"
    require_verified_email: bool = True


def _platform(request: Request):
    return request.app.state.services.platform


def _audit(request: Request, ctx: dict, action: str, **fields: Any) -> None:
    try:
        _platform(request).audit.log(action, tenant=ctx.get("tenant", ""),
                                     actor=str(ctx.get("user") or ctx.get("principal", "")),
                                     actor_kind="api_key" if ctx.get("principal") == "api_key"
                                     else "user", **fields)
    except Exception as exc:  # noqa: BLE001 - auditing must never break a call
        logger.debug("audit write failed: %s", exc)


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
@router.get("/provenance/recent", summary="Latest provenance records")
async def provenance_recent(request: Request, ctx: CtxDep,
                            limit: Annotated[int, Query(ge=1, le=200)] = 25
                            ) -> dict[str, Any]:
    ledger = _platform(request).ledger
    tenant = ctx["tenant"]
    records = ledger.recent(tenant, limit=limit)
    return {
        "items": [r.to_dict() for r in records],
        "stats": ledger.stats(tenant).to_dict(),
        "head": ledger.head(tenant),
    }


@router.get("/provenance/verify", summary="Re-walk the provenance hash chain")
async def provenance_verify(request: Request, ctx: CtxDep) -> dict[str, Any]:
    return _platform(request).ledger.verify(ctx["tenant"])


@router.get("/provenance/{record_id}", summary="One record, replayable")
async def provenance_record(record_id: str, request: Request, ctx: CtxDep,
                            replay: bool = False) -> dict[str, Any]:
    platform = _platform(request)
    try:
        payload = platform.lineage(record_id, tenant=ctx["tenant"])
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "record not found")
    if not replay:
        payload.pop("replay", None)
    return payload


@router.post("/provenance/manifest", summary="Build a signed dataset passport")
async def provenance_manifest(body: ManifestBody, request: Request, ctx: CtxDep
                              ) -> dict[str, Any]:
    platform = _platform(request)
    manifest = platform.provenance_manifest(
        body.dataset, tenant=ctx["tenant"], version=body.version, sign=body.sign,
        markdown=body.markdown)
    _audit(request, ctx, "provenance.export", dataset=body.dataset,
           records=manifest.get("record_count"))
    return manifest


@router.post("/provenance/verify-manifest", summary="Verify a signed manifest")
async def provenance_verify_manifest(body: VerifyManifestBody, request: Request,
                                     ctx: CtxDep) -> dict[str, Any]:
    result = _platform(request).verify_manifest(body.manifest)
    _audit(request, ctx, "provenance.verify", ok=result.get("ok"))
    return result


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
@router.get("/audit", summary="Audit log (hash-chained)")
async def audit_log(request: Request, ctx: CtxDep,
                    limit: Annotated[int, Query(ge=1, le=500)] = 100,
                    action: str = "", actor: str = "") -> dict[str, Any]:
    audit = _platform(request).audit
    tenant = ctx["tenant"]
    entries = audit.recent(tenant, limit=limit, action=action, actor=actor)
    return {"items": [e.to_dict() for e in entries],
            "counts": audit.counts_by_action(tenant, limit=1000),
            "head": audit.r.get(audit._k(tenant, "head"))}


@router.get("/audit/verify", summary="Verify the audit chain")
async def audit_verify(request: Request, ctx: CtxDep) -> dict[str, Any]:
    return _platform(request).audit.verify(ctx["tenant"])


@router.get("/audit/export", summary="Compliance export (entries + verification)")
async def audit_export(request: Request, ctx: CtxDep,
                       limit: Annotated[int, Query(ge=1, le=20000)] = 5000
                       ) -> dict[str, Any]:
    _audit(request, ctx, "audit.export", limit=limit)
    return _platform(request).audit.export(ctx["tenant"], limit=limit)


# --------------------------------------------------------------------------- #
# retention and erasure
# --------------------------------------------------------------------------- #
@router.get("/retention", summary="Current retention policy")
async def get_retention(request: Request, ctx: CtxDep) -> dict[str, Any]:
    policy = _platform(request).retention_policy(ctx["tenant"])
    return {"policy": policy.to_dict(), "problems": policy.validate()}


@router.put("/retention", summary="Set the retention policy")
async def set_retention(body: RetentionBody, request: Request, ctx: CtxDep
                        ) -> dict[str, Any]:
    platform = _platform(request)
    current = platform.retention_policy(ctx["tenant"]).to_dict()
    current.update({k: v for k, v in body.model_dump().items() if v is not None})
    return platform.set_retention_policy(RetentionPolicy.from_dict(current),
                                         tenant=ctx["tenant"])


@router.post("/retention/plan", summary="What a retention run would delete")
async def retention_plan(request: Request, ctx: CtxDep) -> dict[str, Any]:
    return {"actions": _platform(request).retention_plan(ctx["tenant"]),
            "note": "nothing has been deleted yet"}


@router.post("/retention/run", summary="Apply retention and return a receipt")
async def retention_run(request: Request, ctx: CtxDep) -> dict[str, Any]:
    return _platform(request).retention_apply(ctx["tenant"])


@router.post("/erasure/plan", summary="Find a data subject before erasing anything")
async def erasure_plan(body: ErasureBody, request: Request, ctx: CtxDep
                       ) -> dict[str, Any]:
    platform = _platform(request)
    tenant = ctx["tenant"]
    plan = plan_erasure(body.subject, tenant=tenant,
                        records=platform.ledger.recent(tenant, limit=2000),
                        datasets=platform.datasets.list(tenant, limit=200),
                        basis=body.basis)
    return plan.to_dict()


@router.post("/erasure/apply", summary="Redact a subject, keep the chain, issue a receipt")
async def erasure_apply(body: ErasureBody, request: Request, ctx: CtxDep
                        ) -> dict[str, Any]:
    from src.enterprise.governance import apply_erasure

    platform = _platform(request)
    tenant = ctx["tenant"]
    plan = plan_erasure(body.subject, tenant=tenant,
                        records=platform.ledger.recent(tenant, limit=2000),
                        datasets=platform.datasets.list(tenant, limit=200),
                        basis=body.basis)
    if not body.apply:
        return {"dry_run": True, "plan": plan.to_dict(),
                "note": "send apply=true to perform the redaction"}
    receipt = apply_erasure(plan, ledger=platform.ledger, audit=platform.audit,
                            snapshot_store=platform.snapshots)
    return {"dry_run": False, "receipt": receipt}


# --------------------------------------------------------------------------- #
# RBAC, SSO, backup, health
# --------------------------------------------------------------------------- #
@router.get("/rbac", summary="The role/permission matrix")
async def rbac(request: Request, ctx: CtxDep) -> dict[str, Any]:
    platform = _platform(request)
    return {"matrix": platform.rbac_matrix(),
            "your_role": ctx.get("role", "member"),
            "roles": ["viewer", "member", "admin", "owner"]}


class SsoConfigBody(BaseModel):
    issuer: str
    client_id: str
    audience: str = ""
    jwks_uri: str = ""
    role_claim: str = "roles"
    default_role: str = "member"


@router.get("/sso", summary="SSO status: OIDC supported, SAML honest")
async def sso_status(request: Request, ctx: CtxDep) -> dict[str, Any]:
    from src.enterprise.sso import configuration_template, saml_status

    stored = request.app.state.services.redis.get(f"wcp:sso:{ctx['tenant']}")
    return {
        "oidc": {"supported": True,
                 "configured": bool(stored),
                 "config": (stored.decode() if isinstance(stored, bytes) else stored)
                 and __import__("json").loads(stored if isinstance(stored, str)
                                              else stored.decode())},
        "saml": saml_status(),
        "template": configuration_template(),
    }


@router.put("/sso/oidc", summary="Configure OIDC for this tenant")
async def sso_configure(body: SsoConfigBody, request: Request, ctx: CtxDep
                        ) -> dict[str, Any]:
    import json

    config = OIDCConfig(issuer=body.issuer, client_id=body.client_id,
                        audience=body.audience, jwks_uri=body.jwks_uri,
                        role_claim=body.role_claim, default_role=body.default_role)
    problems = config.validate()
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(problems))
    request.app.state.services.redis.set(f"wcp:sso:{ctx['tenant']}",
                                         json.dumps(config.to_dict()))
    _audit(request, ctx, "org.sso_configure", issuer=config.issuer)
    return {"configured": True, "config": config.to_dict()}


@router.post("/sso/oidc/verify", summary="Verify an OIDC ID token (signature + claims)")
async def sso_verify(body: OIDCVerifyBody, request: Request, ctx: CtxDep
                     ) -> dict[str, Any]:
    config = OIDCConfig(issuer=body.issuer, client_id=body.client_id,
                        audience=body.audience, jwks_uri=body.jwks_uri,
                        role_claim=body.role_claim,
                        require_verified_email=body.require_verified_email)
    jwks = body.jwks
    if jwks is None and body.jwks_uri:
        try:
            jwks = JWKSCache().get(body.jwks_uri)
        except SSOError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    try:
        identity = verify_id_token(body.token, config, jwks=jwks)
    except SSOError as exc:
        _audit(request, ctx, "auth.failed_login", reason=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    from src.enterprise.sso import map_role

    return {"identity": identity.to_dict(), "mapped_role": map_role(config, identity)}


@router.get("/system/health", summary="One call for the ops page")
async def system_health(request: Request, ctx: CtxDep) -> dict[str, Any]:
    platform = _platform(request)
    tenant = ctx["tenant"]
    checks = platform.health(tenant)
    try:
        checks["doctor"] = __import__("src.doctor.service", fromlist=["diagnose"]) \
            .diagnose(platform.doctor.recent(tenant, limit=300)).to_dict()
    except Exception as exc:  # noqa: BLE001
        checks["doctor"] = {"error": str(exc)}
    return checks
