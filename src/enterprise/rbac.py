"""Role-based access control — one matrix, enforced everywhere.

Product story: an organisation has owners, admins, members and viewers, and a
viewer must not be able to start a crawl or read an invoice. Engineering story:
permission strings checked by a single function, so an endpoint cannot forget
to check (the API dependency refuses) and a role change takes effect instantly
because nothing is duplicated.

Deliberately *not* hierarchical beyond a rank shortcut: an explicit matrix is
easier to review in a security assessment than a lattice of inheritance rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

ROLES = ("viewer", "member", "admin", "owner")
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}

PERMISSIONS: dict[str, str] = {
    # -- reading ------------------------------------------------------------
    "dashboard:read": "See the dashboard, job list and system health",
    "datasets:read": "Read datasets, versions, diffs and exports",
    "graph:read": "Query the knowledge graph",
    "provenance:read": "Read provenance records and lineage",
    "search:read": "Query the own search index",
    "audit:read": "Read the audit log",
    "billing:read": "Read usage, invoices and plan",
    # -- doing --------------------------------------------------------------
    "jobs:create": "Submit crawl and discovery jobs",
    "jobs:cancel": "Cancel running jobs",
    "ask:create": "Run budgeted questions (/ask) and research reports",
    "watch:write": "Create, edit and run monitors",
    "datasets:write": "Create datasets and append versions",
    "recipes:write": "Install and edit extraction recipes",
    "workflows:write": "Create, edit and run workflows",
    "graph:write": "Write entities and relations",
    "keys:create": "Mint API keys",
    "webhooks:write": "Manage webhook subscriptions",
    # -- governing ----------------------------------------------------------
    "members:manage": "Invite, remove and re-role team members",
    "billing:manage": "Change plan, payment method and quotas",
    "org:manage": "Rename the organisation, rotate JWT keys, set SSO",
    "compliance:export": "Export provenance manifests and audit reports",
    "retention:manage": "Set retention and run erasure requests",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({
        "dashboard:read", "datasets:read", "graph:read", "provenance:read",
        "search:read", "billing:read",
    }),
    "member": frozenset({
        "dashboard:read", "datasets:read", "datasets:write", "graph:read",
        "graph:write", "provenance:read", "search:read", "billing:read",
        "jobs:create", "jobs:cancel", "ask:create", "watch:write",
        "workflows:write", "webhooks:write",
    }),
    "admin": frozenset(set(PERMISSIONS) - {"billing:manage", "org:manage"}),
    "owner": frozenset(PERMISSIONS),
}


@dataclass
class AccessDecision:
    allowed: bool
    role: str
    permission: str
    reason: str = ""

    def __bool__(self) -> bool:  # `if decision:` reads well at call sites
        return self.allowed


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role if role in ROLE_PERMISSIONS else "viewer",
                                ROLE_PERMISSIONS["viewer"])


def can(role: str, permission: str) -> AccessDecision:
    """The single check every endpoint uses."""
    if permission not in PERMISSIONS:
        return AccessDecision(False, role, permission,
                              f"unknown permission {permission!r}")
    granted = permissions_for(role)
    if permission in granted:
        return AccessDecision(True, role, permission, "granted by role")
    return AccessDecision(
        False, role, permission,
        f"role {role!r} lacks {permission!r} — required: "
        + ", ".join(sorted(r for r in ROLES if permission in permissions_for(r)))
        or "no role grants this",
    )


def can_all(role: str, required: Iterable[str]) -> tuple[bool, list[str]]:
    missing = [p for p in required if not can(role, p)]
    return not missing, missing


def highest_role(roles: Iterable[str]) -> str:
    return max(roles, key=lambda r: ROLE_RANK.get(r, -1), default="viewer")


def matrix() -> list[dict]:
    """The matrix as rows — this is what the Settings → Security page renders."""
    return [
        {
            "permission": name,
            "description": PERMISSIONS[name],
            "roles": {role: name in permissions_for(role) for role in ROLES},
        }
        for name in sorted(PERMISSIONS)
    ]
