"""Deterministic provenance passports and dataset manifest signing helpers.

The helpers deliberately record unknown licensing as ``unknown``; they never
infer permission from a successful fetch. HMAC signatures are integrity checks,
not proof of legal rights or identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Iterable


def content_sha256(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def data_passport(*, source_url: str, content: str | bytes,
                  http_status: int | None = None, robots_verdict: str = "unknown",
                  license: str = "unknown", extraction_method: str = "unknown",
                  extractor_version: str | None = None,
                  model_id: str | None = None, prompt_hash: str | None = None,
                  confidence: float | None = None,
                  transformations: Iterable[str] = (),
                  fetched_at: datetime | None = None) -> dict[str, Any]:
    """Build a JSON-serializable, auditable provenance record."""
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return {
        "source_url": source_url,
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "http_status": http_status,
        "robots_verdict": robots_verdict,
        "license": license,
        "content_sha256": content_sha256(content),
        "extraction_method": extraction_method,
        "extractor_version": extractor_version,
        "model_id": model_id,
        "prompt_hash": prompt_hash,
        "confidence": confidence,
        "transformations": list(transformations),
    }


def signed_manifest(records: Iterable[dict[str, Any]], *, dataset_id: str,
                    signing_key: str | bytes) -> dict[str, Any]:
    """Return canonical manifest with HMAC-SHA256 signature."""
    key = signing_key.encode() if isinstance(signing_key, str) else signing_key
    if not key:
        raise ValueError("signing_key must not be empty")
    body = {"dataset_id": dataset_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "records": list(records)}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    signature = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
    return {**body, "signature": {"algorithm": "HMAC-SHA256", "value": signature}}
