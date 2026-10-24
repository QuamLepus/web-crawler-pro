"""Backend-neutral data contracts and fail-closed admission policy.

These types do not provision, attest, or execute a runtime. Integrations must
supply independently verified backend and artifact-review state before use.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeBackend(StrEnum):
    WASM_WASI = "wasm_wasi"
    SANDBOXED_KUBERNETES = "sandboxed_kubernetes"
    MICROVM = "microvm"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactAttestation(StrictContract):
    """Claim binding an immutable artifact to its review/provenance record.

    `signature` is opaque data; constructing this object does not verify it.
    """

    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    backend: RuntimeBackend
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_id: str = Field(min_length=1, max_length=160)
    signer_id: str = Field(min_length=1, max_length=160)
    signature: str = Field(min_length=1, max_length=4096)
    created_at: datetime


class BackendReadiness(StrictContract):
    backend: RuntimeBackend
    enabled: bool
    conformance_passed: bool
    policy_revision: str = Field(min_length=1, max_length=80)


class AdmissionRequest(StrictContract):
    run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    backend: RuntimeBackend
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_bytes: int = Field(ge=0, le=1_048_576)
    deadline_seconds: int = Field(ge=1, le=3600)


class AdmissionDecision(StrictContract):
    admitted: bool
    reason: Literal[
        "admitted", "backend_not_provisioned", "backend_not_conformant",
        "artifact_not_approved", "artifact_mismatch", "agent_version_mismatch",
        "resource_limit_exceeded",
    ]
    backend: RuntimeBackend
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_revision: str


class RuntimeAuditEvent(StrictContract):
    event_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    event_type: Literal[
        "admission_denied", "run_queued", "run_started", "run_completed",
        "run_failed", "run_cancelled", "artifact_revoked", "sandbox_cleanup",
    ]
    run_id: str | None = Field(default=None, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    backend: RuntimeBackend
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_revision: str = Field(min_length=1, max_length=80)
    reason_code: str = Field(min_length=1, max_length=80)

    @field_validator("artifact_sha256")
    @classmethod
    def _lowercase_digest(cls, value: str) -> str:
        if value != value.lower():
            raise ValueError("digest must be lowercase hexadecimal")
        return value


def decide_admission(
    request: AdmissionRequest,
    *,
    attestation: ArtifactAttestation | None,
    review_approved: bool,
    backend_readiness: BackendReadiness | None,
    maximum_input_bytes: int = 1_048_576,
    maximum_deadline_seconds: int = 3600,
) -> AdmissionDecision:
    """Evaluate explicit pinning and readiness; never choose another backend.

    The `review_approved` and readiness arguments must come from trusted,
    independently verified control-plane sources. This pure function does not
    validate signatures, launch a sandbox, or prove a backend is secure.
    """
    readiness = backend_readiness
    policy_revision = readiness.policy_revision if readiness else "unavailable"
    base = {"backend": request.backend, "artifact_sha256": request.sha256,
            "policy_revision": policy_revision}

    def deny(reason: str) -> AdmissionDecision:
        return AdmissionDecision(admitted=False, reason=reason, **base)

    if (readiness is None or readiness.backend != request.backend
            or not readiness.enabled):
        return deny("backend_not_provisioned")
    if not readiness.conformance_passed:
        return deny("backend_not_conformant")
    if attestation is None or not review_approved:
        return deny("artifact_not_approved")
    if (attestation.backend != request.backend
            or attestation.sha256 != request.sha256):
        return deny("artifact_mismatch")
    if attestation.agent_id != request.agent_id or attestation.version != request.version:
        return deny("agent_version_mismatch")
    if (request.input_bytes > maximum_input_bytes
            or request.deadline_seconds > maximum_deadline_seconds):
        return deny("resource_limit_exceeded")
    return AdmissionDecision(admitted=True, reason="admitted", **base)
