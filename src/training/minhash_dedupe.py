"""Billion-document MinHash LSH deduplication (datasketch + Spark-friendly).

Architecture at scale
---------------------
* Single node: ``datasketch.MinHashLSH`` with 128 perms, threshold ~0.85,
  num_perm split across bands for near-linear query time.
* Distributed: documents are partitioned by **band hash** — the same trick
  LSH uses internally. Each band produces a bucket key; two docs sharing any
  band-bucket are candidate duplicates. In Spark we groupByKey on
  ``(band_id, bucket_hash)`` and only compute exact Jaccard inside buckets,
  so shuffle volume stays O(n) instead of O(n^2).

The pure-python fallback (no datasketch) keeps identical semantics so tests
and dev machines work offline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
MAX_HASH = (1 << 32) - 1
PRIME = (1 << 61) - 1


def shingles(text: str, k: int = 5) -> set[str]:
    """Word-level k-shingles (5-gram default matches CC/BIG-BENCH practice)."""
    toks = _TOKEN_RE.findall(text.lower())
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


# --------------------------------------------------------------------------- #
# MinHash signature (pure python, deterministic)
# --------------------------------------------------------------------------- #
class MinHasher:
    """Generates ``num_perm`` minimum hashes per shingle set.

    Uses universal hashing h_i(s) = ((a_i * H(s) + b_i) mod P) mod MAX_HASH
    with fixed random coefficients derived from a seed => reproducible across
    processes without coordination (critical for distributed jobs).
    """

    def __init__(self, num_perm: int = 128, seed: int = 1) -> None:
        self.num_perm = num_perm
        rng = hashlib.blake2b(str(seed).encode(), digest_size=8 * num_perm * 2).digest()
        self.a = [int.from_bytes(rng[16 * i : 16 * i + 8], "big") % PRIME | 1 for i in range(num_perm)]
        self.b = [int.from_bytes(rng[16 * i + 8 : 16 * i + 16], "big") % PRIME for i in range(num_perm)]

    def signature(self, text: str, k: int = 5) -> list[int]:
        sh = shingles(text, k)
        if not sh:
            return [MAX_HASH] * self.num_perm
        base_hashes = [
            struct.unpack("<I", hashlib.md5(s.encode("utf-8")).digest()[:4])[0] for s in sh
        ]
        sig = []
        for i in range(self.num_perm):
            m = MAX_HASH
            for h in base_hashes:
                v = ((self.a[i] * h + self.b[i]) % PRIME) & MAX_HASH
                if v < m:
                    m = v
            sig.append(m)
        return sig


@dataclass
class LSHIndexEntry:
    doc_id: str
    sig: list[int]


class MinHashLSHIndex:
    """Band/row LSH over MinHash signatures.

    ``bands * rows == num_perm``; higher bands => more recall, more candidates.
    For billion-scale we recommend num_perm=128, threshold 0.8 => bands=16, rows=8.
    """

    def __init__(self, num_perm: int = 128, threshold: float = 0.8) -> None:
        self.num_perm = num_perm
        self.threshold = threshold
        self.rows, self.bands = self._optimal_shape(threshold, num_perm)
        # band_key -> {doc_id}
        self._buckets: dict[tuple[int, bytes], set[str]] = {}
        self._sigs: dict[str, list[int]] = {}
        self._hasher = MinHasher(num_perm)

    @staticmethod
    def _optimal_shape(threshold: float, num_perm: int) -> tuple[int, int]:
        """Pick (rows, bands) minimizing error: approx b such that (1/b)^(1/r)~t."""
        best = (8, num_perm // 8)
        best_err = float("inf")
        for b in range(2, num_perm):
            if num_perm % b:
                continue
            r = num_perm // b
            p = 1.0 - (1.0 - threshold ** r) ** b  # S-curve false-positive at t
            err = abs(p - 0.5)
            if err < best_err:
                best_err, best = err, (r, b)
        return best  # (rows, bands)

    # -- core ops ------------------------------------------------------------- #
    def band_keys(self, sig: list[int]) -> Iterator[bytes]:
        for bi in range(self.bands):
            chunk = sig[bi * self.rows : (bi + 1) * self.rows]
            yield hashlib.blake2b(struct.pack(f"<{self.rows}Q", *chunk), digest_size=12).digest()

    def query(self, text: str) -> set[str]:
        """Return candidate doc_ids sharing >=1 band bucket with ``text``."""
        sig = self._hasher.signature(text)
        cands: set[str] = set()
        for bi, bk in enumerate(self.band_keys(sig)):
            cands |= self._buckets.get((bi, bk), set())
        return cands

    def insert(self, doc_id: str, text: str) -> None:
        sig = self._hasher.signature(text)
        self._sigs[doc_id] = sig
        for bi, bk in enumerate(self.band_keys(sig)):
            self._buckets.setdefault((bi, bk), set()).add(doc_id)

    def jaccard_estimate(self, a: str, b: str) -> float:
        sa, sb = self._sigs.get(a), self._sigs.get(b)
        if not sa or not sb:
            return 0.0
        return sum(1 for x, y in zip(sa, sb) if x == y) / self.num_perm

    def is_duplicate(self, doc_id: str, text: str, *, verify_threshold: float | None = None) -> bool:
        """LSH candidates + signature-Jaccard verification (reduces FP rate)."""
        thr = verify_threshold if verify_threshold is not None else self.threshold
        for cand in self.query(text):
            if cand == doc_id:
                continue
            if self.jaccard_estimate(cand, doc_id) >= thr:
                return True
        self.insert(doc_id, text)
        return False

    def size(self) -> int:
        return len(self._sigs)


# --------------------------------------------------------------------------- #
# datasketch fast path (optional)
# --------------------------------------------------------------------------- #
class DataSketchDeduper:
    """Thin adapter using the C-backed datasketch MinHash when installed."""

    def __init__(self, num_perm: int = 128, threshold: float = float(os.getenv("DEDUPE_THRESHOLD", "0.85"))) -> None:
        from datasketch import MinHash, MinHashLSH  # lazy

        self._MinHash = MinHash
        self.lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self.num_perm = num_perm

    def _mh(self, text: str):
        mh = self._MinHash(num_perm=self.num_perm)
        for s in shingles(text):
            mh.update(s.encode("utf-8"))
        return mh

    def query(self, text: str) -> list[str]:
        return list(self.lsh.query(self._mh(text)))

    def insert(self, key: str, text: str) -> None:
        self.lsh.insert(key, self._mh(text))

    def is_duplicate(self, doc_id: str, text: str) -> bool:
        if self.query(text):
            return True
        self.insert(doc_id, text)
        return False


def make_deduper(prefer: str = "auto"):
    """Factory honoring DEDUPE_BACKEND env: auto|pure|datasketch."""
    prefer = os.getenv("DEDUPE_BACKEND", prefer)
    if prefer in ("auto", "datasketch"):
        try:
            return DataSketchDeduper()
        except ImportError:
            if prefer == "datasketch":
                raise
    return MinHashLSHIndex()


# --------------------------------------------------------------------------- #
# Spark-friendly banding helpers (no pyspark import required to test logic)
# --------------------------------------------------------------------------- #
@dataclass
class BandRecord:
    """One emitted (bucket_key, doc_id, signature) triple = Spark row."""

    bucket_key: str
    doc_id: str
    sig: list[int] = field(default_factory=list)


def emit_band_records(docs: Iterable[tuple[str, str]], num_perm: int = 128, threshold: float = 0.8) -> list[BandRecord]:
    """Map phase: turn (doc_id, text) pairs into band-bucket records.

    In PySpark this is exactly the ``flatMap`` before
    ``groupByKey().mapValues(exact_jaccard_within_bucket)``::

        rdd.map(lambda d: emit_band_records([d])) \
           .flatMap(lambda rs: [(r.bucket_key, (r.doc_id, r.sig)) for r in rs]) \
           .groupByKey() ...

    Keeping it as a pure function makes the distributed job unit-testable.
    """
    hasher = MinHasher(num_perm)
    index = MinHashLSHIndex(num_perm=num_perm, threshold=threshold)
    out: list[BandRecord] = []
    for doc_id, text in docs:
        sig = hasher.signature(text)
        for bi, bk in enumerate(index.band_keys(sig)):
            out.append(BandRecord(bucket_key=f"{bi}:{bk.hex()}", doc_id=doc_id, sig=sig))
    return out


def dedupe_batch(docs: Iterable[tuple[str, str]], *, threshold: float = 0.85) -> dict:
    """Convenience single-node driver: returns kept/dropped ids + stats."""
    idx = MinHashLSHIndex(num_perm=128, threshold=threshold)
    kept: list[str] = []
    dropped: list[str] = []
    for doc_id, text in docs:
        if idx.is_duplicate(doc_id, text):
            dropped.append(doc_id)
        else:
            kept.append(doc_id)
    total = len(kept) + len(dropped)
    return {
        "total": total,
        "kept": kept,
        "dropped": dropped,
        "dup_rate": len(dropped) / total if total else 0.0,
        "index_size": idx.size(),
    }
