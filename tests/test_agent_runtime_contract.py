"""Pure contract tests: no sandbox or publisher code is provisioned or run."""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.agent_marketplace.runtime_contract import (
    AdmissionRequest,
    ArtifactAttestation,
    BackendReadiness,
    RuntimeAuditEvent,
    RuntimeBackend,
    decide_admission,
)

DIGEST = "a" * 64
NOW = datetime.now(UTC)


def _request(**changes):
    data = {
        "run_id": "run-1", "tenant_id": "tenant-1", "agent_id": "agent-1",
        "version": "1.2.3", "backend": RuntimeBackend.WASM_WASI,
        "sha256": DIGEST, "input_bytes": 128, "deadline_seconds": 30,
    }
    data.update(changes)
    return AdmissionRequest(**data)


def _attestation(**changes):
    data = {
        "agent_id": "agent-1", "version": "1.2.3",
        "backend": RuntimeBackend.WASM_WASI, "sha256": DIGEST,
        "provenance_id": "prov-1", "signer_id": "review-service",
        "signature": "opaque-signature", "created_at": NOW,
    }
    data.update(changes)
    return ArtifactAttestation(**data)


def _ready(**changes):
    data = {"backend": RuntimeBackend.WASM_WASI, "enabled": True,
            "conformance_passed": True, "policy_revision": "policy-v1"}
    data.update(changes)
    return BackendReadiness(**data)


_DEFAULT = object()


def _decide(request=None, *, attestation=_DEFAULT, approved=True, readiness=_DEFAULT):
    if attestation is _DEFAULT:
        attestation = _attestation()
    if readiness is _DEFAULT:
        readiness = _ready()
    return decide_admission(
        request or _request(), attestation=attestation,
        review_approved=approved, backend_readiness=readiness)


def test_admission_requires_all_pins_review_and_backend_gates():
    assert _decide(attestation=_attestation(), readiness=_ready()).admitted
    assert _decide(approved=False).reason == "artifact_not_approved"
    assert _decide(attestation=None).reason == "artifact_not_approved"
    assert _decide(readiness=None).reason == "backend_not_provisioned"
    assert _decide(readiness=_ready(enabled=False)).reason == "backend_not_provisioned"
    assert _decide(readiness=_ready(conformance_passed=False)).reason == "backend_not_conformant"


def test_admission_rejects_backend_digest_and_version_mismatch_without_fallback():
    assert _decide(attestation=_attestation(sha256="b" * 64)).reason == "artifact_mismatch"
    assert _decide(attestation=_attestation(
        backend=RuntimeBackend.MICROVM)).reason == "artifact_mismatch"
    assert _decide(request=_request(version="2.0.0")).reason == "agent_version_mismatch"
    # Availability of a different class never substitutes for the pinned class.
    other_backend_ready = _ready(backend=RuntimeBackend.MICROVM)
    assert _decide(readiness=other_backend_ready).reason == "backend_not_provisioned"


def test_admission_enforces_resource_bounds():
    request = _request(input_bytes=512, deadline_seconds=30)
    result = decide_admission(
        request, attestation=_attestation(), review_approved=True,
        backend_readiness=_ready(), maximum_input_bytes=256,
        maximum_deadline_seconds=20)
    assert result.reason == "resource_limit_exceeded"

    with pytest.raises(ValidationError):
        _request(input_bytes=1_048_577)
    with pytest.raises(ValidationError):
        _request(deadline_seconds=3601)


def test_contracts_reject_unknown_fields_and_noncanonical_digest():
    with pytest.raises(ValidationError):
        _request(unreviewed_payload="not allowed")
    with pytest.raises(ValidationError):
        _request(sha256="A" * 64)
    with pytest.raises(ValidationError):
        _attestation(backend="automatic")


def test_audit_event_contains_bounded_metadata_not_input_or_output_payloads():
    event = RuntimeAuditEvent(
        event_id="evt-1", occurred_at=NOW, event_type="admission_denied",
        run_id="run-1", tenant_id="tenant-1", agent_id="agent-1",
        version="1.2.3", backend=RuntimeBackend.WASM_WASI,
        artifact_sha256=DIGEST, policy_revision="policy-v1",
        reason_code="artifact_not_approved")
    assert "input" not in event.model_dump()
    assert "output" not in event.model_dump()
    with pytest.raises(ValidationError):
        RuntimeAuditEvent(**{**event.model_dump(), "input": {"secret": "x"}})
