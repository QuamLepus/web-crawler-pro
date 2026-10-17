"""Multi-tenant user & organization store (Redis-backed, pluggable to SQL).

Tenancy model: Organization (tenant) -> Users (roles: owner/admin/member/
viewer) -> isolated namespaces for frontier keys, storage prefixes, quotas.
Every record is keyed by tenant so cross-tenant access is impossible by
construction (keys embed the tenant id).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from src.gateway.auth.tokens import hash_password, verify_password

Role = Literal["owner", "admin", "member", "viewer"]
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


@dataclass
class User:
    id: str
    email: str
    tenant: str
    role: Role
    name: str = ""
    password_hash: str = ""
    oauth_provider: str | None = None      # google | github
    oauth_id: str | None = None
    created_at: float = field(default_factory=time.time)
    disabled: bool = False


@dataclass
class Organization:
    id: str
    name: str
    plan: str = "free"                     # free|starter|pro|enterprise
    stripe_customer_id: str | None = None
    storage_prefix: str = ""               # e.g. s3://lake/tenants/{id}/
    created_at: float = field(default_factory=time.time)


class UserStore:
    """CRUD + auth flows. ``redis_client`` duck-typed; works with fakeredis."""

    def __init__(self, redis_client) -> None:
        self.r = redis_client

    # -- organizations -------------------------------------------------------- #
    def create_org(self, name: str, *, plan: str = "free") -> Organization:
        org_id = uuid.uuid4().hex[:10]
        org = Organization(
            id=org_id, name=name, plan=plan,
            storage_prefix=f"s3://crawler-lake/tenants/{org_id}/",
        )
        self.r.set(f"org:{org_id}", json.dumps(asdict(org)))
        self.r.sadd("orgs", org_id)
        return org

    def get_org(self, org_id: str) -> Organization | None:
        raw = self.r.get(f"org:{org_id}")
        return Organization(**json.loads(raw)) if raw else None

    def set_plan(self, org_id: str, plan: str, stripe_customer_id: str | None = None) -> None:
        org = self.get_org(org_id)
        if not org:
            raise KeyError(org_id)
        org.plan = plan
        if stripe_customer_id:
            org.stripe_customer_id = stripe_customer_id
        self.r.set(f"org:{org_id}", json.dumps(asdict(org)))

    def list_orgs(self) -> list[Organization]:
        out = []
        for oid in self.r.smembers("orgs"):
            oid_s = oid.decode() if isinstance(oid, bytes) else oid
            o = self.get_org(oid_s)
            if o:
                out.append(o)
        return out

    # -- users ------------------------------------------------------------------- #
    def create_user(
        self, email: str, password: str | None, tenant: str, role: Role = "member",
        *, name: str = "", oauth_provider: str | None = None, oauth_id: str | None = None,
    ) -> User:
        if self.r.exists(f"user:email:{email.lower()}"):
            raise ValueError("email already registered")
        if not password and not oauth_provider:
            raise ValueError("password or oauth identity required")
        ph = hash_password(password) if password else ""
        user = User(
            id=uuid.uuid4().hex[:12], email=email.lower(), tenant=tenant, role=role,
            name=name, password_hash=ph, oauth_provider=oauth_provider, oauth_id=oauth_id,
        )
        pipe = self.r.pipeline()
        pipe.set(f"user:{user.id}", json.dumps(asdict(user)))
        pipe.set(f"user:email:{user.email}", user.id)
        pipe.sadd(f"users:tenant:{tenant}", user.id)
        if oauth_provider and oauth_id:
            pipe.set(f"user:oauth:{oauth_provider}:{oauth_id}", user.id)
        pipe.execute()
        return user

    def get_user(self, user_id: str) -> User | None:
        raw = self.r.get(f"user:{user_id}")
        return User(**json.loads(raw)) if raw else None

    def get_by_email(self, email: str) -> User | None:
        uid = self.r.get(f"user:email:{email.lower()}")
        return self.get_user(uid.decode() if isinstance(uid, bytes) else uid) if uid else None

    def get_by_oauth(self, provider: str, oauth_id: str) -> User | None:
        uid = self.r.get(f"user:oauth:{provider}:{oauth_id}")
        if not uid:
            return None
        return self.get_user(uid.decode() if isinstance(uid, bytes) else uid)

    def link_oauth_identity(self, user_id: str, provider: str, oauth_id: str) -> User:
        """Atomically bind a verified external subject to one existing account."""
        user = self.get_user(user_id)
        if user is None:
            raise LookupError("user not found")
        if user.oauth_provider and (user.oauth_provider != provider or user.oauth_id != oauth_id):
            raise ValueError("account already has a different external identity")
        key = f"user:oauth:{provider}:{oauth_id}"
        existing_id = self.r.get(key)
        if existing_id:
            existing_id = existing_id.decode() if isinstance(existing_id, bytes) else existing_id
            if existing_id != user.id:
                raise ValueError("external identity is already linked to another account")
        elif not self.r.set(key, user.id, nx=True):
            raise ValueError("external identity is already linked to another account")
        user.oauth_provider = provider
        user.oauth_id = oauth_id
        self.r.set(f"user:{user.id}", json.dumps(asdict(user)))
        return user

    def authenticate(self, email: str, password: str) -> User | None:
        user = self.get_by_email(email)
        if user is None or user.disabled or not user.password_hash:
            return None
        if not verify_password(password, user.password_hash):
            self._record_failed_login(user)
            return None
        return user

    def _record_failed_login(self, user: User) -> None:
        key = f"logins:failed:{user.id}"
        count = self.r.incr(key)
        self.r.expire(key, 900)
        if count >= 10:  # lockout after 10 failures in 15 minutes
            self.r.set(f"user:{user.id}:locked", "1", ex=3600)

    def is_locked(self, user_id: str) -> bool:
        return bool(self.r.exists(f"user:{user_id}:locked"))

    def change_role(self, actor: User, target_id: str, new_role: Role) -> User:
        """RBAC: only admins/owners, cannot exceed own rank, owners immutable."""
        if ROLE_RANK[actor.role] < ROLE_RANK["admin"]:
            raise PermissionError("requires admin role")
        target = self.get_user(target_id)
        if target is None or target.tenant != actor.tenant:
            raise LookupError("target user not found in tenant")
        if target.role == "owner" or new_role == "owner":
            raise PermissionError("owner transfers are out of band")
        if ROLE_RANK[new_role] > ROLE_RANK[actor.role]:
            raise PermissionError("cannot grant higher role than your own")
        target.role = new_role
        self.r.set(f"user:{target.id}", json.dumps(asdict(target)))
        return target

    def disable_user(self, actor: User, target_id: str) -> bool:
        target = self.get_user(target_id)
        if target is None or target.tenant != actor.tenant:
            return False
        if ROLE_RANK[actor.role] < ROLE_RANK["admin"] or target.role == "owner":
            raise PermissionError("not permitted")
        target.disabled = True
        self.r.set(f"user:{target.id}", json.dumps(asdict(target)))
        return True

    def list_users(self, tenant: str) -> list[User]:
        out = []
        for uid in self.r.smembers(f"users:tenant:{tenant}"):
            uid_s = uid.decode() if isinstance(uid, bytes) else uid
            u = self.get_user(uid_s)
            if u:
                out.append(u)
        return sorted(out, key=lambda x: x.created_at)
