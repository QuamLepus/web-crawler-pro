"""Enterprise controls: RBAC, audit, SSO, retention, backup, analytics.

Everything here answers a question an enterprise buyer asks during a security
or procurement review, and answers it in code you can read:

* :mod:`src.enterprise.rbac` — one explicit role/permission matrix.
* :mod:`src.enterprise.audit` — append-only, hash-chained action log.
* :mod:`src.enterprise.sso` — OIDC verified properly; SAML declared honestly.
* :mod:`src.enterprise.governance` — retention plans, erasure receipts,
  backup/restore with dry-run, usage analytics.

No certifications are claimed here (SOC 2 / ISO 27001 are audits, not
features) — these are the *controls* such an audit examines.
"""

from src.enterprise.audit import ACTIONS, AuditEntry, AuditLog, action_catalogue
from src.enterprise.governance import (
    ErasurePlan,
    RetentionAction,
    RetentionPolicy,
    apply_erasure,
    apply_retention,
    bump_usage,
    export_tenant,
    plan_erasure,
    plan_retention,
    restore_tenant,
    usage_report,
)
from src.enterprise.rbac import (
    PERMISSIONS,
    ROLE_PERMISSIONS,
    ROLES,
    AccessDecision,
    can,
    can_all,
    highest_role,
    matrix,
    permissions_for,
)
from src.enterprise.sso import (
    OIDCConfig,
    OIDCIdentity,
    JWKSCache,
    SSOError,
    configuration_template,
    map_role,
    provision,
    saml_status,
    verify_id_token,
)

__all__ = [
    "ACTIONS",
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "ROLES",
    "AccessDecision",
    "AuditEntry",
    "AuditLog",
    "ErasurePlan",
    "JWKSCache",
    "OIDCConfig",
    "OIDCIdentity",
    "RetentionAction",
    "RetentionPolicy",
    "SSOError",
    "action_catalogue",
    "apply_erasure",
    "apply_retention",
    "bump_usage",
    "can",
    "can_all",
    "configuration_template",
    "export_tenant",
    "highest_role",
    "map_role",
    "matrix",
    "permissions_for",
    "plan_erasure",
    "plan_retention",
    "provision",
    "restore_tenant",
    "saml_status",
    "usage_report",
    "verify_id_token",
]
