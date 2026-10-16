"""The optional C fast path for :mod:`src.search_index.text`.

The tokeniser is the hottest loop in the indexer — it runs once per document,
and a Common Crawl snapshot is billions of documents. :file:`wcpfast.c` does
the same work in C; this module is the bridge.

Three properties matter more than the speed itself:

**It can never change an answer.** The C library handles ASCII only. The
moment a byte above 0x7F appears it declines the call and Python does the work.
Lowercasing in C matches Python's ``str.lower()`` only for ASCII, and
``\\w`` matches a Unicode set we are not going to reimplement — so we do not
try. Non-ASCII corpora (Bengali, Arabic, accented Latin) get the Unicode-correct
path and exactly the results they had before.

**It can never drift.** The stopword table and the suffix rules are handed to
the library from :mod:`src.search_index.text` at configure time. There is no
second copy of either list.

**It is genuinely optional.** No compiler, no library, or a library that
declines: every function here returns ``None`` and the caller falls back.
Nothing in the product depends on it being present.

Build it with ``scripts/build-native.sh``. Check it with
``scripts/bench-tokenizer.py``, which also asserts the two implementations
agree on a corpus of awkward inputs.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("wcp.search_index.native")

LIB_NAME = "libwcpfast.so"
LIB_PATH = Path(__file__).with_name(LIB_NAME)

# Sentinels returned by the C library instead of a byte count.
_NOT_ASCII = ctypes.c_size_t(-1).value
_TOO_SMALL = ctypes.c_size_t(-2).value

# Longest token the C kernel accepts; longer ones are skipped. The Python path
# has no such limit, which is another reason a decline is always safe.
MAX_TOKEN = 255

_lib: Any = None
_loaded = False

# One reusable output buffer per thread. Allocating a buffer the size of the
# document on every call was a measurable share of the remaining cost.
_local = threading.local()


def _out_buffer(cap: int) -> Any:
    buf = getattr(_local, "buf", None)
    if buf is None or len(buf) < cap:
        buf = ctypes.create_string_buffer(cap)
        _local.buf = buf
    return buf


def _load() -> Any:
    """Load the shared library once. Returns ``None`` for any reason at all."""
    global _lib, _loaded
    if _loaded:
        return _lib
    _loaded = True
    _lib = None

    if os.getenv("WCP_NATIVE", "1").lower() in ("0", "false", "no"):
        logger.debug("native tokeniser disabled by WCP_NATIVE=0")
        return None
    if not LIB_PATH.exists():
        logger.debug("native tokeniser not built (%s)", LIB_PATH)
        return None

    try:
        lib = ctypes.CDLL(str(LIB_PATH))
    except OSError as exc:
        logger.warning("native tokeniser present but unloadable (%s)", exc)
        return None

    lib.wcp_configure.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.wcp_configure.restype = ctypes.c_int
    lib.wcp_enabled.argtypes = []
    lib.wcp_enabled.restype = ctypes.c_int
    for name in ("wcp_tokenize", "wcp_analyze", "wcp_analyze_pairs"):
        fn = getattr(lib, name, None)
        if fn is None:
            logger.warning("native tokeniser is missing %s", name)
            return None
        fn.restype = ctypes.c_size_t
    lib.wcp_analyze.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    for name in ("wcp_tokenize", "wcp_analyze_pairs"):
        getattr(lib, name).argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

    _lib = lib
    return _lib


def _blob(parts) -> bytes:
    """NUL-separated, double-NUL-terminated — the shape ``wcp_configure`` wants."""
    return b"".join(p.encode("utf-8") + b"\0" for p in parts) + b"\0"


def configure(stopwords, rules) -> bool:
    """Hand the stopword set and the stemming rules to the library."""
    lib = _load()
    if lib is None:
        return False
    try:
        ok = lib.wcp_configure(
            _blob(sorted(stopwords)), _blob(f"{suf}={rep}" for suf, rep in rules)
        )
    except (OSError, AttributeError, UnicodeEncodeError) as exc:
        logger.warning("native tokeniser configuration failed (%s)", exc)
        return False
    return bool(ok)


def available() -> bool:
    """Is a configured native tokeniser ready to take calls?"""
    lib = _load()
    if lib is None:
        return False
    try:
        return bool(lib.wcp_enabled())
    except (OSError, AttributeError):
        return False


def _run(fn_name: str, text: str, *extra) -> list[str] | None:
    """Call one C entry point. ``None`` means 'not my job' — caller falls back."""
    lib = _load()
    if lib is None or not text:
        return None
    if len(text) > (1 << 30):  # absurd input: let Python handle it
        return None

    data = text.encode("utf-8")
    # Output can never be longer than the input, so this bound is exact.
    cap = len(data) + 2
    buf = _out_buffer(cap)
    try:
        fn = getattr(lib, fn_name)
        if extra:
            written = fn(data, len(data), buf, cap, *extra)
        else:
            written = fn(data, len(data), buf, cap)
    except (OSError, AttributeError, UnicodeEncodeError):
        return None

    if written == _NOT_ASCII or written == _TOO_SMALL or written >= cap:
        return None
    if written == 0:
        return []

    # One decode of the whole block, then one split. Decoding per token costs
    # more than the tokenising does on short documents.
    raw = ctypes.string_at(buf, written)
    try:
        text_out = raw.decode("ascii")
    except UnicodeDecodeError:  # cannot happen for ASCII input; be safe anyway
        return None
    parts = text_out.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def tokenize(text: str) -> list[str] | None:
    return _run("wcp_tokenize", text)


def analyze(text: str, drop_stopwords: bool = True) -> list[str] | None:
    return _run("wcp_analyze", text, 1 if drop_stopwords else 0)


def analyze_pairs(text: str) -> list[str] | None:
    """``[stem, surface, stem, surface, …]`` — flattened on purpose.

    Flattening avoids building a tuple per token in Python, which is exactly
    the overhead this module exists to remove.
    """
    return _run("wcp_analyze_pairs", text)
