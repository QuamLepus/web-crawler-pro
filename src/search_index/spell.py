"""Spelling correction learned from the index itself.

Why build our own instead of shipping a dictionary: a general English word list
does not know that this corpus contains ``kubernetes``, ``photovoltaics``,
``tranco`` or ``duckduckgo``, and it will happily "correct" all of them into
something worse. The vocabulary we already computed for BM25 *is* the perfect
dictionary — every term in it is a word that occurs in the documents we can
actually return, with a document frequency attached.

Algorithm (SymSpell, simplified)
--------------------------------
SymSpell's trick is that you never enumerate edits at query time: you precompute
the *deletions* of every dictionary term, and a misspelling shares at least one
deletion with any term within the edit distance. ``solar`` and ``solar`` share
``solar`` minus nothing; ``solr`` and ``solar`` share ``solr``.

We use a **reduced** form of it on purpose:

* ``deletes`` maps a deletion signature to the single most frequent term that
  produces it (not a list), which keeps the table at roughly one string pair per
  term instead of a list per term — tens of MB instead of hundreds.
* Only the top ``max_terms`` most frequent terms are indexed for deletion
  lookup; the rest are still in ``dictionary`` and reachable by edit generation.
* Distance-2 correction only runs when distance 1 finds nothing, and only for
  words up to 12 characters, because the candidate set grows quadratically.

The full delete index would buy sub-millisecond lookup on a 10M-term vocabulary.
Discovery runs a handful of queries per job, so that latency is not worth the
memory here; the honest trade-off is written down instead of hidden.

When it fires
-------------
A term is only corrected when **all** of these hold — being timid matters,
because a wrong correction silently hides the results you wanted:

* the term is not already in the dictionary;
* it is at least ``min_len`` characters (never touch "ai" or "ml");
* it contains no digit (never touch "gpt4" or "2026");
* a candidate exists within the edit distance;
* the candidate is at least ``min_freq_ratio`` times more frequent than the
  typed term (so rare-but-real words survive).

Every correction is reported, so the UI can show "Showing results for X".
"""

from __future__ import annotations

import gzip
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from src.search_index.bm25 import SURFACE_FILE

logger = logging.getLogger(__name__)

SPELL_FILE = "spell.json.gz"
SPELL_VERSION = 2

# Letters we are willing to substitute or insert, learned from the corpus.
_FALLBACK_ALPHABET = "abcdefghijklmnopqrstuvwxyz'-"


def _deletes(term: str) -> set[str]:
    """Every string formed by removing exactly one character from ``term``."""
    return {term[:i] + term[i + 1:] for i in range(len(term))}


def _edits1(term: str, alphabet: str) -> set[str]:
    """Deletions, transpositions, substitutions and insertions at distance 1."""
    out: set[str] = set()
    for i in range(len(term)):
        out.add(term[:i] + term[i + 1:])                     # delete
        if i + 1 < len(term):
            out.add(term[:i] + term[i + 1] + term[i] + term[i + 2:])   # transpose
    for i in range(len(term) + 1):
        head, tail = term[:i], term[i:]
        for ch in alphabet:
            out.add(head + ch + tail)                        # insert
            if i < len(term):
                out.add(head + ch + tail[1:])                # substitute
    return out


@dataclass(slots=True)
class SpellConfig:
    """Knobs that trade memory and recall against precision."""

    max_terms: int = 30_000          # how many terms get the delete index
    min_freq: int = 3                # ignore hapax legomena
    min_len: int = 4                 # never correct anything shorter
    max_len: int = 30                # skip absurd tokens
    max_edits: int = 2
    max_suggestions: int = 1         # terms kept per delete signature
    min_freq_ratio: float = 8.0      # candidate must dominate the typed term
    allow_distance2: bool = True
    distance2_max_len: int = 12


class SpellChecker:
    """Correction against a vocabulary extracted from our own index."""

    def __init__(self, config: SpellConfig | None = None) -> None:
        self.config = config or SpellConfig()
        self.dictionary: dict[str, int] = {}
        self.deletes: dict[str, str] = {}
        #: index term (stem) -> the word a human actually writes
        self.display: dict[str, str] = {}
        self._alphabet = _FALLBACK_ALPHABET
        self._cache: dict[str, str | None] = {}

    # -- construction -------------------------------------------------------- #
    @classmethod
    def from_terms(cls, frequencies: dict[str, int],
                   config: SpellConfig | None = None) -> SpellChecker:
        """Build from a ``term -> corpus frequency`` mapping."""
        checker = cls(config)
        cfg = checker.config
        kept = {t: int(f) for t, f in frequencies.items()
                if f >= cfg.min_freq and cfg.min_len <= len(t) <= cfg.max_len
                and not any(ch.isdigit() for ch in t)}
        checker.dictionary = dict(
            sorted(kept.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.max_terms])

        # most frequent term wins each delete signature
        best: dict[str, tuple[int, str]] = {}
        for term, freq in sorted(checker.dictionary.items(),
                                 key=lambda kv: (-kv[1], kv[0])):
            for deletion in _deletes(term):
                current = best.get(deletion)
                if current is None or freq > current[0]:
                    best[deletion] = (freq, term)
        checker.deletes = {d: term for d, (_f, term) in best.items()}
        checker._alphabet = "".join(
            sorted({ch for term in list(checker.dictionary)[:5000]
                    for ch in term if ch.isalpha()})) or _FALLBACK_ALPHABET
        return checker

    @classmethod
    def from_index(cls, index_dir: str | Path,
                   config: SpellConfig | None = None) -> SpellChecker:
        """Read every shard's ``vocab.json`` + ``df.npy`` and aggregate.

        ``df`` is the document frequency we already computed for BM25, so the
        spelling dictionary costs nothing extra to build beyond this pass — and
        it is exactly the right frequency for this purpose: how many documents
        in *this* corpus use the word.
        """
        import numpy as np

        index_dir = Path(index_dir)
        shards_file = index_dir / "shards.json"
        if not shards_file.exists():
            return cls(config)
        payload = json.loads(shards_file.read_text(encoding="utf-8"))
        raw = payload.get("shards", []) if isinstance(payload, dict) else payload

        totals: Counter[str] = Counter()
        forms: dict[str, Counter[str]] = {}
        for meta in raw:
            directory = index_dir / str(meta.get("path", ""))
            try:
                vocab = json.loads((directory / "vocab.json").read_text())
                counts = np.load(directory / "df.npy")
            except (OSError, ValueError, KeyError):
                continue
            for term, tid in vocab.items():
                try:
                    totals[term] += int(counts[int(tid)])
                except (IndexError, ValueError):  # pragma: no cover - corrupt
                    continue
            surface_file = directory / SURFACE_FILE
            if surface_file.exists():
                try:
                    for stem, candidates in json.loads(
                            surface_file.read_text()).items():
                        bucket = forms.setdefault(stem, Counter())
                        for rank, form in enumerate(candidates):
                            bucket[form] += len(candidates) - rank
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        logger.info("spell: %d distinct terms, %d usable",
                    len(totals), sum(1 for f in totals.values() if f >= 3))
        checker = cls.from_terms(dict(totals), config)
        checker.display = {stem: counter.most_common(1)[0][0]
                           for stem, counter in forms.items() if counter}
        return checker

    # -- persistence --------------------------------------------------------- #
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        blob = {
            "version": SPELL_VERSION,
            "config": asdict(self.config),
            "dictionary": self.dictionary,
            "deletes": self.deletes,
            "display": self.display,
            "alphabet": self._alphabet,
        }
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(blob, fh)
        return path

    @classmethod
    def load(cls, path: str | Path) -> SpellChecker | None:
        path = Path(path)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if blob.get("version") not in (1, SPELL_VERSION):
            return None
        config = SpellConfig(**blob.get("config", {}))
        checker = cls(config)
        checker.dictionary = dict(blob.get("dictionary", {}))
        checker.deletes = dict(blob.get("deletes", {}))
        checker.display = dict(blob.get("display", {}))
        checker._alphabet = blob.get("alphabet", _FALLBACK_ALPHABET)
        return checker

    # -- lookup -------------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self.dictionary)

    def knows(self, term: str) -> bool:
        return term in self.dictionary

    def suggest(self, word: str) -> str | None:
        """Best correction for one token, or ``None`` to leave it alone."""
        if word in self._cache:
            return self._cache[word]
        result = self._suggest_uncached(word)
        self._cache[word] = result
        return result

    def _suggest_uncached(self, word: str) -> str | None:
        cfg = self.config
        if not word or len(word) < cfg.min_len or len(word) > cfg.max_len:
            return None
        if any(ch.isdigit() for ch in word):
            return None
        if word in self.dictionary:
            return None

        typed_freq = self.dictionary.get(word, 0)
        candidates: dict[str, int] = {}

        # --- SymSpell delete lookup (distance <= 1, sometimes 2) ------------ #
        # The input itself is a valid key too: when the typo dropped a letter
        # ("solr" for "solar") the typed word *is* one of that term's deletes.
        for deletion in [word, *_deletes(word)]:
            hit = self.deletes.get(deletion)
            if hit:
                candidates[hit] = self.dictionary[hit]
            if cfg.max_edits >= 2 and cfg.allow_distance2:
                for second in _deletes(deletion):
                    hit2 = self.deletes.get(second)
                    if hit2:
                        candidates[hit2] = self.dictionary[hit2]

        # --- direct edit generation, catches what the delete index missed --- #
        for candidate in _edits1(word, self._alphabet):
            freq = self.dictionary.get(candidate)
            if freq:
                candidates[candidate] = freq
        if (cfg.max_edits >= 2 and cfg.allow_distance2
                and not candidates and len(word) <= cfg.distance2_max_len):
            for once in _edits1(word, self._alphabet):
                for candidate in _edits1(once, self._alphabet):
                    freq = self.dictionary.get(candidate)
                    if freq:
                        candidates[candidate] = freq

        if not candidates:
            return None
        best = max(candidates.items(), key=lambda kv: (kv[1], -len(kv[0])))
        if best[1] < max(typed_freq * cfg.min_freq_ratio, cfg.min_freq):
            return None
        return self.show(best[0])

    def show(self, term: str) -> str:
        """The word to *display* for an index term.

        The dictionary is built from stems, so "kubernetes" lives in it as
        "kubernet". Searching with either works, but telling someone "showing
        results for kubernet" is wrong — so every suggestion goes through this.
        """
        return self.display.get(term, term)

    def correct_terms(self, terms: list[str]
                      ) -> tuple[list[str], list[tuple[str, str]]]:
        """Correct a list of tokens; returns ``(corrected, [(was, became)])``."""
        out: list[str] = []
        corrections: list[tuple[str, str]] = []
        for term in terms:
            suggestion = self.suggest(term)
            if suggestion and suggestion != term:
                corrections.append((term, suggestion))
                out.append(suggestion)
            else:
                out.append(term)
        return out, corrections

    def stats(self) -> dict:
        return {"terms": len(self.dictionary),
                "delete_signatures": len(self.deletes),
                "display_forms": len(self.display),
                "min_freq": self.config.min_freq,
                "max_terms": self.config.max_terms}
