"""Two-stage retrieval: BM25 finds the candidates, a reranker picks the order.

BM25 is a bag-of-words model. It cannot tell that "cars" and "automobiles" are
the same thing, and it cannot tell that a page *about* solar panels outranks a
page that merely repeats "solar panels" forty times. A reranker fixes both, and
it only has to look at the handful of documents BM25 already shortlisted.

Three backends, in increasing order of quality and cost:

``none`` (default)
    No rerank. $0, offline, zero latency. This is what level 0 ships with and
    it is genuinely fine — Curlie's category paths already encode most of the
    semantics BM25 would otherwise miss.

``embedding``
    A bi-encoder (``all-MiniLM-L6-v2`` by default, 384-d, Apache-2.0, runs on
    CPU) embeds the query and each candidate and reorders by cosine similarity.
    Two flavours:
      * **precomputed** — vectors were written at index time into each shard's
        ``embeddings.npy``, so reranking is one matrix-vector product per shard;
      * **on the fly** — vectors are computed at query time. Slower, but needs
        no extra disk and works on an index built before this existed.

``cross-encoder``
    ``cross-encoder/ms-marco-MiniLM-L-6-v2`` scores (query, document) *pairs*,
    which is materially more accurate than cosine over separate vectors because
    the model sees both at once. Costs one forward pass per candidate, so keep
    the candidate list short. Optional dependency.

Blending
--------
Both scores are min-max normalised **within the candidate set** and combined as
``(1 - w)·bm25 + w·semantic`` with ``w`` defaulting to 0.35. Normalising inside
the candidate set is deliberate: BM25 scores are unbounded and vary by orders of
magnitude between shards, so mixing raw numbers would let one signal swamp the
other.

Refusing to lie
---------------
``src.training.semantic_dedupe.make_embedder`` falls back to a *hashing*
embedder when sentence-transformers is not installed. That fallback is lexical,
not semantic — using it to rerank BM25 would just re-measure the same thing and
call it a second opinion. So the reranker detects the fallback and disables
itself with a warning unless you explicitly set ``allow_hash_fallback=True``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from src.search_index.bm25 import EMBEDDINGS_FILE
from src.search_index.models import SearchResult

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    """Anything that can reorder shortlisted results."""

    name: str

    def rerank(self, query: str, results: Sequence[SearchResult], *,
               top_k: int = 10) -> list[SearchResult]:
        ...


def _minmax(values: np.ndarray) -> np.ndarray:
    """Scale to [0, 1]; a constant vector maps to all-ones, not all-zeros."""
    if values.size == 0:
        return values
    lo, hi = float(values.min()), float(values.max())
    if math.isclose(hi, lo):
        return np.ones_like(values)
    return (values - lo) / (hi - lo)


# --------------------------------------------------------------------------- #
# no-op
# --------------------------------------------------------------------------- #
class NoopReranker:
    """The default: keep BM25's order (after authority blending)."""

    name = "none"

    def rerank(self, query: str, results: Sequence[SearchResult], *,
               top_k: int = 10) -> list[SearchResult]:
        return list(results)[:top_k]


# --------------------------------------------------------------------------- #
# embeddings
# --------------------------------------------------------------------------- #
def is_hashing_embedder(embedder) -> bool:
    """True when ``embedder`` is the lexical fallback, not a real model."""
    return type(embedder).__name__ == "_HashingEmbedder" or not hasattr(
        embedder, "model")


class EmbeddingReranker:
    """Cosine similarity between the query vector and each candidate's vector."""

    name = "embedding"

    def __init__(self, embedder=None, *, model_name: str | None = None,
                 weight: float = 0.35, batch_size: int = 64,
                 allow_hash_fallback: bool = False,
                 use_precomputed: bool = True) -> None:
        self.weight = float(weight)
        self.batch_size = max(1, int(batch_size))
        self.use_precomputed = bool(use_precomputed)
        self._embedder = embedder
        self._model_name = model_name
        self._enabled = True
        self.disabled_reason = ""

    # -- lazy setup ---------------------------------------------------------- #
    @property
    def embedder(self):
        if self._embedder is None:
            from src.training.semantic_dedupe import make_embedder

            self._embedder = make_embedder(self._model_name)
            if is_hashing_embedder(self._embedder) and not getattr(
                    self, "_allow_hash", False):
                self._enabled = False
                self.disabled_reason = (
                    "sentence-transformers is not installed; the hashing "
                    "embedder is lexical, so using it to 'rerank' BM25 would "
                    "be measuring the same thing twice")
        return self._embedder

    def allow_hash_fallback(self, allowed: bool = True) -> EmbeddingReranker:
        self._allow_hash = allowed
        self._enabled = True
        return self

    @property
    def available(self) -> bool:
        if not self._enabled:
            return False
        _ = self.embedder
        return self._enabled

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return an L2-normalised float32 matrix (n × dim)."""
        vectors = np.asarray(self.embedder.encode(list(texts)), dtype="float32")
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    # -- the protocol -------------------------------------------------------- #
    def rerank(self, query: str, results: Sequence[SearchResult], *,
               top_k: int = 10) -> list[SearchResult]:
        results = list(results)
        if len(results) < 2 or not self.available:
            return results[:top_k]
        try:
            query_vec = self.encode([query])[0]
            doc_vecs = self._document_vectors(results)
            if doc_vecs is None or len(doc_vecs) != len(results):
                return results[:top_k]
            similarity = doc_vecs @ query_vec
        except Exception as exc:  # noqa: BLE001 - reranking must never break search
            logger.warning("embedding rerank failed (%s); keeping BM25 order", exc)
            return results[:top_k]

        return _blend(results, similarity, self.weight, top_k, "semantic")

    def _document_vectors(self, results: list[SearchResult]) -> np.ndarray | None:
        """Subclasses override to use precomputed vectors; default computes them."""
        texts = [_candidate_text(r) for r in results]
        chunks = []
        for start in range(0, len(texts), self.batch_size):
            chunks.append(self.encode(texts[start:start + self.batch_size]))
        return np.vstack(chunks) if chunks else None


class ShardEmbeddingReranker(EmbeddingReranker):
    """Uses vectors stored in each shard at index time (the fast path).

    ``build --embeddings`` writes ``embeddings.npy`` next to the BM25 arrays,
    aligned row-for-row with ``rowids.npy``. Reranking then costs one
    matrix-vector product per shard instead of one model call per candidate —
    roughly three orders of magnitude faster.

    Disk: ``docs × dim × 4`` bytes (float32). At MiniLM's 384 dimensions that is
    154 MB per 100k-document shard, so level 3 (100M docs) would need ~150 GB.
    Vectors are loaded lazily per shard and evicted least-recently-used.
    """

    name = "embedding:precomputed"

    def __init__(self, index_dir: str | Path, embedder=None, *,
                 model_name: str | None = None, weight: float = 0.35,
                 max_loaded_shards: int = 4, use_precomputed: bool = True) -> None:
        super().__init__(embedder, model_name=model_name, weight=weight,
                         use_precomputed=use_precomputed)
        self.index_dir = Path(index_dir)
        self._max_loaded = max(1, int(max_loaded_shards))
        self._cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._lru: list[int] = []

    def _shard_vectors(self, shard_id: int) -> tuple[np.ndarray, np.ndarray] | None:
        """``(vectors, rowids)`` for one shard, or ``None`` if it has none."""
        cached = self._cache.get(shard_id)
        if cached is not None:
            return cached
        directory = self.index_dir / f"shard-{shard_id:05d}"
        vectors_path = directory / EMBEDDINGS_FILE
        rowids_path = directory / "rowids.npy"
        if not (vectors_path.exists() and rowids_path.exists()):
            return None
        try:
            vectors = np.load(vectors_path, mmap_mode="r")
            rowids = np.load(rowids_path)
        except (OSError, ValueError) as exc:  # pragma: no cover - corrupt shard
            logger.warning("cannot read embeddings for shard %d (%s)", shard_id, exc)
            return None
        self._cache[shard_id] = (vectors, rowids)
        self._lru.append(shard_id)
        while len(self._cache) > self._max_loaded:
            self._cache.pop(self._lru.pop(0), None)
        return self._cache[shard_id]

    def _document_vectors(self, results: list[SearchResult]) -> np.ndarray | None:
        if not self.use_precomputed:
            return super()._document_vectors(results)

        by_shard: dict[int, list[int]] = {}
        for i, result in enumerate(results):
            shard_id = getattr(result, "shard_id", -1)
            if shard_id is not None and shard_id >= 0:
                by_shard.setdefault(int(shard_id), []).append(i)

        out = np.zeros((len(results), int(self.embedder.dim)), dtype="float32")
        filled = np.zeros(len(results), dtype=bool)
        for shard_id, positions in by_shard.items():
            loaded = self._shard_vectors(shard_id)
            if loaded is None:
                continue
            vectors, rowids = loaded
            index_of = {int(rid): j for j, rid in enumerate(rowids.tolist())}
            for position in positions:
                row = index_of.get(int(results[position].doc_id))
                if row is not None:
                    out[position] = np.asarray(vectors[row], dtype="float32")
                    filled[position] = True
        if not filled.all():
            # some shard has no vectors — compute just those on the fly
            missing = [i for i, ok in enumerate(filled.tolist()) if not ok]
            computed = super()._document_vectors([results[i] for i in missing])
            if computed is None or len(computed) != len(missing):
                return None
            for slot, i in enumerate(missing):
                out[i] = computed[slot]
        return out


# --------------------------------------------------------------------------- #
# cross-encoder
# --------------------------------------------------------------------------- #
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker:
    """Scores (query, document) pairs jointly — the most accurate option.

    Needs ``sentence-transformers``; degrades to a no-op when it is missing or
    when the model cannot be downloaded, so an offline deployment still answers.
    """

    name = "cross-encoder"

    def __init__(self, model_name: str = CROSS_ENCODER_MODEL,
                 weight: float = 0.5, max_candidates: int = 50) -> None:
        self.model_name = model_name
        self.weight = float(weight)
        self.max_candidates = max(1, int(max_candidates))
        self._model = None
        self._unavailable = False
        self.disabled_reason = ""

    @property
    def model(self):
        if self._model is None and not self._unavailable:
            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self.model_name)
            except Exception as exc:  # noqa: BLE001 - optional dependency
                self._unavailable = True
                self.disabled_reason = str(exc)
                logger.info("cross-encoder unavailable (%s); rerank disabled", exc)
        return self._model

    @property
    def available(self) -> bool:
        return self.model is not None

    def rerank(self, query: str, results: Sequence[SearchResult], *,
               top_k: int = 10) -> list[SearchResult]:
        results = list(results)
        if len(results) < 2 or not self.available:
            return results[:top_k]
        candidates = results[: self.max_candidates]
        try:
            pairs = [(query, _candidate_text(r)) for r in candidates]
            raw = self.model.predict(pairs)
            scores = np.asarray(raw, dtype="float64").reshape(-1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross-encoder rerank failed (%s)", exc)
            return results[:top_k]
        if len(scores) < len(results):
            scores = np.concatenate(
                [scores, np.full(len(results) - len(scores), float(scores.min()))])
        return _blend(results, scores, self.weight, top_k, "rerank")


# --------------------------------------------------------------------------- #
# shared plumbing
# --------------------------------------------------------------------------- #
def _candidate_text(result: SearchResult, limit: int = 1200) -> str:
    doc = result.doc
    title = doc.title or ""
    body = (doc.snippet or "") + " " + (doc.body or "")
    return f"{title}. {body.strip()}"[:limit] or doc.url


def _blend(results: list[SearchResult], semantic: np.ndarray, weight: float,
           top_k: int, field: str) -> list[SearchResult]:
    """Convex combination of min-max-normalised BM25 and semantic scores."""
    bm25 = np.asarray([r.score for r in results], dtype="float64")
    semantic = np.asarray(semantic, dtype="float64").reshape(-1)
    weight = min(1.0, max(0.0, float(weight)))
    combined = (1.0 - weight) * _minmax(bm25) + weight * _minmax(semantic)

    order = np.argsort(-combined, kind="stable")
    out: list[SearchResult] = []
    for rank, i in enumerate(order.tolist(), 1):
        result = results[i]
        result.score = float(combined[i])
        setattr(result, field, float(semantic[i]))
        out.append(result)
        if len(out) >= top_k:
            break
    return out


def build_reranker(mode: str = "none", *, index_dir: str | Path | None = None,
                   weight: float = 0.35, model_name: str | None = None,
                   cross_encoder_model: str = CROSS_ENCODER_MODEL,
                   max_loaded_shards: int = 4) -> Reranker:
    """Instantiate the configured reranker; unknown modes fall back to none."""
    mode = (mode or "none").strip().lower()
    if mode in ("", "none", "off", "false"):
        return NoopReranker()
    if mode in ("cross", "cross-encoder", "crossencoder"):
        return CrossEncoderReranker(cross_encoder_model, weight=max(weight, 0.5))
    if mode in ("embedding", "embeddings", "vector", "semantic"):
        if index_dir is None:
            return EmbeddingReranker(weight=weight, model_name=model_name)
        return ShardEmbeddingReranker(index_dir, weight=weight,
                                      model_name=model_name,
                                      max_loaded_shards=max_loaded_shards)
    logger.warning("unknown SEARCH_INDEX_RERANK mode %r; reranking disabled", mode)
    return NoopReranker()
