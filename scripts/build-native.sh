#!/usr/bin/env bash
# --------------------------------------------------------------------------- #
# Build the optional C fast path for the search-index tokeniser.
#
#   ./scripts/build-native.sh             # build
#   ./scripts/build-native.sh --clean     # remove the built library
#
# Nothing depends on this. Without it the tokeniser takes the pure-Python path
# and every result is identical — just slower over very large corpora. Build
# it when you are indexing Common Crawl; skip it when you are not.
#
# It compiles a plain shared library with no Python headers, so it works on
# slim images that ship Python without python3-dev, and the same .so loads
# under any CPython 3.x.
#
# Why C and not Rust or Go: this is a single ~280-line kernel that has to be
# callable from a process that is already Python. A Rust version (PyO3) or a
# Go version (c-shared) would be a drop-in for wcpfast.c and would need that
# toolchain on the build host — the benchmark in scripts/bench-tokenizer.py
# measures whichever one is present.
# --------------------------------------------------------------------------- #
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="src/search_index/wcpfast.c"
OUT="src/search_index/libwcpfast.so"

if [[ "${1:-}" == "--clean" ]]; then
  rm -f "$OUT"
  echo "removed $OUT"
  exit 0
fi

CC_BIN="${CC:-}"
if [[ -z "$CC_BIN" ]]; then
  CC_BIN="$(command -v cc || command -v gcc || command -v clang || true)"
fi
if [[ -z "$CC_BIN" ]]; then
  echo "no C compiler found; skipping the optional native extension" >&2
  exit 0
fi

echo "building $OUT with $CC_BIN"
"$CC_BIN" -O3 -fPIC -shared -std=c99 -Wall -Wextra \
  -fno-strict-aliasing "$SRC" -o "$OUT"

"${PYTHON:-python3}" - <<'PY'
import ctypes, pathlib
lib = ctypes.CDLL(str(pathlib.Path("src/search_index/libwcpfast.so").resolve()))
print("libwcpfast loaded OK; wcp_enabled() =", lib.wcp_enabled())
PY
