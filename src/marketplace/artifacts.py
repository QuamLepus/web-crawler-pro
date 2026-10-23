"""Bounded, tenant-isolated object storage for complete crawl result samples.

Artifact persistence is opt-in. The object key is namespaced by a one-way
hash of the tenant ID and the crawl job ID; the payload is canonical JSONL and
its digest is calculated from the exact bytes written and read back from MinIO.
This module does not issue buyer download URLs or alter marketplace manifests.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

MAX_MARKETPLACE_ARTIFACT_BYTES = 10 * 1024 * 1024
ARTIFACT_RETENTION_DAYS = 30
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ArtifactError(ValueError):
    """Artifact could not be safely serialized, stored, or verified."""


class ArtifactRejected(ArtifactError):
    """Artifact input failed a deterministic policy or validation check."""


class ArtifactUnavailable(ArtifactError):
    """Object storage could not accept or verify an artifact."""


@dataclass(frozen=True)
class ArtifactMetadata:
    uri: str
    object_key: str
    sha256: str
    size_bytes: int
    record_count: int
    format: str = "jsonl"
    expires_at: str = ""


def canonical_jsonl(records: Iterable[dict[str, Any]], *,
                    max_bytes: int = MAX_MARKETPLACE_ARTIFACT_BYTES) -> tuple[bytes, str, int]:
    """Serialize records deterministically within a strict byte limit.

    Rows are sorted by URL and JSON keys are sorted. The returned SHA-256 is
    over the complete JSONL byte stream, including its final newline.
    """
    rows = sorted(records, key=lambda row: str(row.get("url", "")))
    output = io.BytesIO()
    index = 0
    while index < len(rows):
        url = str(rows[index].get("url", ""))
        end = index + 1
        while end < len(rows) and str(rows[end].get("url", "")) == url:
            end += 1
        lines: list[bytes] = []
        group_size = 0
        for row in rows[index:end]:
            try:
                line = (json.dumps(row, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), default=str) + "\n").encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ArtifactRejected("crawl records could not be serialized") from exc
            group_size += len(line)
            if output.tell() + group_size > max_bytes:
                raise ArtifactRejected("crawl artifact exceeds the configured byte limit")
            lines.append(line)
        for line in sorted(lines):
            output.write(line)
        index = end
    payload = output.getvalue()
    if not payload:
        raise ArtifactRejected("empty crawl artifacts are not stored")
    return payload, hashlib.sha256(payload).hexdigest(), len(rows)


class MinioMarketplaceArtifactStore:
    """Write verified artifacts to a dedicated MinIO/S3-compatible bucket."""

    def __init__(self, *, client=None, bucket: str | None = None) -> None:
        self.bucket = bucket or os.getenv(
            "MARKETPLACE_ARTIFACT_BUCKET", "wcp-marketplace-artifacts")
        self._client_override = client
        self._bucket_ready = False

    @property
    def client(self):
        if self._client_override is not None:
            return self._client_override
        endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
        access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
        secret_key = os.getenv("MINIO_SECRET_KEY", "")
        if not endpoint or not access_key or not secret_key:
            raise ArtifactUnavailable("marketplace artifact storage is not configured")
        from minio import Minio

        secure = os.getenv("MINIO_SECURE", "false").strip().lower() in {
            "1", "true", "yes", "on"}
        self._client_override = Minio(endpoint, access_key=access_key,
                                      secret_key=secret_key, secure=secure)
        return self._client_override

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        from minio.commonconfig import Filter
        from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

        client = self.client
        if not client.bucket_exists(self.bucket):
            client.make_bucket(self.bucket)
        # This is a dedicated marketplace-artifact bucket. The lifecycle rule
        # bounds physical retention; access paths also enforce expires_at.
        lifecycle_rule_id = "wcp-marketplace-artifacts-expire-30d"
        current = client.get_bucket_lifecycle(self.bucket)
        rules = [rule for rule in (current.rules if current else [])
                 if rule.rule_id != lifecycle_rule_id]
        rules.append(Rule(
            rule_id=lifecycle_rule_id, status="Enabled",
            expiration=Expiration(days=ARTIFACT_RETENTION_DAYS),
            rule_filter=Filter(prefix="marketplace/tenants/")))
        client.set_bucket_lifecycle(self.bucket, LifecycleConfig(rules))
        self._bucket_ready = True

    def put_verified(self, tenant: str, job_id: str, payload: bytes,
                     expected_sha256: str) -> ArtifactMetadata:
        """Upload and read back bytes, rejecting storage that changes content."""
        if not tenant or not JOB_ID_PATTERN.fullmatch(job_id):
            raise ArtifactRejected("a valid tenant and crawl job ID are required")
        if not payload or len(payload) > MAX_MARKETPLACE_ARTIFACT_BYTES:
            raise ArtifactRejected("crawl artifact size is outside the allowed bound")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ArtifactRejected("crawl artifact digest does not match its payload")

        tenant_key = hashlib.sha256(tenant.encode("utf-8")).hexdigest()
        object_key = (f"marketplace/tenants/{tenant_key}/jobs/{job_id}/"
                      f"{expected_sha256}/dataset.jsonl")
        client = self.client
        self._ensure_bucket()
        client.put_object(self.bucket, object_key, io.BytesIO(payload), len(payload),
                          content_type="application/x-ndjson")
        response = None
        try:
            response = client.get_object(self.bucket, object_key)
            stored_digest = hashlib.sha256(response.read()).hexdigest()
            if stored_digest != expected_sha256:
                raise ArtifactUnavailable("stored crawl artifact failed digest verification")
        except Exception as exc:
            try:
                client.remove_object(self.bucket, object_key)
            except Exception:
                pass  # bucket lifecycle still bounds any orphaned failed upload
            if isinstance(exc, ArtifactError):
                raise
            raise ArtifactUnavailable("stored crawl artifact could not be verified") from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

        expires_at = (datetime.now(timezone.utc) +
                      timedelta(days=ARTIFACT_RETENTION_DAYS)).isoformat()
        return ArtifactMetadata(
            uri=f"s3://{self.bucket}/{object_key}", object_key=object_key,
            sha256=stored_digest, size_bytes=len(payload),
            record_count=payload.count(b"\n"), expires_at=expires_at)
