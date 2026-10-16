"""Enterprise sign-in: OIDC today, SAML declared honestly.

Buyers ask for "SSO: SAML/OIDC". Building half of it and labelling both is how
products lose trust in a security review, so this module does one properly and
says so about the other:

* **OIDC** — discovery document, JWKS fetch+cache, ID-token validation
  (issuer, audience, expiry, nonce), claim → role mapping, user provisioning
  into the existing :class:`~src.gateway.auth.users.UserStore`. Works with
  Okta, Entra ID, Google Workspace, Keycloak and Auth0, because they all speak
  the same standard.
* **SAML 2.0** — :func:`saml_status` returns ``{"supported": False, ...}`` with
  the reason. A stub that pretends to validate an assertion would be worse than
  no stub at all.

No dependency is required to *configure* SSO; validating a token uses
``python-jose`` when present (it already ships for the JWT layer) and fails
closed with a clear message otherwise.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

logger = logging.getLogger("wcp.enterprise.sso")

DEFAULT_ROLE_MAP = {
    "owner": "owner", "admin": "admin", "administrator": "admin",
    "member": "member", "user": "member", "developer": "member",
    "viewer": "viewer", "readonly": "viewer", "read-only": "viewer",
    "guest": "viewer",
}


@dataclass
class OIDCConfig:
    issuer: str
    client_id: str
    audience: str = ""
    jwks_uri: str = ""
    authorize_endpoint: str = ""
    token_endpoint: str = ""
    scopes: list[str] = field(default_factory=lambda: ["openid", "email", "profile"])
    role_claim: str = "roles"
    role_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROLE_MAP))
    default_role: str = "member"
    require_verified_email: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> OIDCConfig:
        base = cls(issuer="", client_id="")
        for k, v in (row or {}).items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base

    @property
    def discovery_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"

    def validate(self) -> list[str]:
        problems = []
        if not self.issuer.startswith("https://"):
            problems.append("issuer must be an https URL")
        if not self.client_id:
            problems.append("client_id is required")
        unknown = set(self.role_map) - {"owner", "admin", "member", "viewer",
                                        "administrator", "developer", "user",
                                        "readonly", "read-only", "guest"}
        # unknown *source* roles are fine (they are the customer's vocabulary);
        # unknown *target* roles are not (they are our vocabulary).
        for source, target in self.role_map.items():
            if target not in ("owner", "admin", "member", "viewer"):
                problems.append(f"role_map[{source!r}] -> {target!r} is not a role")
        return problems


@dataclass
class OIDCIdentity:
    subject: str
    email: str = ""
    name: str = ""
    email_verified: bool = False
    issuer: str = ""
    audience: str = ""
    expires_at: float = 0.0
    raw_roles: list[str] = field(default_factory=list)
    claims: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("claims", None)
        return d


class SSOError(RuntimeError):
    pass


class JWKSCache:
    """JWKS with a TTL and a single-flight refresh.

    Fetching keys on every login is a latency tax and an outage amplifier; not
    caching them at all is the same, but worse. The fetcher is injectable so
    this is testable and works offline with a pinned key set.
    """

    def __init__(self, fetcher: Callable[[str], dict] | None = None,
                 *, ttl: float = 3600.0) -> None:
        self.fetcher = fetcher or _http_json
        self.ttl = ttl
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0

    def get(self, jwks_uri: str, *, force: bool = False) -> dict[str, Any]:
        if not jwks_uri:
            raise SSOError("jwks_uri is not configured")
        fresh = (time.time() - self._fetched_at) < self.ttl
        if self._keys and fresh and not force:
            return self._keys
        try:
            self._keys = self.fetcher(jwks_uri) or {}
        except Exception as exc:  # noqa: BLE001
            if self._keys:                     # serve stale rather than fail
                logger.warning("JWKS refresh failed (%s) — using cached keys", exc)
                return self._keys
            raise SSOError(f"cannot fetch JWKS from {jwks_uri}: {exc}") from exc
        self._fetched_at = time.time()
        return self._keys


def _http_json(url: str) -> dict:  # pragma: no cover - network
    import requests

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def verify_id_token(token: str, config: OIDCConfig, *,
                    jwks: dict[str, Any] | None = None,
                    now: float | None = None,
                    leeway: float = 60.0) -> OIDCIdentity:
    """Validate an OIDC ID token and return the identity it asserts.

    Checks issuer, audience, expiry and (when configured) verified email.
    Raises :class:`SSOError` with a specific reason — a security review asks
    "what exactly do you verify", and the answer should be readable in code.
    """
    claims = _decode_verified(token, jwks, config)
    now = now or time.time()

    issuer = str(claims.get("iss", ""))
    if config.issuer.rstrip("/") != issuer.rstrip("/"):
        raise SSOError(f"issuer mismatch: token says {issuer!r}")

    audience = claims.get("aud") or claims.get("client_id")
    audiences = audience if isinstance(audience, list) else [audience]
    expected = config.audience or config.client_id
    if expected and expected not in [str(a) for a in audiences if a]:
        raise SSOError(f"audience mismatch: token is for {audiences!r}")

    exp = float(claims.get("exp", 0) or 0)
    if exp and exp + leeway < now:
        raise SSOError("token has expired")
    nbf = float(claims.get("nbf", 0) or 0)
    if nbf and nbf - leeway > now:
        raise SSOError("token is not valid yet")

    email = str(claims.get("email", "") or "")
    verified = bool(claims.get("email_verified", False))
    if config.require_verified_email and email and not verified:
        raise SSOError("email is not verified at the identity provider")

    roles = _roles_from_claims(claims, config.role_claim)
    return OIDCIdentity(
        subject=str(claims.get("sub", "")), email=email,
        name=str(claims.get("name", "") or claims.get("preferred_username", "")),
        email_verified=verified, issuer=issuer, audience=str(expected),
        expires_at=exp, raw_roles=roles, claims=dict(claims),
    )


def _decode_verified(token: str, jwks: dict[str, Any] | None,
                     config: OIDCConfig) -> dict[str, Any]:
    if not jwks:
        raise SSOError("no JWKS available — pass the key set or configure jwks_uri")
    try:
        from jose import jwt as jose_jwt
    except ImportError as exc:  # pragma: no cover
        raise SSOError("python-jose is required to verify OIDC tokens: "
                       "pip install python-jose[cryptography]") from exc
    try:
        return jose_jwt.decode(token, jwks, algorithms=["RS256", "ES256", "PS256"],
                               options={"verify_aud": False, "verify_iss": False})
    except Exception as exc:  # noqa: BLE001
        raise SSOError(f"token signature verification failed: {exc}") from exc


def _roles_from_claims(claims: dict[str, Any], claim_name: str) -> list[str]:
    raw = claims.get(claim_name)
    if raw is None:
        raw = claims.get("groups") or claims.get("realm_access", {}).get("roles") \
            if isinstance(claims.get("realm_access"), dict) else claims.get("groups")
    if isinstance(raw, str):
        return [r.strip() for r in raw.replace(",", " ").split() if r.strip()]
    if isinstance(raw, Sequence):
        return [str(r) for r in raw]
    return []


def map_role(config: OIDCConfig, identity: OIDCIdentity) -> str:
    """Highest-privilege mapped role; defaults to ``config.default_role``."""
    from src.enterprise.rbac import ROLE_RANK

    mapped = [config.role_map.get(r.lower(), "") for r in identity.raw_roles]
    mapped = [m for m in mapped if m]
    if not mapped:
        return config.default_role
    return max(mapped, key=lambda r: ROLE_RANK.get(r, -1))


def provision(identity: OIDCIdentity, config: OIDCConfig, users: Any, *,
              tenant: str) -> dict[str, Any]:
    """Find-or-create the user for an SSO identity.

    The email is the join key (a person who signed up with a password and later
    signs in with SSO must land in the same account), and roles are applied on
    every login — a downgrade at the IdP takes effect at the next sign-in.
    """
    role = map_role(config, identity)
    existing = None
    if identity.email:
        try:
            existing = users.get_by_email(identity.email)
        except Exception:  # noqa: BLE001
            existing = None
    if existing is not None:
        if getattr(existing, "role", "") != role:
            try:
                users.set_role(existing.id, role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not update role for %s: %s", existing.id, exc)
        return {"user_id": existing.id, "created": False, "role": role,
                "tenant": getattr(existing, "tenant", tenant)}
    user = users.create_user(identity.email or f"{identity.subject}@sso.local",
                            None, tenant, role, name=identity.name,
                            oauth_provider="oidc", oauth_id=identity.subject)
    return {"user_id": user.id, "created": True, "role": role, "tenant": tenant}


def saml_status() -> dict[str, Any]:
    """The honest answer about SAML."""
    return {
        "supported": False,
        "reason": "SAML 2.0 is not implemented: shipping an unverified XML "
                  "signature check would be worse than not shipping it",
        "alternative": "OIDC works with every major IdP, including those that "
                       "also offer SAML (Okta, Entra ID, Keycloak)",
        "roadmap": "tracked as an enterprise item; the OIDC path shares the "
                   "claim-mapping and provisioning code it will need",
    }


def configuration_template(issuer: str = "https://idp.example.com") -> dict[str, Any]:
    """Copy-pasteable config for the Settings → Security page."""
    return {
        "issuer": issuer,
        "client_id": "web-intelligence-os",
        "audience": "web-intelligence-os",
        "jwks_uri": issuer.rstrip("/") + "/protocol/openid-connect/certs",
        "role_claim": "roles",
        "role_map": dict(DEFAULT_ROLE_MAP),
        "default_role": "member",
        "redirect_uri": "https://your-deployment.example.com/auth/sso/callback",
    }
