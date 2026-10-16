"""Query understanding: tokenising, stemming, query syntax and snippets.

Deliberately small and dependency-free. A general web engine needs spelling
correction, synonyms and a learned ranker; ours needs to match a *topic phrase*
against a *category path* and a *page description*, which a decent BM25 with a
light stemmer handles fine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

# ``\w`` with re.UNICODE matches letters in any script, so a Bengali or Arabic
# query tokenises correctly instead of vanishing.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# The usual suspects. Kept small on purpose: over-aggressive stopword lists
# destroy queries like "the who" or "to be or not to be".
STOPWORDS = frozenset(["a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during", "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself", "let's", "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", "that", "that's", "the", "their", "theirs", "them", "themselves", "then", "there", "there's", "these", "they", "they'd", "they'll", "they're", "they've", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't", "what", "what's", "when", "when's", "where", "where's", "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves"])

_SUFFIXES = (
    ("ies", "y"), ("ing", ""), ("ings", ""), ("ers", ""), ("er", ""),
    ("est", ""), ("edly", ""), ("ly", ""), ("ed", ""), ("es", ""), ("s", ""),
)

# The optional C fast path. Configured from the constants above rather than
# carrying its own copy of them, and used only for ASCII input — see
# src/search_index/native.py for the reasoning.
try:  # pragma: no cover - exercised via scripts/bench-tokenizer.py
    from src.search_index import native as _native

    _NATIVE = _native.configure(STOPWORDS, _SUFFIXES)
except Exception:  # noqa: BLE001 - a missing .so must never break indexing
    _native = None  # type: ignore[assignment]
    _NATIVE = False


def tokenize(text: str) -> list[str]:
    """Split ``text`` into lower-cased word tokens."""
    if not text:
        return []
    if _NATIVE:
        fast = _native.tokenize(text)
        if fast is not None:
            return fast
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def stem(token: str) -> str:
    """A very light suffix stripper — enough to match ``energy``/``energies``.

    A full Porter stemmer is ~200 lines and mangles non-English words; ours
    only fires when the remaining stem is at least 4 characters, so it cannot
    turn ``bus`` into ``bu``.
    """
    if len(token) < 5:
        return token
    for suffix, replacement in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)] + replacement
    return token


def iter_terms(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(stem, surface)`` pairs — the index term and the word as typed.

    :func:`analyze` throws the surface form away; anything that has to *show*
    a term to a human (the spelling dictionary, notably) needs it back, and
    re-deriving it later means tokenising the corpus a second time.
    """
    if _NATIVE:
        flat = _native.analyze_pairs(text)
        if flat is not None:
            for i in range(0, len(flat) - 1, 2):
                yield flat[i], flat[i + 1]
            return
    for tok in tokenize(text):
        if tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) > 6:   # checksums / ids, not words
            continue
        yield stem(tok), tok


def analyze(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Tokenise + stem, optionally dropping stopwords."""
    if _NATIVE:
        fast = _native.analyze(text, drop_stopwords)
        if fast is not None:
            return fast
    out: list[str] = []
    for tok in tokenize(text):
        if drop_stopwords and tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) > 6:   # checksums / ids, not words
            continue
        out.append(stem(tok))
    return out


# --------------------------------------------------------------------------- #
# Query syntax
# --------------------------------------------------------------------------- #
_PHRASE_RE = re.compile(r'"([^"]+)"')
_SITE_RE = re.compile(r"(?:^|\s)(?:site|host):(\S+)", re.IGNORECASE)
_MINUS_RE = re.compile(r"(?:^|\s)-(\S+)")
_SOURCE_RE = re.compile(r"(?:^|\s)(?:source|in):(\S+)", re.IGNORECASE)


@dataclass(slots=True)
class ParsedQuery:
    """A user query broken into the pieces the index can act on."""

    raw: str
    terms: list[str] = field(default_factory=list)      # stemmed, for BM25
    phrases: list[list[str]] = field(default_factory=list)  # stemmed, positional
    excludes: list[str] = field(default_factory=list)
    site: str = ""
    source: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.terms or self.phrases)

    def all_terms(self) -> list[str]:
        """Every stem we want the BM25 scorer to look up (phrases included)."""
        out = list(self.terms)
        for phrase in self.phrases:
            out.extend(phrase)
        return out


def parse_query(raw: str) -> ParsedQuery:
    """Parse ``"renewable energy" -blog site:.edu source:wikipedia``."""
    raw = (raw or "").strip()
    parsed = ParsedQuery(raw=raw)

    site_m = _SITE_RE.search(raw)
    if site_m:
        parsed.site = site_m.group(1).strip().lower()
    source_m = _SOURCE_RE.search(raw)
    if source_m:
        parsed.source = source_m.group(1).strip().lower()

    body = _SITE_RE.sub(" ", raw)
    body = _SOURCE_RE.sub(" ", body)

    for match in _PHRASE_RE.finditer(body):
        toks = analyze(match.group(1))
        if toks:
            parsed.phrases.append(toks)
    body = _PHRASE_RE.sub(" ", body)

    for match in _MINUS_RE.finditer(body):
        parsed.excludes.extend(analyze(match.group(1)))
    body = _MINUS_RE.sub(" ", body)

    parsed.terms = analyze(body)
    # de-duplicate, keep order
    seen: set[str] = set()
    parsed.terms = [t for t in parsed.terms if not (t in seen or seen.add(t))]
    return parsed


# --------------------------------------------------------------------------- #
# Snippets
# --------------------------------------------------------------------------- #
def best_window(text: str, terms: list[str], width: int = 240) -> str:
    """Return the ``width``-character window of ``text`` containing most terms."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    toks = tokenize(text)
    if not toks:
        return text[:width]
    stems = [stem(t) for t in toks]
    wanted = {t for t in terms if t}
    if not wanted:
        return text[:width]

    # Positions in *character* space, so we can slice the original string.
    # ``tokenize`` normalises (lower-cases, splits on punctuation), so a token
    # is not guaranteed to be findable in the raw text; when that happens we
    # fall back to the running offset instead of dropping the token — the
    # spans must stay the same length as ``stems`` or the window maths below
    # walks off the end.
    spans: list[tuple[int, int]] = []
    idx = 0
    for tok in toks:
        pos = text.find(tok, idx)
        end = (pos + len(tok)) if pos >= 0 else min(len(text), idx + len(tok))
        if pos < 0:
            pos = min(idx, len(text))
        spans.append((pos, end))
        idx = end

    hits = [i for i, s in enumerate(stems) if s in wanted]
    if not hits:
        return text[:width].rstrip() + "…"

    # slide a window over token hits, pick the densest
    best_start, best_score = 0, -1.0
    for i, start in enumerate(hits):
        end_char = spans[start][0] + width
        covered = 0
        for j in range(i, len(hits)):
            if spans[hits[j]][1] > end_char:
                break
            covered += 1
        score = covered - 0.01 * start
        if score > best_score:
            best_score, best_start = score, start

    start_char = spans[best_start][0]
    # back up to a word boundary
    if start_char > 0:
        space = text.rfind(" ", max(0, start_char - 30), start_char)
        start_char = space + 1 if space > 0 else start_char
    chunk = text[start_char:start_char + width]
    return ("…" if start_char > 0 else "") + chunk.rstrip() + (
        "…" if start_char + width < len(text) else "")


def highlight(text: str, terms: list[str], width: int = 240) -> str:
    """``best_window`` with ``[term]`` brackets around matches (for terminal/JSON)."""
    window = best_window(text, terms, width)
    if not terms:
        return window
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\w*",
                         re.IGNORECASE | re.UNICODE)
    return pattern.sub(lambda m: f"[{m.group(0)}]", window)
