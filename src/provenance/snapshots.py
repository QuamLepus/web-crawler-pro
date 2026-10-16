"""Time Machine — content-addressed copies of what we actually saw.

"Show me why the model said that" is only answerable if the exact bytes are
still around. So every page the platform is asked to remember is stored under
``sha256`` of its content, gzipped, on disk (or in object storage), and the
provenance record points at it by hash. Identical pages collapse to one file,
which is also how the platform can tell you a hundred URLs served the same
content.

The store is intentionally simple and dependency-free:

    data/snapshots/<ab>/<sha256>.html.gz
    data/snapshots/index/<sha256>.json      # small metadata sidecar

Replay (``replay``) returns the bytes, the extraction inputs, and the prompt
hash for any record — enough to reproduce a decision, not just describe it.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.provenance.models import content_hash

logger = logging.getLogger("wcp.provenance.snapshots")


@dataclass
class Snapshot:
    snapshot_id: str          # sha256:<hex> of the raw bytes
    path: Path
    size: int                 # uncompressed bytes
    stored_at: float
    content_type: str = ""
    url: str = ""
    status: int = 200

    @property
    def hex(self) -> str:
        return self.snapshot_id.split(":", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "size": self.size,
            "stored_at": round(self.stored_at, 3),
            "content_type": self.content_type,
            "url": self.url,
            "status": self.status,
        }


class SnapshotStore:
    """Filesystem-backed, content-addressed snapshot store."""

    def __init__(self, root: str | Path | None = None, *,
                 max_bytes: int = 2 * 1024 * 1024) -> None:
        self.root = Path(root or os.getenv("WCP_SNAPSHOT_DIR",
                                           "data/snapshots"))
        # Anything above this is recorded by hash only (a 300 MB PDF is not
        # worth gzip-ing into the replay path).
        self.max_bytes = max_bytes

    # -- paths --------------------------------------------------------------- #
    def _blob(self, hex_digest: str) -> Path:
        return self.root / hex_digest[:2] / f"{hex_digest}.html.gz"

    def _sidecar(self, hex_digest: str) -> Path:
        return self.root / "index" / f"{hex_digest}.json"

    # -- writes -------------------------------------------------------------- #
    def put(self, body: bytes | str, *, url: str = "", content_type: str = "",
            status: int = 200) -> Snapshot:
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        sid = content_hash(body)
        hex_digest = sid.split(":", 1)[-1]
        blob = self._blob(hex_digest)
        snap = Snapshot(snapshot_id=sid, path=blob, size=len(body),
                        stored_at=time.time(), content_type=content_type,
                        url=url, status=status)
        if len(body) > self.max_bytes:
            logger.debug("snapshot %s exceeds budget (%s bytes) — not stored",
                         sid, len(body))
            return snap
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_suffix(".tmp")
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                fh.write(body)
            tmp.replace(blob)          # atomic: no half-written snapshots
        side = self._sidecar(hex_digest)
        if not side.exists():
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_text(json.dumps({**snap.to_dict(), "url_hint": url,
                                        "content_type": content_type},
                                       indent=2), encoding="utf-8")
        return snap

    # -- reads --------------------------------------------------------------- #
    def has(self, snapshot_id: str) -> bool:
        return self._blob(snapshot_id.split(":", 1)[-1]).exists()

    def get(self, snapshot_id: str) -> bytes | None:
        blob = self._blob(snapshot_id.split(":", 1)[-1])
        if not blob.exists():
            return None
        with gzip.open(blob, "rb") as fh:
            return fh.read()

    def meta(self, snapshot_id: str) -> dict[str, Any] | None:
        side = self._sidecar(snapshot_id.split(":", 1)[-1])
        if not side.exists():
            return None
        try:
            return json.loads(side.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def text(self, snapshot_id: str, *, limit: int = 200_000) -> str:
        raw = self.get(snapshot_id) or b""
        return raw[:limit].decode("utf-8", "replace")

    def stats(self) -> dict[str, Any]:
        files = list(self.root.rglob("*.html.gz")) if self.root.exists() else []
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                continue
        return {"snapshots": len(files), "compressed_bytes": total,
                "root": str(self.root)}

    # -- replay -------------------------------------------------------------- #
    def replay(self, record, *, limit: int = 200_000) -> dict[str, Any]:
        """Everything needed to reproduce how a record was produced.

        Accepts a :class:`~src.provenance.models.ProvenanceRecord` (or any
        object with the same attributes) and returns the raw content plus the
        chain, extraction method and prompt hash that made the record.
        """
        sid = getattr(record, "snapshot_id", "") or getattr(record, "content_sha256", "")
        raw = self.get(sid) if sid else None
        return {
            "record_id": getattr(record, "record_id", ""),
            "source_url": getattr(record, "source_url", ""),
            "snapshot_id": sid,
            "available": raw is not None,
            "content_type": (self.meta(sid) or {}).get("content_type", "") if sid else "",
            "content": (raw or b"")[:limit].decode("utf-8", "replace"),
            "chain": [s.to_dict() for s in getattr(record, "chain", [])],
            "extractor": getattr(record, "extractor", ""),
            "extractor_version": getattr(record, "extractor_version", ""),
            "llm_model_id": getattr(record, "llm_model_id", ""),
            "prompt_hash": getattr(record, "prompt_hash", ""),
            "fetched_at": getattr(record, "fetched_at", None),
            "content_sha256": getattr(record, "content_sha256", ""),
        }
