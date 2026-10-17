"""Tenant-managed OpenID Connect SSO with PKCE, encrypted client secrets and audit events.

This is an OIDC-only SSO boundary: tenant admins configure a trusted issuer,
email-domain allowlist and optional low-privilege JIT provisioning. Login does
not create organizations, elevate roles, or accept caller-supplied redirects.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from src.api.routers.jobs import CtxDep

router = APIRouter(prefix="/sso", tags=["enterprise-sso"])
auth_router = APIRouter(prefix="/api/auth/oidc", tags=["authentication"])
CONFIG_KEY = "wcp:v1:sso:oidc:tenant:{tenant}"
METADATA_KEY = "wcp:v1:sso:oidc:metadata:{tenant}"
STATE_KEY = "wcp:v1:sso:oidc:state:{digest}"
AUDIT_KEY = "wcp:v1:sso:audit:{tenant}"
STATE_COOKIE = "wcp_oidc_state"
STATE_TTL = 300
AUDIT_LIMIT = 1000
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
logger = logging.getLogger(__name__)


class OIDCConfigInput(BaseModel):
    issuer: str = Field(min_length=8, max_length=2048)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str = Field(min_length=1, max_length=4096)
    allowed_domains: list[str] = Field(min_length=1, max_length=50)
    jit_provisioning: bool = False
    default_role: str = Field(default="viewer", pattern=r"^(viewer|member)$")

    @field_validator("issuer")
    @classmethod
    def validate_issuer(cls, value: str) -> str:
        parsed = urlsplit(value.strip().rstrip("/"))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or
                parsed.port not in (None, 443)):
            raise ValueError("issuer must be an HTTPS URL without credentials, query, or non-standard port")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "metadata.google.internal"} or "." not in host:
            raise ValueError("issuer must use a public DNS hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("issuer must use a DNS hostname, not an IP address")
        return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        out = list(dict.fromkeys(value.strip().lower().rstrip(".") for value in values))
        if any(not DOMAIN_RE.fullmatch(value) for value in out):
            raise ValueError("allowed_domains must contain exact public DNS domains")
        return out


class _OIDCFailure(Exception):
    pass


def _fernet() -> Fernet:
    raw = os.getenv("WCP_SSO_ENCRYPTION_KEY", "").strip().encode()
    if not raw:
        raise HTTPException(503, "SSO is disabled until WCP_SSO_ENCRYPTION_KEY is configured")
    try:
        return Fernet(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "WCP_SSO_ENCRYPTION_KEY is invalid") from exc


def _tenant(ctx: dict) -> str:
    tenant = str(ctx.get("tenant", ""))
    if not re.fullmatch(r"[a-f0-9]{10}", tenant):
        raise HTTPException(403, "SSO requires an organization owner or admin")
    if ctx.get("role") not in {"owner", "admin"} or not ctx.get("user"):
        raise HTTPException(403, "SSO configuration requires an organization owner or admin")
    return tenant


def _read_config(request: Request, tenant: str) -> dict:
    raw = request.app.state.services.redis.get(CONFIG_KEY.format(tenant=tenant))
    if not raw:
        raise HTTPException(404, "OIDC SSO is not configured")
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def _public_config(record: dict) -> dict:
    return {key: value for key, value in record.items()
            if key not in {"client_secret_enc", "tenant"}} | {
                "client_secret_configured": bool(record.get("client_secret_enc"))}


def _audit(redis, tenant: str, event: str, *, user_id: str = "", result: str = "") -> None:
    entry = json.dumps({"event_id": uuid.uuid4().hex[:16], "event": event,
                        "user_id": user_id, "result": result,
                        "at": datetime.now(UTC).isoformat()},
                       separators=(",", ":"))
    key = AUDIT_KEY.format(tenant=tenant)
    pipe = redis.pipeline()
    pipe.lpush(key, entry)
    pipe.ltrim(key, 0, AUDIT_LIMIT - 1)
    pipe.expire(key, 365 * 86400)
    pipe.execute()


def _public_base() -> str:
    base = os.getenv("WCP_PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(base)
    local_http = parsed.hostname in {"localhost", "127.0.0.1"} and parsed.scheme == "http"
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or
            parsed.fragment or parsed.path not in ("", "/") or
            (parsed.scheme != "https" and not local_http)):
        raise HTTPException(503, "configure WCP_PUBLIC_BASE_URL as the public HTTPS origin")
    return base


def _resolve_public(host: str) -> None:
    """Reject names resolving to non-global addresses before outbound OIDC calls."""
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise _OIDCFailure("identity provider hostname did not resolve") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise _OIDCFailure("identity provider resolves to a non-public address")


def _validate_endpoint(value: str, issuer_host: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.port not in (None, 443) or
            parsed.hostname.lower().rstrip(".") != issuer_host):
        raise _OIDCFailure("identity provider endpoints must use HTTPS on the configured issuer host")
    return value


async def _http_json(url: str, *, method: str = "GET", data: dict | None = None,
                     auth: httpx.BasicAuth | None = None) -> dict:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise _OIDCFailure("invalid identity provider URL")
    await asyncio.to_thread(_resolve_public, parsed.hostname)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0),
                                     follow_redirects=False) as client, \
                client.stream(method, url, data=data, auth=auth,
                              headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                raise _OIDCFailure("identity provider returned an invalid response")
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > 1_000_000:
                raise _OIDCFailure("identity provider response was too large")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1_000_000:
                    raise _OIDCFailure("identity provider response was too large")
    except _OIDCFailure:
        raise
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise _OIDCFailure("identity provider request failed") from exc
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise _OIDCFailure("identity provider response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _OIDCFailure("identity provider response was not an object")
    return payload


async def _metadata(request: Request, tenant: str, config: dict, *, refresh: bool = False) -> dict:
    redis = request.app.state.services.redis
    cache_key = METADATA_KEY.format(tenant=tenant)
    cached = None if refresh else redis.get(cache_key)
    if cached:
        return json.loads(cached.decode() if isinstance(cached, bytes) else cached)
    issuer = config["issuer"]
    issuer_host = urlsplit(issuer).hostname.lower().rstrip(".")
    try:
        await asyncio.to_thread(_resolve_public, issuer_host)
        discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        metadata = await _http_json(discovery_url)
        if metadata.get("issuer") != issuer:
            raise _OIDCFailure("identity provider issuer did not match configuration")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            metadata[name] = _validate_endpoint(str(metadata.get(name, "")), issuer_host)
        jwks = await _http_json(metadata["jwks_uri"])
        if not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
            raise _OIDCFailure("identity provider returned no signing keys")
        metadata["_jwks"] = jwks["keys"]
        redis.set(cache_key, json.dumps(metadata, separators=(",", ":")), ex=300)
        return metadata
    except _OIDCFailure:
        raise
    except Exception as exc:
        raise _OIDCFailure("identity provider metadata could not be loaded") from exc


def _verify_id_token(token: str, config: dict, metadata: dict, nonce: str) -> dict:
    try:
        from jose import jwt
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise _OIDCFailure("ID token must use a known RS256 signing key")
        key = next((candidate for candidate in metadata["_jwks"]
                    if candidate.get("kid") == header["kid"] and
                    candidate.get("kty") == "RSA" and
                    candidate.get("use", "sig") == "sig" and
                    candidate.get("alg", "RS256") == "RS256"), None)
        if key is None:
            raise _OIDCFailure("ID token signing key was not found")
        claims = jwt.decode(token, key, algorithms=["RS256"],
                            audience=config["client_id"], issuer=config["issuer"],
                            options={"require_exp": True, "require_iat": True,
                                     "require_sub": True})
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise _OIDCFailure("ID token nonce did not match")
        audiences = claims.get("aud")
        if isinstance(audiences, list) and len(audiences) > 1 and claims.get("azp") != config["client_id"]:
            raise _OIDCFailure("ID token authorized party did not match")
        if claims.get("email_verified") is not True:
            raise _OIDCFailure("identity provider email is not verified")
        email = str(claims.get("email", "")).strip().lower()
        subject = str(claims.get("sub", ""))
        local_part, separator, domain = email.rpartition("@")
        if (len(email) > 254 or not separator or not local_part or len(local_part) > 64 or
                not DOMAIN_RE.fullmatch(domain) or not subject or len(subject) > 255):
            raise _OIDCFailure("ID token omitted a valid email or subject")
        if domain not in config["allowed_domains"]:
            raise _OIDCFailure("email domain is not allowed for this organization")
        return {"email": email, "subject": subject,
                "name": str(claims.get("name", ""))[:120]}
    except _OIDCFailure:
        raise
    except Exception as exc:
        raise _OIDCFailure("ID token validation failed") from exc


def _consume_state(redis, state: str) -> dict | None:
    key = STATE_KEY.format(digest=hashlib.sha256(state.encode()).hexdigest())
    raw = None
    if hasattr(redis, "getdel"):
        try:
            raw = redis.getdel(key)
        except Exception:  # noqa: BLE001 - use non-atomic fallback for old Redis
            raw = None
    if raw is None:  # compatibility fallback for older Redis clients/servers
        raw = redis.get(key)
        if raw:
            redis.delete(key)
    if not raw:
        return None
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def _release_lock(redis, key: str, token: str) -> None:
    try:
        redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then "
                   "return redis.call('del', KEYS[1]) else return 0 end", 1, key, token)
    except Exception:  # lease expires if Redis/Lua cleanup is unavailable
        logger.debug("SSO provision lock cleanup failed; lease will expire", exc_info=True)


def _link_or_provision(services, tenant: str, record: dict, identity: dict):
    """Serialize first-login email linking/JIT so Redis uniqueness is preserved."""
    users = services.users
    provider = "oidc:" + tenant
    user = users.get_by_oauth(provider, identity["subject"])
    if user is not None:
        if user.tenant != tenant or user.disabled:
            raise _OIDCFailure("organization account is unavailable")
        return user
    digest = hashlib.sha256((tenant + "\0" + identity["email"]).encode()).hexdigest()
    lock_key = "wcp:v1:sso:provision-lock:" + digest
    lock_token = secrets.token_hex(16)
    if not services.redis.set(lock_key, lock_token, nx=True, ex=30):
        # Another callback may have completed while this request waited.
        user = users.get_by_oauth(provider, identity["subject"])
        if user is not None and user.tenant == tenant and not user.disabled:
            return user
        raise _OIDCFailure("account provisioning is already in progress; retry sign-in")
    try:
        user = users.get_by_oauth(provider, identity["subject"])
        if user is not None:
            if user.tenant != tenant or user.disabled:
                raise _OIDCFailure("organization account is unavailable")
            return user
        user = users.get_by_email(identity["email"])
        if user is not None:
            if user.tenant != tenant:
                raise _OIDCFailure("email is registered to a different organization")
            if user.disabled:
                raise _OIDCFailure("account is disabled")
            try:
                users.link_oauth_identity(user.id, provider, identity["subject"])
            except (ValueError, LookupError) as exc:
                raise _OIDCFailure("external identity is already linked to another account") from exc
            return users.get_user(user.id)
        if not record["jit_provisioning"]:
            raise _OIDCFailure("no organization account exists and JIT provisioning is disabled")
        try:
            return users.create_user(identity["email"], None, tenant,
                                     record["default_role"], name=identity["name"],
                                     oauth_provider=provider,
                                     oauth_id=identity["subject"])
        except ValueError as exc:
            user = users.get_by_oauth(provider, identity["subject"])
            if user is None or user.tenant != tenant or user.disabled:
                raise _OIDCFailure("account provisioning conflicted; retry sign-in") from exc
            return user
    finally:
        _release_lock(services.redis, lock_key, lock_token)


@router.put("/oidc", summary="Configure tenant OpenID Connect SSO")
async def configure_oidc(body: OIDCConfigInput, request: Request, ctx: CtxDep) -> dict:
    tenant = _tenant(ctx)
    services = request.app.state.services
    organization = services.users.get_org(tenant)
    if organization is None:
        raise HTTPException(404, "organization not found")
    if organization.plan != "enterprise":
        raise HTTPException(403, "OIDC SSO requires the enterprise plan")
    fernet = _fernet()
    key = CONFIG_KEY.format(tenant=tenant)
    existing = services.redis.get(key)
    old = json.loads(existing.decode() if isinstance(existing, bytes) else existing) if existing else {}
    now = datetime.now(UTC).isoformat()
    record = {"tenant": tenant, "issuer": body.issuer, "client_id": body.client_id,
              "client_secret_enc": fernet.encrypt(body.client_secret.encode()).decode(),
              "allowed_domains": body.allowed_domains,
              "jit_provisioning": body.jit_provisioning,
              "default_role": body.default_role, "enabled": True,
              "created_at": old.get("created_at", now), "updated_at": now}
    services.redis.set(key, json.dumps(record, separators=(",", ":")))
    services.redis.delete(METADATA_KEY.format(tenant=tenant))
    _audit(services.redis, tenant, "sso.oidc.configured", user_id=str(ctx["user"]), result="success")
    return _public_config(record) | {"login_path": f"/api/auth/oidc/{tenant}/login"}


@router.get("/oidc", summary="Read tenant OIDC SSO configuration")
async def get_oidc(request: Request, ctx: CtxDep) -> dict:
    tenant = _tenant(ctx)
    return _public_config(_read_config(request, tenant)) | {
        "login_path": f"/api/auth/oidc/{tenant}/login"}


@router.delete("/oidc", status_code=204, summary="Disable tenant OIDC SSO")
async def delete_oidc(request: Request, ctx: CtxDep) -> None:
    tenant = _tenant(ctx)
    redis = request.app.state.services.redis
    redis.delete(CONFIG_KEY.format(tenant=tenant), METADATA_KEY.format(tenant=tenant))
    _audit(redis, tenant, "sso.oidc.disabled", user_id=str(ctx["user"]), result="success")


@router.get("/audit", summary="Read tenant SSO audit events")
async def sso_audit(request: Request, ctx: CtxDep,
                    limit: int = Query(100, ge=1, le=200)) -> dict:
    tenant = _tenant(ctx)
    redis = request.app.state.services.redis
    raw_items = redis.lrange(AUDIT_KEY.format(tenant=tenant), 0, limit - 1)
    items = [json.loads(raw.decode() if isinstance(raw, bytes) else raw) for raw in raw_items]
    return {"items": items, "limit": limit}


@auth_router.get("/{tenant}/login", summary="Begin tenant OIDC sign-in")
async def oidc_login(tenant: str, request: Request) -> RedirectResponse:
    if not re.fullmatch(r"[a-f0-9]{10}", tenant):
        raise HTTPException(404, "SSO provider not found")
    record = _read_config(request, tenant)
    services = request.app.state.services
    organization = services.users.get_org(tenant)
    if organization is None or organization.plan != "enterprise":
        raise HTTPException(404, "SSO provider not found")
    base = _public_base()
    try:
        metadata = await _metadata(request, tenant, record)
    except _OIDCFailure as exc:
        _audit(services.redis, tenant, "sso.oidc.login.failed", result="provider_metadata")
        raise HTTPException(503, str(exc)) from exc
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    transaction = {"tenant": tenant, "nonce": nonce, "verifier": verifier,
                   "created_at": time.time()}
    digest = hashlib.sha256(state.encode()).hexdigest()
    services.redis.set(STATE_KEY.format(digest=digest),
                       json.dumps(transaction, separators=(",", ":")), ex=STATE_TTL)
    callback = base + "/api/auth/oidc/callback"
    params = urlencode({"client_id": record["client_id"], "response_type": "code",
                        "redirect_uri": callback, "scope": "openid email profile",
                        "state": state, "nonce": nonce,
                        "code_challenge": challenge, "code_challenge_method": "S256"})
    endpoint = metadata["authorization_endpoint"]
    separator = "&" if urlsplit(endpoint).query else "?"
    response = RedirectResponse(endpoint + separator + params, status_code=302)
    response.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL, httponly=True,
                        secure=urlsplit(base).scheme == "https", samesite="lax",
                        path="/api/auth/oidc/callback")
    response.headers["Cache-Control"] = "no-store"
    return response


@auth_router.get("/callback", summary="Complete tenant OIDC sign-in")
async def oidc_callback(request: Request, state: str = Query("", max_length=256),
                        code: str = Query("", max_length=4096),
                        error: str = Query("", max_length=128)):
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return PlainTextResponse("SSO sign-in failed: invalid state.", status_code=400,
                                 headers={"Cache-Control": "no-store"})
    services = request.app.state.services
    transaction = _consume_state(services.redis, state)
    response_headers = {"Cache-Control": "no-store"}
    if not transaction or time.time() - float(transaction.get("created_at", 0)) > STATE_TTL:
        response = PlainTextResponse("SSO sign-in failed: expired transaction.", status_code=400,
                                     headers=response_headers)
        response.delete_cookie(STATE_COOKIE, path="/api/auth/oidc/callback")
        return response
    tenant = transaction.get("tenant", "")
    if error or not code:
        _audit(services.redis, tenant, "sso.oidc.login.failed", result="provider_denied")
        response = PlainTextResponse("SSO sign-in was not completed.", status_code=400,
                                     headers=response_headers)
        response.delete_cookie(STATE_COOKIE, path="/api/auth/oidc/callback")
        return response
    try:
        record = _read_config(request, tenant)
        metadata = await _metadata(request, tenant, record)
        base = _public_base()
        fernet = _fernet()
        try:
            secret = fernet.decrypt(record["client_secret_enc"].encode()).decode()
        except InvalidToken as exc:
            raise _OIDCFailure("stored client secret could not be decrypted") from exc
        token_payload = await _http_json(
            metadata["token_endpoint"], method="POST",
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": base + "/api/auth/oidc/callback",
                  "code_verifier": transaction["verifier"]},
            auth=httpx.BasicAuth(record["client_id"], secret))
        id_token = token_payload.get("id_token")
        if not isinstance(id_token, str) or len(id_token) > 100_000:
            raise _OIDCFailure("identity provider omitted an ID token")
        identity = _verify_id_token(id_token, record, metadata, transaction["nonce"])
        user = _link_or_provision(services, tenant, record, identity)
        if user is None or user.tenant != tenant or user.disabled:
            raise _OIDCFailure("organization account is unavailable")
        from src.gateway.auth.tokens import issue_session
        session = issue_session(user.id, user.tenant, user.role)
        response = RedirectResponse(base + "/app", status_code=303)
        response.set_cookie("wcp_session", session["access_token"],
                            max_age=int(session.get("expires_in", 3600)),
                            httponly=True, secure=urlsplit(base).scheme == "https",
                            samesite="lax", path="/")
        response.delete_cookie(STATE_COOKIE, path="/api/auth/oidc/callback")
        response.headers["Cache-Control"] = "no-store"
        _audit(services.redis, tenant, "sso.oidc.login.succeeded",
               user_id=user.id, result="success")
        return response
    except _OIDCFailure:
        _audit(services.redis, tenant, "sso.oidc.login.failed", result="validation")
        response = PlainTextResponse(
            "SSO sign-in failed. Verify workspace access with your organization administrator.",
            status_code=401, headers=response_headers)
    except HTTPException as exc:
        _audit(services.redis, tenant, "sso.oidc.login.failed", result="configuration")
        response = PlainTextResponse("SSO sign-in is not configured correctly.",
                                     status_code=exc.status_code, headers=response_headers)
    except Exception:  # noqa: BLE001 - never return token/provider internals
        _audit(services.redis, tenant, "sso.oidc.login.failed", result="internal")
        response = PlainTextResponse("SSO sign-in failed. Contact your organization admin.",
                                     status_code=401, headers=response_headers)
    response.delete_cookie(STATE_COOKIE, path="/api/auth/oidc/callback")
    return response
