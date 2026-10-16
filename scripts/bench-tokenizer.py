#!/usr/bin/env python3
"""Verify and benchmark the optional C tokeniser.

Two jobs, in this order:

1. **Prove the two implementations agree.** Speed is worthless if it changes
   an answer. Every awkward input we can think of — punctuation runs, mixed
   case, long digit strings, suffix collisions, a 300-character token,
   Bengali and Arabic and accented Latin — is run through both paths and
   compared element by element.
2. **Measure.** Same corpus, both paths, best of N runs.

    python scripts/bench-tokenizer.py

Without the compiled library it reports the Python baseline and says so.
Build it first with ``scripts/build-native.sh``.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.search_index import native
from src.search_index.text import (
    _TOKEN_RE,
    STOPWORDS,
    analyze,
    iter_terms,
    stem,
    tokenize,
)


# --------------------------------------------------------------------------- #
# reference implementations — the pure-Python path, called directly so we can
# compare against it even while the C path is switched on.
# --------------------------------------------------------------------------- #
def py_tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def py_analyze(text: str, drop_stopwords: bool = True) -> list[str]:
    out: list[str] = []
    for tok in py_tokenize(text):
        if drop_stopwords and tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) > 6:
            continue
        out.append(stem(tok))
    return out


def py_pairs(text: str) -> list[tuple[str, str]]:
    out = []
    for tok in py_tokenize(text):
        if tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) > 6:
            continue
        out.append((stem(tok), tok))
    return out


# --------------------------------------------------------------------------- #
# the awkward corpus
# --------------------------------------------------------------------------- #
AWKWARD = [
    "",
    " ",
    "solar panels",
    "Solar PANELS and Photovoltaic Research",
    '"exact phrase" -blog site:.edu',
    "the who — to be or not to be",
    "kubernetes orchestration containers containerized",
    "1234567 123456 abc1234567 000000000000",
    "energies energy energie energetic",
    "bus businesses busing",
    "aaaaaaaaaa" + "s",
    "x" * 300,  # longer than the C kernel's buffer
    "a" * 254,
    "a" * 255,
    "a" * 256,  # must be skipped by C, kept by Python
    "UPPER lower MiXeD",
    "under_score snake_case __dunder__",
    "punctuation!!! ??? ... ---",
    "tab\tnewline\nseparated",
    "café naïve résumé",  # non-ASCII -> must fall back
    "সৌর প্যানেল গবেষণা",  # Bengali
    "أبحاث الطاقة الشمسية",  # Arabic
    "emoji 🕷️ spider",
    "mixed সৌর and solar",
    "trailing spaces   ",
    "   leading spaces",
    "hyphen-ated dash—ed",
    "stopword the and of",
    "stopwords only: a an the",
    "ing ing ingly edly",
    "unbelievable unbelievably",
    "0" * 100,
    "version 1.2.3 release-4",
]


def check_equivalence() -> tuple[int, list[str]]:
    """Compare the C path with the Python path on every awkward input."""
    failures: list[str] = []
    checked = 0

    for text in AWKWARD:
        checked += 1
        got_tok, want_tok = tokenize(text), py_tokenize(text)
        if got_tok != want_tok:
            failures.append(f"tokenize({text!r}): {got_tok} != {want_tok}")

        for drop in (True, False):
            got = analyze(text, drop_stopwords=drop)
            want = py_analyze(text, drop)
            if got != want:
                failures.append(f"analyze({text!r}, {drop}): {got} != {want}")

        got_pairs = list(iter_terms(text))
        want_pairs = py_pairs(text)
        if got_pairs != want_pairs:
            failures.append(f"iter_terms({text!r}): {got_pairs} != {want_pairs}")

    # plus a randomised sweep over ASCII noise
    rng = random.Random(20261016)
    alphabet = list(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ .,!?-"
    )
    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
        checked += 1
        if tokenize(text) != py_tokenize(text):
            failures.append(f"random tokenize: {text!r}")
        if analyze(text) != py_analyze(text):
            failures.append(f"random analyze: {text!r}")
        if list(iter_terms(text)) != py_pairs(text):
            failures.append(f"random iter_terms: {text!r}")

    return checked, failures


# --------------------------------------------------------------------------- #
# benchmark
# --------------------------------------------------------------------------- #
def make_corpus(n: int = 4000) -> list[str]:
    """A few thousand English-looking sentences of a realistic length."""
    words = [
        "solar",
        "photovoltaic",
        "panel",
        "installer",
        "research",
        "energy",
        "kubernetes",
        "docker",
        "container",
        "orchestration",
        "wikipedia",
        "curlie",
        "commoncrawl",
        "database",
        "benchmark",
        "tokenize",
        "stemming",
        "indexing",
        "the",
        "and",
        "of",
        "to",
        "in",
        "for",
        "with",
        "on",
        "at",
        "by",
        "from",
    ]
    rng = random.Random(7)
    return [
        " ".join(rng.choice(words) for _ in range(rng.randint(40, 120)))
        for _ in range(n)
    ]


def timeit(fn, corpus, repeats: int = 5) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        for text in corpus:
            fn(text)
        best = min(best, time.perf_counter() - t0)
    return best


def main() -> int:
    have_native = native.available()
    print("=" * 68)
    print("native tokeniser:", "PRESENT" if have_native else "absent (Python path)")
    print("=" * 68)

    checked, failures = check_equivalence()
    if failures:
        print(f"\nFAIL: {len(failures)} disagreement(s) out of {checked} inputs:")
        for f in failures[:10]:
            print("   ", f)
        return 1
    print(f"OK: {checked} inputs produce identical results on both paths")

    corpus = make_corpus()
    tokens = sum(len(t.split()) for t in corpus)
    print(f"\ncorpus: {len(corpus)} documents, ~{tokens:,} tokens\n")

    # py_analyze is the pure-Python reference, so this is a true baseline even
    # while the C path is switched on.
    py_s = timeit(lambda t: py_analyze(t), corpus)
    print(f"  pure Python   {py_s * 1000:8.1f} ms   {len(corpus) / py_s:9.0f} docs/s")

    c_s = None
    if have_native:
        c_s = timeit(lambda t: analyze(t), corpus)
        print(f"  C + ctypes    {c_s * 1000:8.1f} ms   {len(corpus) / c_s:9.0f} docs/s")
        print(f"  speedup       {py_s / c_s:8.2f} x")

    if c_s is None:
        print("\nBuild the native path to compare:")
        print("    ./scripts/build-native.sh && python scripts/bench-tokenizer.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
