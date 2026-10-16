"""The optional C tokeniser must never change an answer.

Two layers of guarantee:

* The fallback path is tested unconditionally — the tokeniser has to behave
  exactly as it did before ``native.py`` existed, on any machine, built or not.
* When ``libwcpfast.so`` happens to be present (someone ran
  ``scripts/build-native.sh``) the C path is compared against the Python path
  on the awkward corpus and on randomised input. Machines without a compiler
  skip that half; CI builds it and runs it.
"""

from __future__ import annotations

import random

import pytest

from src.search_index import native
from src.search_index.text import analyze, iter_terms, stem, tokenize

AWKWARD = [
    "",
    " ",
    "solar panels",
    "The QUICK brown foxes are jumping",
    '"exact phrase" -blog site:.edu',
    "kubernetes orchestration containers containerized",
    "1234567 123456 abc1234567 000000000000",
    "energies energy energie energetic",
    "bus businesses busing",
    "x" * 300,  # longer than the C kernel's stack scratch
    "a" * 511,
    "a" * 512,
    "a" * 513,
    "a" * 4096,
    "UPPER lower MiXeD",
    "under_score snake_case __dunder__",
    "punctuation!!! ??? ... ---",
    "tab\tnewline\nseparated",
    "cafe naive",
    "সৌর প্যানেল গবেষণা",
    "أبحاث الطاقة الشمسية",
    "mixed সৌর and solar",
    "hyphen-ated dash—ed",
    "a an the and of",
    "ing ing ingly edly",
    "0" * 100,
    "version 1.2.3 release-4",
]


def _python_analyze(text: str, drop_stopwords: bool = True) -> list[str]:
    """Independent reimplementation used only by these tests."""
    from src.search_index.text import _TOKEN_RE, STOPWORDS

    out = []
    for tok in (t.lower() for t in _TOKEN_RE.findall(text)):
        if drop_stopwords and tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) > 6:
            continue
        out.append(stem(tok))
    return out


# --------------------------------------------------------------------------- #
# the path that always runs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", AWKWARD)
def test_tokenize_matches_reference(text):
    assert tokenize(text) == [t.lower() for t in _tokens(text)]


def _tokens(text: str):
    from src.search_index.text import _TOKEN_RE

    return _TOKEN_RE.findall(text)


@pytest.mark.parametrize("text", AWKWARD)
@pytest.mark.parametrize("drop_stopwords", [True, False])
def test_analyze_matches_reference(text, drop_stopwords):
    assert analyze(text, drop_stopwords=drop_stopwords) == _python_analyze(
        text, drop_stopwords
    )


@pytest.mark.parametrize("text", AWKWARD)
def test_iter_terms_emits_stem_and_surface(text):
    pairs = list(iter_terms(text))
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)
    # every surface form must be a lower-cased token from the text
    surfaces = {s for _, s in pairs}
    assert surfaces <= {t.lower() for t in _tokens(text)}
    # and every stem must be the stem of its own surface form
    assert all(a == stem(b) for a, b in pairs)


def test_ascii_only_input_is_still_correct_with_native_enabled(monkeypatch):
    """WCP_NATIVE=0 must force the Python path, and both paths must agree."""
    monkeypatch.setenv("WCP_NATIVE", "0")
    native._lib = None
    native._loaded = False
    assert native.available() is False
    for text in AWKWARD:
        assert analyze(text) == _python_analyze(text)


def test_missing_library_is_not_an_error(monkeypatch, tmp_path):
    """A build with no compiler simply does not have the file."""
    monkeypatch.delenv("WCP_NATIVE", raising=False)
    monkeypatch.setattr(native, "LIB_PATH", tmp_path / "nope.so")
    native._lib = None
    native._loaded = False
    assert native.available() is False
    assert native.analyze("solar panels") is None
    assert native.tokenize("solar panels") is None
    assert native.analyze_pairs("solar panels") is None
    # and the public functions still work through the fallback
    assert analyze("solar panels") == _python_analyze("solar panels")


# --------------------------------------------------------------------------- #
# the path that runs only when the library has been built
# --------------------------------------------------------------------------- #
pytestmark_built = pytest.mark.skipif(
    not native.LIB_PATH.exists(), reason="native library not built"
)


@pytestmark_built
def test_native_agrees_with_python_on_awkward_input():
    native._lib = None
    native._loaded = False
    assert native.available(), "library present but would not configure"
    for text in AWKWARD:
        assert analyze(text) == _python_analyze(text), repr(text)
        assert list(iter_terms(text)) == [
            (stem(t.lower()), t.lower())
            for t in _tokens(text)
            if t.lower() not in _stopwords() and not _long_digit(t.lower())
        ], repr(text)


@pytestmark_built
def test_native_declines_non_ascii():
    native._lib = None
    native._loaded = False
    assert native.tokenize("সৌর প্যানেল") is None
    assert native.analyze("café") is None
    assert native.analyze_pairs("naïve") is None
    # and the public functions quietly do the right thing anyway
    assert analyze("সৌর প্যানেল") == _python_analyze("সৌর প্যানেল")


@pytestmark_built
def test_native_agrees_with_python_on_random_ascii():
    native._lib = None
    native._loaded = False
    rng = random.Random(1234)
    alphabet = list(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ .,!?-"
    )
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
        assert tokenize(text) == [t.lower() for t in _tokens(text)], repr(text)
        assert analyze(text) == _python_analyze(text), repr(text)
        assert list(iter_terms(text)) == [
            (stem(t.lower()), t.lower())
            for t in _tokens(text)
            if t.lower() not in _stopwords() and not _long_digit(t.lower())
        ], repr(text)


def _stopwords():
    from src.search_index.text import STOPWORDS

    return STOPWORDS


def _long_digit(tok: str) -> bool:
    return tok.isdigit() and len(tok) > 6
