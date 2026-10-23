"""Bounded, privacy-conscious marketplace preview helpers.

These heuristics suppress common direct identifiers; they are not a guarantee
that arbitrary source text has been fully de-identified.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

PREVIEW_RETENTION_SECONDS = 30 * 24 * 60 * 60
PREVIEW_MAX_ROWS = 500
PREVIEW_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
JOB_PREVIEW_KEY = "wcp:v1:job:{job_id}:marketplace-preview:{digest}"
LISTING_PREVIEW_KEY = "mkt:preview:{dataset_id}"

_PREVIEW_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PREVIEW_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PREVIEW_PHONE_RE = re.compile(r"(?<![\w])\+?(?:\d[\s().-]*){9,14}\d(?!\w)")
_PREVIEW_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PREVIEW_CREDENTIAL_RE = re.compile(
    r"(?i)\b((?:[a-z0-9]*(?:token|secret|password|passwd|authorization|cookie|"
    r"session(?:id)?|jwt|email|phone|mobile|ssn|ipaddress))|"
    r"api[_-]?key|client[_-]?key|private[_-]?key|auth)\s*=\s*([^&#\s]+)")
_PREVIEW_PRIVATE_FIELDS = {
    "ip", "ipaddress", "html", "cookie", "cookies", "authorization",
    "password", "secret", "token", "apikey", "credential", "credentials",
    "ssn", "socialsecuritynumber", "email", "phone", "mobile", "telephone",
    "jwt", "bearer", "key",
}
_PREVIEW_MAX_FIELDS = 40
_PREVIEW_MAX_LIST_ITEMS = 20
_PREVIEW_MAX_VALUE_CHARS = 1000
_PREVIEW_MAX_DEPTH = 4


def _preview_private_field(name: Any) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    tokens = re.findall(r"[a-z0-9]+", separated.casefold())
    return any(token in _PREVIEW_PRIVATE_FIELDS for token in tokens)


def _redact_preview_text(value: str) -> str:
    value = _PREVIEW_EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = _PREVIEW_SSN_RE.sub("[REDACTED_SSN]", value)
    value = _PREVIEW_PHONE_RE.sub("[REDACTED_PHONE]", value)
    value = _PREVIEW_IPV4_RE.sub("[REDACTED_IP]", value)
    return _PREVIEW_CREDENTIAL_RE.sub(r"\1=[REDACTED]", value)


def _sanitize_preview_value(value: Any, depth: int = 0) -> Any:
    if depth > _PREVIEW_MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        safe = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _PREVIEW_MAX_FIELDS:
                break
            if not _preview_private_field(key):
                safe[str(key)[:100]] = _sanitize_preview_value(item, depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [_sanitize_preview_value(item, depth + 1)
                for item in value[:_PREVIEW_MAX_LIST_ITEMS]]
    if isinstance(value, str):
        return _redact_preview_text(value)[:_PREVIEW_MAX_VALUE_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_preview_text(str(value))[:_PREVIEW_MAX_VALUE_CHARS]


def sanitize_preview_records(records: Iterable[dict], rows: int = PREVIEW_MAX_ROWS
                             ) -> list[dict]:
    """Redact direct identifiers and constrain row and serialized-payload size."""
    row_limit = max(0, min(int(rows), PREVIEW_MAX_ROWS))
    preview = []
    payload_bytes = 2  # JSON list delimiters
    for rec in records:
        if len(preview) >= row_limit:
            break
        if not isinstance(rec, dict):
            continue
        raw_text = rec.get("content")
        if raw_text is None:
            raw_text = rec.get("text", "")
        safe_text = _sanitize_preview_value(raw_text)
        if not isinstance(safe_text, str):
            safe_text = json.dumps(safe_text, ensure_ascii=False, default=str)
        safe = _sanitize_preview_value(rec)
        if not isinstance(safe, dict):
            continue
        safe.pop("content", None)
        safe.pop("text", None)
        safe["content"] = safe_text[:280] + ("…" if len(safe_text) > 280 else "")
        encoded_size = len(json.dumps(
            safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        additional_bytes = encoded_size + (1 if preview else 0)
        if payload_bytes + additional_bytes > PREVIEW_MAX_PAYLOAD_BYTES:
            break
        preview.append(safe)
        payload_bytes += additional_bytes
    return preview
