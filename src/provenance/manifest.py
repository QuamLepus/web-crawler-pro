"""Signed dataset manifests — the artefact you hand a buyer or a lawyer.

A manifest answers, for one dataset version, in one file:

* how many records, from how many distinct sources;
* the licence mix, and how much of it is safe to redistribute;
* the robots.txt outcome mix;
* a Merkle root over every record hash, so any single record can be proven to
  belong to the version that was signed;
* the tool and model versions that produced it;
* an HMAC-SHA256 signature over the canonical bytes.

Verification needs no database and no network — just the file, the secret and
the published algorithm. That property is the product: "your data came from
somewhere, and here is the proof", exportable to a customer who will never
run our software.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import Counter
from typing import Any, Iterable, Sequence

from src.provenance.models import (
    REDISTRIBUTABLE,
    ProvenanceRecord,
    canonical_json,
)

MANIFEST_VERSION = 1
ALGORITHM = "HMAC-SHA256/canonical-json/m1"


# --------------------------------------------------------------------------- #
# Merkle tree over record hashes
# --------------------------------------------------------------------------- #
def merkle_root(leaves: Sequence[str]) -> str:
    """RFC-6962-style binary Merkle root over hex digests.

    Leaves are the per-record chain hashes; the root commits to all of them at
    once, so a manifest of a million records still fits in an email while
    every record stays individually provable (via an inclusion proof, see
    :func:`inclusion_proof`).
    """
    if not leaves:
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    level = [hashlib.sha256(leaf.encode()).digest() for leaf in leaves]
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(hashlib.sha256(left + right).digest())
        level = nxt
    return "sha256:" + level[0].hex()


def inclusion_proof(leaves: Sequence[str], index: int) -> list[dict[str, str]]:
    """Sibling hashes needed to prove ``leaves[index]`` is in the tree."""
    if not leaves or index < 0 or index >= len(leaves):
        raise IndexError(index)
    level = [hashlib.sha256(leaf.encode()).digest() for leaf in leaves]
    idx = index
    proof: list[dict[str, str]] = []
    while len(level) > 1:
        sib = idx + 1 if idx % 2 == 0 else idx - 1
        if sib >= len(level):
            sib = idx
        proof.append({"position": "right" if idx % 2 == 0 else "left",
                      "hash": level[sib].hex()})
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(hashlib.sha256(left + right).digest())
        level = nxt
        idx //= 2
    return proof


def verify_inclusion(leaf: str, proof: Sequence[dict[str, str]], root: str) -> bool:
    node = hashlib.sha256(leaf.encode()).digest()
    for step in proof:
        sib = bytes.fromhex(step["hash"])
        node = (hashlib.sha256(node + sib).digest() if step["position"] == "right"
                else hashlib.sha256(sib + node).digest())
    return "sha256:" + node.hex() == root


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def build_manifest(
    *,
    dataset: str,
    version: str,
    records: Iterable[ProvenanceRecord],
    created_by: str = "",
    tenant: str = "",
    created_at: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarise records into a manifest body (unsigned)."""
    recs = list(records)
    licenses = Counter(r.license for r in recs)
    verdicts = Counter(r.robots_verdict for r in recs)
    methods = Counter(r.collection_method for r in recs)
    extractors = Counter(f"{r.extractor}@{r.extractor_version}" if r.extractor
                         else "unknown" for r in recs)
    models = Counter(r.llm_model_id for r in recs if r.llm_model_id)
    hosts = Counter(_host(r.source_url) for r in recs if r.source_url)
    redistributable = sum(n for lic, n in licenses.items() if lic in REDISTRIBUTABLE)
    hits = sum(1 for r in recs)
    fetched = [r.fetched_at for r in recs if r.fetched_at]

    return {
        "manifest_version": MANIFEST_VERSION,
        "algorithm": ALGORITHM,
        "dataset": dataset,
        "version": version,
        "tenant": tenant,
        "created_by": created_by,
        "created_at": round(float(created_at or time.time()), 3),
        "record_count": len(recs),
        "source_count": len(hosts),
        "top_sources": [{"host": h, "records": n} for h, n in hosts.most_common(10)],
        "fetched_range": {
            "from": round(min(fetched), 3) if fetched else None,
            "to": round(max(fetched), 3) if fetched else None,
        },
        "licenses": {
            "counts": dict(sorted(licenses.items())),
            "redistributable_records": redistributable,
            "redistributable_share": round(redistributable / hits, 4) if hits else 0.0,
            "unknown_records": licenses.get("unknown", 0),
        },
        "robots": dict(sorted(verdicts.items())),
        "collection_methods": dict(sorted(methods.items())),
        "extractors": dict(sorted(extractors.items())),
        "models": dict(sorted(models.items())),
        "quality": {
            "average_confidence": round(
                sum(r.confidence for r in recs) / hits, 4) if hits else 0.0,
            "verified_records": sum(1 for r in recs if r.last_verified_at),
            "stale_records": sum(1 for r in recs if r.stale),
        },
        "merkle_root": merkle_root([r.record_hash or r.payload_hash for r in recs]),
        "record_hashes_sample": [r.record_hash or r.payload_hash for r in recs[:5]],
        "extra": dict(extra or {}),
    }


def sign_manifest(manifest: dict[str, Any], secret: str) -> str:
    """HMAC over the canonical bytes of the manifest body.

    Any change to any field — one record added, one licence corrected — yields
    a different signature, so a signed manifest cannot be edited after the
    fact without the secret.
    """
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return hmac.new(
        secret.encode("utf-8"), canonical_json(body), hashlib.sha256
    ).hexdigest()


def signed_manifest(manifest: dict[str, Any], secret: str) -> dict[str, Any]:
    out = dict(manifest)
    out["signature"] = {"algorithm": ALGORITHM,
                        "key_id": key_id(secret),
                        "value": sign_manifest(manifest, secret)}
    return out


def verify_manifest(manifest: dict[str, Any], secret: str) -> tuple[bool, str]:
    """``(ok, reason)`` — reason is human-readable in both directions."""
    sig = manifest.get("signature") or {}
    if not sig:
        return False, "manifest is not signed"
    if sig.get("algorithm") != ALGORITHM:
        return False, f"unsupported algorithm {sig.get('algorithm')!r}"
    expected = sign_manifest(manifest, secret)
    if not hmac.compare_digest(str(sig.get("value", "")), expected):
        return False, "signature does not match the manifest body"
    return True, f"signed with key {sig.get('key_id', '?')}"


def key_id(secret: str) -> str:
    """Public, non-reversible key identifier (safe to publish)."""
    return "k_" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def manifest_markdown(manifest: dict[str, Any]) -> str:
    """Human-readable form — the page that goes in the sales deck."""
    lic = manifest.get("licenses", {})
    sig = manifest.get("signature", {})
    lines = [
        f"# Data passport — {manifest.get('dataset')} v{manifest.get('version')}",
        "",
        f"- **Records:** {manifest.get('record_count'):,}",
        f"- **Sources:** {manifest.get('source_count'):,}",
        f"- **Redistributable:** {lic.get('redistributable_records', 0):,} "
        f"({(lic.get('redistributable_share') or 0) * 100:.1f}%)",
        f"- **Unknown licence:** {lic.get('unknown_records', 0):,} "
        "(excluded from commercial exports unless you opt in)",
        f"- **Robots:** {_kv(manifest.get('robots'))}",
        f"- **Methods:** {_kv(manifest.get('collection_methods'))}",
        f"- **Average extraction confidence:** "
        f"{(manifest.get('quality') or {}).get('average_confidence', 0):.3f}",
        f"- **Merkle root:** `{manifest.get('merkle_root')}`",
        f"- **Signature:** `{sig.get('value', '(unsigned)')[:32]}…` "
        f"({sig.get('key_id', '-')}, {sig.get('algorithm', '-')})",
        "",
        "## Verify",
        "",
        "```python",
        "from src.provenance.manifest import verify_manifest",
        "ok, why = verify_manifest(manifest_json, secret)",
        "```",
    ]
    return "\n".join(lines) + "\n"


def _kv(d: Any) -> str:
    if not isinstance(d, dict) or not d:
        return "—"
    return ", ".join(f"{k} {v:,}" for k, v in d.items())


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
