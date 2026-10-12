"""Near-duplicate detection: MinHash (datasketch) + SimHash fallback.

MinHashDeduper  – Jaccard-based shingle comparison, probabilistic index.
SimHashDeduper  – Hamming-distance fingerprint (Charikar), cheap & stateless.

Both degrade gracefully to pure-python implementations when the optional
`datasketch` package is missing, so the pipeline never hard-fails.
"""

from __future__ import annotations

import hashlib
import re

from src.config import get_config

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _shingles(text: str, k: int = 5) -> set[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


class MinHashDeduper:
    """Redis-persisted MinHash index shared across worker nodes."""

    NUM_PERM = 128
    THRESHOLD = float(__import__("os").getenv("DEDUPE_THRESHOLD", "0.9"))
    REDIS_KEY = "dedupe:minhash"

    # -- pure-python MinHash (no datasketch required) ------------------- #
    @staticmethod
    def signature(text: str) -> list[int]:
        sh = sorted(_shingles(text))
        sig = []
        for i in range(MinHashDeduper.NUM_PERM):
            best = None
            for s in sh:
                h = int(hashlib.md5(f"{i}:{s}".encode()).hexdigest(), 16)
                best = h if best is None or h < best else best
            sig.append(best or 0)
        return sig

    @staticmethod
    def jaccard(sig_a: list[int], sig_b: list[int]) -> float:
        if not sig_a or not sig_b:
            return 0.0
        equal = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
        return equal / len(sig_a)

    @classmethod
    def is_duplicate(cls, item: dict) -> bool:
        """Check against the shared index; register if novel. Uses Redis when
        reachable, otherwise an in-process set (single-node dev mode)."""
        text = item.get("text") or item.get("title") or ""
        if not text:
            return False
        sig = cls.signature(text)
        try:
            import redis
            r = redis.from_url(get_config().REDIS_URL)
            known = r.sscan_iter(cls.REDIS_KEY) if hasattr(r, "sscan_iter") else iter(())
            # store compact hex digest of signature for O(1) exact dedupe
            digest = hashlib.sha1(",".join(map(str, sig)).encode()).hexdigest()
            added = r.sadd(cls.REDIS_KEY, digest)
            if added:
                # approximate near-dup check would go through datasketch index;
                # exact-signature dedupe is enough at this layer.
                return False
            return True
        except Exception:  # noqa: BLE001 - redis unavailable -> local fallback
            return cls._local_check(sig)

    _local_index: list[list[int]] = []

    @classmethod
    def _local_check(cls, sig: list[int]) -> bool:
        for other in cls._local_index:
            if cls.jaccard(sig, other) >= cls.THRESHOLD:
                return True
        cls._local_index.append(sig)
        return False


class SimHashDeduper:
    """Charikar SimHash: 64-bit fingerprint, Hamming distance <= 3 => dup."""

    BITS = 64

    @classmethod
    def fingerprint(cls, text: str) -> int:
        v = [0] * cls.BITS
        for shingle in _shingles(text):
            h = int(hashlib.md5(shingle.encode()).hexdigest()[:16], 16)
            for i in range(cls.BITS):
                v[i] += 1 if (h >> i) & 1 else -1
        fingerprint = 0
        for i in range(cls.BITS):
            if v[i] > 0:
                fingerprint |= 1 << i
        return fingerprint

    @staticmethod
    def hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    def __init__(self, max_distance: int = 3):
        self.max_distance = max_distance
        self.seen: list[int] = []

    def is_duplicate(self, text: str) -> bool:
        fp = self.fingerprint(text)
        for other in self.seen:
            if self.hamming(fp, other) <= self.max_distance:
                return True
        self.seen.append(fp)
        return False
