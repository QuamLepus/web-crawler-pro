"""Pure contract tests: no sandbox or publisher code is provisioned or run."""
import base64
import hashlib
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from src.agent_marketplace.runtime_contract import (
    AdmissionRequest,
    ArtifactAttestation,
    BackendReadiness,
    RuntimeAuditEvent,
    RuntimeBackend,
    attestation_signing_payload,
    decide_admission,
    verify_artifact_bytes,
    verify_attestation_signature,
)

ARTIFACT_BYTES = b"minimal inert wasm artifact fixture"
DIGEST = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
NOW = datetime.now(UTC)
_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY_PEM = _PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo)
_TRUSTED_KEYS = {"review-key-1": _PUBLIC_KEY_PEM}


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
        "key_id": "review-key-1", "signature_algorithm": "ed25519",
        "signature": "pending", "created_at": NOW,
    }
    data.update(changes)
    attestation = ArtifactAttestation(**data)
    if "signature" not in changes:
        signature = base64.urlsafe_b64encode(
            _PRIVATE_KEY.sign(attestation_signing_payload(attestation))
        ).decode().rstrip("=")
        attestation = attestation.model_copy(update={"signature": signature})
    return attestation


def _ready(**changes):
    data = {"backend": RuntimeBackend.WASM_WASI, "enabled": True,
            "conformance_passed": True, "policy_revision": "policy-v1"}
    data.update(changes)
    return BackendReadiness(**data)


_DEFAULT = object()


def _decide(request=None, *, attestation=_DEFAULT, approved=True,
            readiness=_DEFAULT, trusted_keys=_TRUSTED_KEYS,
            artifact_bytes=ARTIFACT_BYTES):
    if attestation is _DEFAULT:
        attestation = _attestation()
    if readiness is _DEFAULT:
        readiness = _ready()
    return decide_admission(
        request or _request(), attestation=attestation,
        artifact_bytes=artifact_bytes, trusted_public_keys=trusted_keys,
        review_approved=approved, backend_readiness=readiness)


def test_admission_requires_all_pins_review_and_backend_gates():
    assert _decide(attestation=_attestation(), readiness=_ready()).admitted
    assert _decide(approved=False).reason == "artifact_not_approved"
    assert _decide(attestation=None).reason == "artifact_not_approved"
    assert _decide(attestation=_attestation(signature="bad")).reason == "artifact_signature_invalid"
    assert _decide(trusted_keys={}).reason == "artifact_signature_invalid"
    assert _decide(artifact_bytes=None).reason == "artifact_mismatch"
    assert _decide(artifact_bytes=ARTIFACT_BYTES + b"tampered").reason == "artifact_mismatch"
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
        request, attestation=_attestation(), artifact_bytes=ARTIFACT_BYTES,
        trusted_public_keys=_TRUSTED_KEYS, review_approved=True,
        backend_readiness=_ready(), maximum_input_bytes=256,
        maximum_deadline_seconds=20)
    assert result.reason == "resource_limit_exceeded"

    with pytest.raises(ValidationError):
        _request(input_bytes=1_048_577)
    with pytest.raises(ValidationError):
        _request(deadline_seconds=3601)


def test_attestation_signature_verifies_claim_and_fails_closed_on_tampering():
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    unsigned = _attestation(signature="pending")
    signature = base64.urlsafe_b64encode(
        private_key.sign(attestation_signing_payload(unsigned))).decode().rstrip("=")
    signed = unsigned.model_copy(update={"signature": signature})

    assert verify_attestation_signature(signed, {"review-key-1": public_pem})
    assert not verify_attestation_signature(signed, {})
    assert not verify_attestation_signature(
        signed.model_copy(update={"sha256": "b" * 64}),
        {"review-key-1": public_pem})
    assert not verify_attestation_signature(
        signed.model_copy(update={"signature": "not-base64%%%"}),
        {"review-key-1": public_pem})


def test_artifact_bytes_are_bounded_and_match_signed_digest():
    artifact = b"minimal inert wasm artifact fixture"
    attestation = _attestation(sha256=hashlib.sha256(artifact).hexdigest())

    assert verify_artifact_bytes(attestation, artifact)
    assert not verify_artifact_bytes(attestation, artifact + b"tampered")
    assert not verify_artifact_bytes(attestation, artifact, maximum_bytes=4)
    assert not verify_artifact_bytes(attestation, bytearray(artifact))


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
