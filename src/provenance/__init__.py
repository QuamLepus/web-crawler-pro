"""Trust & Provenance Engine.

    "Your data came from somewhere. Here is the proof."

Everything the platform collects can carry a *passport* (:mod:`models`),
written once into an append-only, hash-chained ledger (:mod:`ledger`), whose
dataset exports are signed manifests (:mod:`manifest`) backed by content
addressable snapshots you can replay (:mod:`snapshots`).

Why this exists
---------------
The hardest question in applied AI data work is not "can you crawl it", it is
"may I use it, and can you show me where it came from". Licences, robots
terms, GDPR erasure requests, model-training audits and procurement reviews
all ask the same question about a record: *what is its history?* Answering it
after the fact is guesswork; answering it requires recording it at collection
time. This package is that record.

Nothing here calls out to a third party, and nothing here is a claim of
certification — it is evidence you can hand to someone who will check.
"""

from src.provenance.ledger import ProvenanceLedger, record_for_page
from src.provenance.manifest import (
    build_manifest,
    inclusion_proof,
    manifest_markdown,
    merkle_root,
    sign_manifest,
    signed_manifest,
    verify_inclusion,
    verify_manifest,
)
from src.provenance.models import (
    ChainStep,
    LedgerStats,
    ProvenanceRecord,
    canonical_json,
    content_hash,
)
from src.provenance.snapshots import Snapshot, SnapshotStore

__all__ = [
    "ChainStep",
    "LedgerStats",
    "ProvenanceLedger",
    "ProvenanceRecord",
    "Snapshot",
    "SnapshotStore",
    "build_manifest",
    "canonical_json",
    "content_hash",
    "inclusion_proof",
    "manifest_markdown",
    "merkle_root",
    "record_for_page",
    "sign_manifest",
    "signed_manifest",
    "verify_inclusion",
    "verify_manifest",
]
