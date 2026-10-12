"""Shared pytest configuration.

``src.gateway.auth.tokens`` now *requires* ``JWT_SECRET`` from the environment
(no hardcoded fallback — see security audit). Tests must run with a
deterministic throwaway signing key so token round-trips are reproducible.

These values are set before any test module imports the gateway/auth stack,
and they are test-only secrets — never use them outside the test suite.
"""

import os

os.environ.setdefault("JWT_SECRET", "test-only-secret-do-not-use-in-production")
os.environ.setdefault("JWT_OLD_SECRETS", "")
