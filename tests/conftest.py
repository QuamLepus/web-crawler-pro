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
# Force offline mode: unit tests must never touch the real network even when
# fetcher dependencies (e.g. curl_cffi) happen to be installed in CI images.
os.environ.setdefault("WCP_OFFLINE", "1")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_environment():
    """Guard against cross-test 'environment pollution'.

    Snapshots ``os.environ`` before each test and restores it afterwards, so
    a test that sets variables like ``FASTTEXT_MODEL``, ``WCP_OFFLINE`` or any
    other env var can never leak state into subsequent tests (this was the
    root cause of ``test_pii_redact_fallback`` failing only in full-suite
    runs). Also removes stray model files a test may have created in CWD.
    """
    snapshot = os.environ.copy()
    cwd_before = {p for p in os.listdir(".") if p.endswith(".bin")}
    yield
    # Restore environment exactly as it was.
    for key in list(os.environ.keys()):
        if key not in snapshot:
            del os.environ[key]
    os.environ.update(snapshot)
    # Clean up any fastText-style model artifacts dropped into CWD.
    for path in os.listdir("."):
        if path.endswith(".bin") and path not in cwd_before:
            try:
                os.remove(path)
            except OSError:
                pass
