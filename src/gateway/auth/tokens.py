"""JWT + API-key authentication primitives (OAuth2-compatible).

* Access/refresh JWTs signed HS256 with rotation-ready key list
  (``JWT_SECRET`` current, ``JWT_OLD_SECRETS`` comma list for graceful
  rotation without invalidating sessions).
* API keys: ``wcp_<32 hex>`` format, stored **hashed** (sha256) in Redis;
  plaintext shown once at creation. Keys carry scopes + tenant binding and
  are checked by the gateway dependency in ``src.gateway.api``.
* Password hashing via argon2/bcrypt when available, PBKDF2-sha256 fallback
  (constant-time verify) so dev environments work without native deps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALG = "HS256"
ACCESS_TTL = int(os.getenv("JWT_ACCESS_TTL", "3600"))
REFRESH_TTL = int(os.getenv("JWT_REFRESH_TTL", str(86400 * 30)))
ISSUER = "webcrawler-pro"


def _old_secrets() -> list[str]:
    raw = os.getenv("JWT_OLD_SECRETS", "")
    return [s for s in raw.split(",") if s]


# --------------------------------------------------------------------------- #
# Minimal JWT implementation (no external deps; stdlib-only HS256)
# --------------------------------------------------------------------------- #
def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    import base64

    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(msg: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode(), msg, hashlib.sha256).digest()


def create_token(claims: dict[str, Any], *, ttl: int = ACCESS_TTL, secret: str | None = None) -> str:
    now = int(time.time())
    payload = {**claims, "iss": ISSUER, "iat": now, "exp": now + ttl}
    header = {"alg": JWT_ALG, "typ": "JWT"}
    seg = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
          f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = _sign(seg.encode(), secret or JWT_SECRET)
    return f"{seg}.{_b64url(sig)}"


class TokenExpired(Exception):
    pass


class TokenInvalid(Exception):
    pass


def decode_token(token: str) -> dict[str, Any]:
    try:
        h_seg, p_seg, s_seg = token.split(".")
    except ValueError as exc:
        raise TokenInvalid("malformed jwt") from exc
    signing_input = f"{h_seg}.{p_seg}".encode()
    provided = _b64url_decode(s_seg)
    for secret in [JWT_SECRET, *_old_secrets()]:
        if hmac.compare_digest(_sign(signing_input, secret), provided):
            break
    else:
        raise TokenInvalid("bad signature")
    payload = json.loads(_b64url_decode(p_seg))
    if payload.get("exp", 0) < time.time():
        raise TokenExpired("token expired")
    if payload.get("iss") != ISSUER:
        raise TokenInvalid("bad issuer")
    return payload


def issue_session(user_id: str, tenant: str, role: str) -> dict[str, Any]:
    """Return access+refresh pair used by /auth/login."""
    return {
        "access_token": create_token({"sub": user_id, "tenant": tenant, "role": role, "typ": "access"}),
        "refresh_token": create_token(
            {"sub": user_id, "tenant": tenant, "role": role, "typ": "refresh"}, ttl=REFRESH_TTL
        ),
        "expires_in": ACCESS_TTL,
        "token_type": "Bearer",
    }


def refresh_access(refresh_token: str) -> dict[str, Any]:
    claims = decode_token(refresh_token)
    if claims.get("typ") != "refresh":
        raise TokenInvalid("not a refresh token")
    return {
        "access_token": create_token({k: claims[k] for k in ("sub", "tenant", "role")} | {"typ": "access"}),
        "expires_in": ACCESS_TTL,
        "token_type": "Bearer",
    }


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    try:
        from argon2 import PasswordHasher

        return "argon2:" + PasswordHasher().hash(password)
    except ImportError:
        pass
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 390_000)
    return f"pbkdf2${_b64url(salt)}${_b64url(dk)}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("argon2:"):
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError

            try:
                PasswordHasher().verify(stored[7:], password)
                return True
            except VerifyMismatchError:
                return False
        except ImportError:
            return False
    try:
        scheme, salt_b64, hash_b64 = stored.split("$", 2)
        assert scheme == "pbkdf2"
        salt = _b64url_decode(salt_b64)
        expected = _b64url_decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 390_000)
        return hmac.compare_digest(dk, expected)
    except (ValueError, AssertionError):
        return False


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
@dataclass
class ApiKey:
    key_id: str
    tenant: str
    name: str
    scopes: list[str] = field(default_factory=lambda: ["read", "crawl"])
    created_at: float = field(default_factory=time.time)
    last_used: float | None = None
    revoked: bool = False


def generate_api_key() -> tuple[str, str]:
    """Return (plaintext_key, key_id). Plaintext is never stored."""
    raw = secrets.token_hex(16)
    plaintext = f"wcp_{raw}"
    key_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return plaintext, key_id


def hash_api_key(plaintext: str) -> str:
    body = plaintext.removeprefix("wcp_")
    return hashlib.sha256(body.encode()).hexdigest()[:16]


class ApiKeyStore:
    """Redis-backed key store: hash->meta plus per-tenant index."""

    def __init__(self, redis_client) -> None:
        self.r = redis_client

    def create(self, tenant: str, name: str, scopes: list[str] | None = None) -> tuple[ApiKey, str]:
        plaintext, key_id = generate_api_key()
        meta = ApiKey(key_id=key_id, tenant=tenant, name=name,
                      scopes=scopes or ["read", "crawl"])
        self.r.hset(f"apikey:{key_id}", mapping={
            "tenant": tenant, "name": name,
            "scopes": json.dumps(meta.scopes), "created_at": meta.created_at,
            "revoked": "0",
        })
        self.r.sadd(f"apikeys:tenant:{tenant}", key_id)
        return meta, plaintext

    def lookup(self, plaintext: str) -> ApiKey | None:
        key_id = hash_api_key(plaintext)
        raw = self.r.hgetall(f"apikey:{key_id}")
        if not raw:
            return None
        d = {k.decode() if isinstance(k, bytes) else k:
             v.decode() if isinstance(v, bytes) else v for k, v in raw.items()}
        if d.get("revoked") == "1":
            return None
        ak = ApiKey(key_id=key_id, tenant=d["tenant"], name=d["name"],
                    scopes=json.loads(d["scopes"]), created_at=float(d["created_at"]))
        self.r.hset(f"apikey:{key_id}", "last_used", time.time())
        return ak

    def revoke(self, tenant: str, key_id: str) -> bool:
        if not self.r.exists(f"apikey:{key_id}"):
            return False
        self.r.hset(f"apikey:{key_id}", "revoked", "1")
        return True

    def list_for_tenant(self, tenant: str) -> list[dict]:
        out = []
        for kid in self.r.smembers(f"apikeys:tenant:{tenant}"):
            kid_s = kid.decode() if isinstance(kid, bytes) else kid
            raw = self.r.hgetall(f"apikey:{kid_s}")
            if raw:
                d = {k.decode() if isinstance(k, bytes) else k:
                     v.decode() if isinstance(v, bytes) else v for k, v in raw.items()}
                d["key_id"] = kid_s
                d["scopes"] = json.loads(d.get("scopes", "[]"))
                d["revoked"] = d.get("revoked") == "1"
                out.append(d)
        return out
