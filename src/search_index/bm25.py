"""Sharded Okapi BM25.

Design notes (the interesting part)
-----------------------------------
**One index per shard, searched like a distributed engine.** A shard is a
self-contained CSR posting list plus its own vocabulary — the same shape as a
Lucene segment. Querying fans out to every shard, takes the top-k of each, and
merges. That is what makes level 3 possible: you can build shards on twenty
GitHub Actions runners in parallel and never hold the whole index in RAM.

**Memory-mapped.** Arrays are loaded with ``mmap_mode="r"``, so an index much
larger than RAM still answers queries; the OS pages in only the postings the
query touched. Shards are loaded lazily and evicted least-recently-used.

**Global IDF when it fits, shard-local when it does not.** Shard-local IDF is
the classic distributed-search approximation and it is slightly wrong. When the
collection's vocabulary is small enough (default 2M terms) we build a true
global document-frequency table at query time from the per-shard ``df.npy``
files and use that instead.

**Phrases are verified, not indexed.** Storing positions would roughly double
the index. Instead we retrieve extra candidates by BM25 and then check
``"exact phrase"`` against the stored text, which is nearly free because we are
already fetching those documents.
"""

from __future__ import annotations

import json
import math
from array import array
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from src.search_index.text import ParsedQuery

SHARDS_FILE = "shards.json"
MAX_TF = 0xFFFF          # tf saturates here so postings fit in uint16
MAX_TOKENS_PER_DOC = 512  # caps postings per document; see module docstring


@dataclass(slots=True)
class ShardMeta:
    """Pointer to one on-disk shard, plus the numbers its score needs."""

    shard_id: int
    source: str = ""
    doc_count: int = 0
    term_count: int = 0
    total_len: int = 0
    path: str = ""

    @property
    def avgdl(self) -> float:
        return (self.total_len / self.doc_count) if self.doc_count else 0.0

    def to_dict(self) -> dict:
        return {"shard_id": self.shard_id, "source": self.source,
                "doc_count": self.doc_count, "term_count": self.term_count,
                "total_len": self.total_len, "path": self.path}

    @classmethod
    def from_dict(cls, data: dict) -> ShardMeta:
        return cls(shard_id=int(data["shard_id"]), source=str(data.get("source", "")),
                   doc_count=int(data.get("doc_count", 0)),
                   term_count=int(data.get("term_count", 0)),
                   total_len=int(data.get("total_len", 0)),
                   path=str(data.get("path", "")))


class BM25Shard:
    """A single immutable BM25 segment.

    Build with :meth:`build` -> :meth:`save`; read back with :meth:`load`.
    """

    K1 = 1.2
    B = 0.75

    def __init__(self, meta: ShardMeta | None = None) -> None:
        self.meta = meta or ShardMeta(shard_id=-1)
        self.vocab: dict[str, int] = {}
        self.indptr: np.ndarray = np.zeros(1, dtype=np.int64)
        self.indices: np.ndarray = np.zeros(0, dtype=np.int32)
        self.data: np.ndarray = np.zeros(0, dtype=np.uint16)
        self.doclen: np.ndarray = np.zeros(0, dtype=np.int32)
        self.rowids: np.ndarray = np.zeros(0, dtype=np.int64)

    # -- build --------------------------------------------------------------- #
    def build(self, rowids: Sequence[int], texts: Sequence[str], *,
              shard_id: int = 0, source: str = "") -> BM25Shard:
        """Invert ``texts``; ``rowids[i]`` is the DocStore id of ``texts[i]``."""
        from src.search_index.text import analyze

        vocab: dict[str, int] = {}
        postings: list[array] = []
        doclen = array("i")
        n_docs = 0
        total_len = 0

        for text in texts:
            tokens = analyze(text)[:MAX_TOKENS_PER_DOC]
            ordinal = n_docs
            counts = Counter(tokens)
            doclen.append(len(tokens))
            total_len += len(tokens)
            for term, tf in counts.items():
                tid = vocab.get(term)
                if tid is None:
                    tid = len(vocab)
                    vocab[term] = tid
                    postings.append(array("q"))
                postings[tid].append((ordinal << 16) | min(tf, MAX_TF))
            n_docs += 1

        indptr = np.zeros(len(vocab) + 1, dtype=np.int64)
        for tid, arr in enumerate(postings):
            indptr[tid + 1] = indptr[tid] + len(arr)
        packed = np.empty(int(indptr[-1]), dtype=np.int64)
        for tid, arr in enumerate(postings):
            if len(arr):
                packed[indptr[tid]:indptr[tid + 1]] = np.frombuffer(arr, dtype=np.int64)

        self.vocab = vocab
        self.indptr = indptr
        self.indices = (packed >> 16).astype(np.int32)
        self.data = (packed & MAX_TF).astype(np.uint16)
        self.doclen = np.frombuffer(doclen, dtype=np.int32).astype(np.int32)
        self.rowids = np.asarray(rowids, dtype=np.int64)
        self.meta = ShardMeta(shard_id=shard_id, source=source, doc_count=n_docs,
                              term_count=len(vocab), total_len=total_len)
        return self

    @property
    def df(self) -> np.ndarray:
        return (self.indptr[1:] - self.indptr[:-1]).astype(np.int32)

    # -- persistence --------------------------------------------------------- #
    def save(self, directory: str | Path) -> ShardMeta:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "vocab.json").write_text(
            json.dumps(self.vocab), encoding="utf-8")
        np.save(directory / "indptr.npy", self.indptr)
        np.save(directory / "indices.npy", self.indices)
        np.save(directory / "data.npy", self.data)
        np.save(directory / "doclen.npy", self.doclen)
        np.save(directory / "rowids.npy", self.rowids)
        np.save(directory / "df.npy", self.df)
        self.meta.path = directory.name
        (directory / "meta.json").write_text(
            json.dumps(self.meta.to_dict()), encoding="utf-8")
        return self.meta

    @classmethod
    def load(cls, directory: str | Path, *, mmap: bool = True) -> BM25Shard:
        directory = Path(directory)
        meta = ShardMeta.from_dict(json.loads((directory / "meta.json").read_text()))
        shard = cls(meta)
        mode = "r" if mmap else None
        shard.vocab = json.loads((directory / "vocab.json").read_text())
        shard.indptr = np.load(directory / "indptr.npy", mmap_mode=mode)
        shard.indices = np.load(directory / "indices.npy", mmap_mode=mode)
        shard.data = np.load(directory / "data.npy", mmap_mode=mode)
        shard.doclen = np.load(directory / "doclen.npy", mmap_mode=mode)
        shard.rowids = np.load(directory / "rowids.npy", mmap_mode=mode)
        return shard

    # -- search -------------------------------------------------------------- #
    def search(self, terms: Sequence[str], top_k: int, *,
               global_idf: dict[str, float] | None = None,
               global_avgdl: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(rowids, scores)`` for the shard's best ``top_k`` documents."""
        n = int(self.meta.doc_count)
        if n == 0 or not terms:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32)

        avgdl = global_avgdl or self.meta.avgdl or 1.0
        scores = np.zeros(n, dtype=np.float32)
        k1, b = self.K1, self.B
        indptr, indices, data, doclen = self.indptr, self.indices, self.data, self.doclen
        df = self.df

        for term in terms:
            tid = self.vocab.get(term)
            if tid is None:
                continue
            lo, hi = int(indptr[tid]), int(indptr[tid + 1])
            if lo == hi:
                continue
            if global_idf is not None:
                idf = global_idf.get(term)
                if idf is None:
                    continue
            else:
                idf = math.log(1.0 + (n - float(df[tid]) + 0.5) / (float(df[tid]) + 0.5))
            if idf <= 0.0:
                continue
            doc_ids = indices[lo:hi]
            tf = data[lo:hi].astype(np.float32)
            norm = k1 * (1.0 - b + b * doclen[doc_ids].astype(np.float32) / avgdl)
            np.add.at(scores, doc_ids, idf * tf * (k1 + 1.0) / (tf + norm))

        if top_k >= n:
            take = np.nonzero(scores > 0)[0]
        else:
            take = np.argpartition(scores, -top_k)[-top_k:]
            take = take[scores[take] > 0]
        order = np.argsort(scores[take])[::-1]
        take = take[order]
        return np.asarray(self.rowids[take], dtype=np.int64), scores[take].astype(float)


# --------------------------------------------------------------------------- #
# Collection of shards
# --------------------------------------------------------------------------- #
@dataclass
class IndexPaths:
    index_dir: Path
    shards_file: Path
    metas: list[ShardMeta] = field(default_factory=list)


class ShardedBM25:
    """Fan a query out across every shard and merge the results."""

    def __init__(self, index_dir: str | Path, *, mmap: bool = True,
                 max_loaded_shards: int = 8,
                 global_idf_max_terms: int = 2_000_000) -> None:
        self.index_dir = Path(index_dir)
        self._mmap = mmap
        self._max_loaded = max(1, int(max_loaded_shards))
        self._global_idf_max_terms = int(global_idf_max_terms)
        self._metas: list[ShardMeta] = []
        self._loaded: dict[int, BM25Shard] = {}
        self._lru: list[int] = []
        self._global_idf: dict[str, float] | None = None
        self._global_avgdl: float | None = None
        self._global_attempted = False
        self.reload()

    # -- discovery ----------------------------------------------------------- #
    def reload(self) -> int:
        """(Re)read ``shards.json``. Returns the shard count."""
        path = self.index_dir / SHARDS_FILE
        self._metas = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("shards", []) if isinstance(data, dict) else data
                self._metas = [ShardMeta.from_dict(m) for m in raw]
                self._metas.sort(key=lambda m: m.shard_id)
            except (json.JSONDecodeError, KeyError, TypeError):
                self._metas = []
        self._global_idf = None
        self._global_avgdl = None
        self._global_attempted = False
        return len(self._metas)

    @property
    def metas(self) -> list[ShardMeta]:
        return list(self._metas)

    @property
    def shard_count(self) -> int:
        return len(self._metas)

    @property
    def doc_count(self) -> int:
        return sum(m.doc_count for m in self._metas)

    @property
    def term_count(self) -> int:
        return sum(m.term_count for m in self._metas)

    def _shard(self, meta: ShardMeta) -> BM25Shard | None:
        cached = self._loaded.get(meta.shard_id)
        if cached is not None:
            return cached
        directory = self.index_dir / meta.path
        if not (directory / "meta.json").exists():
            return None
        try:
            shard = BM25Shard.load(directory, mmap=self._mmap)
        except (OSError, ValueError, KeyError):
            return None
        self._loaded[meta.shard_id] = shard
        self._lru.append(meta.shard_id)
        while len(self._loaded) > self._max_loaded:
            victim = self._lru.pop(0)
            self._loaded.pop(victim, None)
        return shard

    def _ensure_global_idf(self) -> None:
        """Build a true global IDF table, unless the vocabulary is too big."""
        if self._global_attempted:
            return
        self._global_attempted = True
        if self.term_count > self._global_idf_max_terms or not self._metas:
            return
        df: dict[str, int] = {}
        total_len = 0
        total_docs = 0
        for meta in self._metas:
            directory = self.index_dir / meta.path
            try:
                vocab = json.loads((directory / "vocab.json").read_text(encoding="utf-8"))
                counts = np.load(directory / "df.npy")
            except (OSError, ValueError):
                return          # incomplete index -> fall back to shard-local
            for term, tid in vocab.items():
                df[term] = df.get(term, 0) + int(counts[int(tid)])
            total_len += meta.total_len
            total_docs += meta.doc_count
        if not total_docs:
            return
        self._global_avgdl = total_len / total_docs
        self._global_idf = {
            term: math.log(1.0 + (total_docs - n + 0.5) / (n + 0.5))
            for term, n in df.items()
        }

    # -- search -------------------------------------------------------------- #
    def search(self, parsed: ParsedQuery, top_k: int = 10, *,
               sources: Sequence[str] | None = None,
               oversample: int = 4) -> list[tuple[int, float]]:
        """Return ``[(rowid, bm25_score), ...]`` best-first across all shards."""
        if not self._metas or parsed.is_empty:
            return []
        self._ensure_global_idf()

        wanted = {s.lower() for s in sources} if sources else None
        # ask each shard for more than we need: phrase/exclusion checks and
        # host collapsing both prune afterwards
        per_shard = max(top_k * oversample, 20)
        merged: list[tuple[int, float]] = []
        for meta in self._metas:
            if wanted and meta.source.lower() not in wanted:
                continue
            shard = self._shard(meta)
            if shard is None:
                continue
            try:
                rowids, scores = shard.search(
                    parsed.all_terms(), per_shard,
                    global_idf=self._global_idf, global_avgdl=self._global_avgdl)
            except (ValueError, IndexError, OSError):
                continue
            merged.extend(zip(rowids.tolist(), scores.tolist()))

        # The same document can be reached through two shards (e.g. two parts
        # of a merged build both indexed it) — keep its best score only.
        best: dict[int, float] = {}
        for rowid, score in merged:
            if score > best.get(rowid, -1.0):
                best[rowid] = score
        merged = sorted(best.items(), key=lambda pair: pair[1], reverse=True)
        return merged[: max(top_k * oversample, top_k)]

    def stats(self) -> dict:
        return {
            "shards": self.shard_count,
            "docs": self.doc_count,
            "terms": self.term_count,
            "loaded_shards": len(self._loaded),
            "global_idf": bool(self._global_idf),
            "sources": {
                meta.source: sum(m.doc_count for m in self._metas if m.source == meta.source)
                for meta in self._metas
            },
        }
