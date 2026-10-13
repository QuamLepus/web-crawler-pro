"""Semantic deduplication: Sentence-Transformers embeddings + FAISS ANN search.

Near-duplicate MinHash catches lexical overlap; semantic dedupe catches
paraphrases and template-reworded spam. Pipeline per document:

  1. embed with ``all-MiniLM-L6-v2`` (384-d, normalized) — or any ST model
  2. FAISS ``IndexIDMap(IndexFlatIP)`` for exact cosine at small scale;
     production switches to ``IVF,HNSW`` variants via ``index_factory``
  3. if best cosine >= threshold => duplicate of the returned id

Without GPU/optional deps we fall back to a deterministic hashing embedding
(bag-of-shadows into 256 dims) + numpy brute force, so behaviour is testable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


# --------------------------------------------------------------------------- #
# Embedding backends
# --------------------------------------------------------------------------- #
class _HashingEmbedder:
    """Dependency-free fallback: hashed trigram projection, L2-normalized."""

    dim = 256

    def encode(self, texts: list[str]):
        import numpy as np

        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            toks = _TOKEN_RE.findall(t.lower())
            grams = toks + [" ".join(toks[j : j + 2]) for j in range(len(toks) - 1)]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=4).digest(), "big")
                idx = h % self.dim
                sign = 1.0 if (h >> 31) & 1 else -1.0
                out[i, idx] += sign
            norm = float(np.linalg.norm(out[i])) or 1.0
            out[i] /= norm
        return out


class _STEmbedder:
    """Sentence-Transformers backend (lazy heavy import)."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]):
        return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def make_embedder(model_name: str | None = None):
    name = model_name or os.getenv("ST_MODEL", "all-MiniLM-L6-v2")
    try:
        emb = _STEmbedder(name)
        logger.info("semantic dedupe using sentence-transformers/%s", name)
        return emb
    except Exception as exc:  # noqa: BLE001 ImportError or model download failure
        logger.warning("sentence-transformers unavailable (%s); hashing fallback", exc)
        return _HashingEmbedder()


# --------------------------------------------------------------------------- #
# Vector index (FAISS fast path, numpy fallback)
# --------------------------------------------------------------------------- #
@dataclass
class MatchResult:
    doc_id: str
    score: float  # cosine similarity


class VectorIndex:
    """Cosine-similarity ANN index over normalized embeddings."""

    def __init__(self, dim: int, use_faiss: bool = True) -> None:
        self.dim = dim
        self._ids: list[str] = []
        self._faiss = None
        self._index = None
        self._vectors = None
        if use_faiss:
            try:
                import faiss

                self._faiss = faiss
                self._index = faiss.IndexIDMap(faiss.IndexFlatIP(dim))
            except ImportError:
                logger.info("faiss not installed; numpy brute-force fallback")
        if self._index is None:
            import numpy as np

            self._vectors = np.zeros((0, dim), dtype="float32")

    def add(self, doc_ids: Iterable[str], vectors) -> None:
        import numpy as np

        vecs = np.asarray(vectors, dtype="float32")
        ids_list = list(doc_ids)
        if self._index is not None:
            ids = np.fromiter(
                (int.from_bytes(hashlib.blake2b(i.encode(), digest_size=8).digest(), "big") for i in ids_list),
                dtype="int64",
            )
            self._index.add_with_ids(vecs, ids)
            self._ids.extend(ids_list)
        else:
            self._vectors = np.vstack([self._vectors, vecs]) if len(ids_list) else self._vectors
            self._ids.extend(ids_list)

    def search(self, vector, k: int = 1) -> list[MatchResult]:
        import numpy as np

        q = np.asarray([vector], dtype="float32")
        if self._index is not None:
            scores, idxs = self._index.search(q, k)
            out: list[MatchResult] = []
            id_to_name = getattr(self, "_id_names", None)
            for s, ix in zip(scores[0], idxs[0]):
                if ix == -1:
                    continue
                pos = self._lookup_pos(int(ix))
                if pos is not None:
                    out.append(MatchResult(self._ids[pos], float(s)))
            return out
        if len(self._ids) == 0:
            return []
        sims = (self._vectors @ q[0]).tolist()
        top = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)[:k]
        return [MatchResult(self._ids[i], float(sims[i])) for i in top]

    def _lookup_pos(self, faiss_id: int) -> int | None:
        # Map hash-id back to insertion position by scanning the shadow list.
        if not hasattr(self, "_hash_to_pos"):
            self._hash_to_pos = {}
            for pos, did in enumerate(self._ids):
                self._hash_to_pos[
                    int.from_bytes(hashlib.blake2b(did.encode(), digest_size=8).digest(), "big")
                ] = pos
        return self._hash_to_pos.get(faiss_id)

    def count(self) -> int:
        return len(self._ids)


# --------------------------------------------------------------------------- #
# Public deduper
# --------------------------------------------------------------------------- #
class SemanticDeduplicator:
    """embed -> ANN query -> threshold decision; inserts novel docs."""

    def __init__(
        self,
        *,
        threshold: float = float(os.getenv("SEMDEDUP_THRESHOLD", "0.92")),
        model_name: str | None = None,
        batch_size: int = 64,
    ) -> None:
        self.embedder = make_embedder(model_name)
        self.threshold = threshold
        self.batch_size = batch_size
        self.index = VectorIndex(dim=self.embedder.dim)

    def is_duplicate(self, doc_id: str, text: str) -> MatchResult | None:
        vec = self.embedder.encode([text])[0]
        matches = self.index.search(vec, k=1)
        if matches and matches[0].score >= self.threshold:
            return matches[0]
        self.index.add([doc_id], [vec])
        return None

    def dedupe_stream(self, docs: Iterable[tuple[str, str]]) -> dict:
        """Run over (doc_id, text); returns kept/dropped summary with dup pairs."""
        kept: list[str] = []
        dropped: list[tuple[str, str, float]] = []
        for doc_id, text in docs:
            m = self.is_duplicate(doc_id, text)
            if m is None:
                kept.append(doc_id)
            else:
                dropped.append((doc_id, m.doc_id, round(m.score, 4)))
        return {
            "kept": len(kept),
            "dropped": len(dropped),
            "pairs": dropped[:100],  # sample for human review
            "index_size": self.index.count(),
        }
