"""Resumable, checkpointed downloading — the boring half of the problem.

Every dataset this package consumes is large enough that a failed download is
a matter of *when*, not *if*: a 27 GB Wikipedia dump on a hotel connection, a
WET file that 404s halfway, a GitHub Actions runner killed at the six-hour
mark. So every download here

* writes to ``<name>.part`` and resumes with an HTTP ``Range`` request,
* retries transient failures with backoff,
* is a no-op once the final file exists with the expected size, and
* raises :class:`OfflineError` instead of touching a socket when
  ``WCP_OFFLINE=1`` (which the test suite sets).
"""

from __future__ import annotations

import bz2
import gzip
import io
import os
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; WebCrawlerPro/2.2; +own-search-index)")
CHUNK = 1 << 20  # 1 MiB


class OfflineError(RuntimeError):
    """Raised when a download is attempted while ``WCP_OFFLINE=1``."""


class DownloadError(RuntimeError):
    """Raised when a download cannot be completed (after retries)."""


def offline() -> bool:
    """True when the process must not open outbound sockets."""
    return os.getenv("WCP_OFFLINE", "").lower() in ("1", "true", "yes")


def _require_network() -> None:
    if offline():
        raise OfflineError("WCP_OFFLINE is set — refusing to download")


# --------------------------------------------------------------------------- #
# Transparent decompression
# --------------------------------------------------------------------------- #
def open_maybe_compressed(path: str | Path, mode: str = "rb") -> BinaryIO:
    """Open ``path``, decompressing ``.gz`` / ``.bz2`` / single-file ``.zip``.

    Returns a binary file object. For ``.zip`` the *first* member is used, which
    is what Tranco (``top-1m.csv.zip``) ships.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".gz":
        return gzip.open(path, mode)  # type: ignore[return-value]
    if suffix == ".bz2":
        return bz2.open(path, mode)  # type: ignore[return-value]
    if suffix == ".zip":
        archive = zipfile.ZipFile(path)
        names = archive.namelist()
        if not names:
            raise DownloadError(f"{path} is an empty zip archive")
        return archive.open(names[0])
    return path.open(mode)


def iter_lines(path: str | Path, *, encoding: str = "utf-8") -> Iterator[str]:
    """Yield decoded lines from a (possibly compressed) text file."""
    with open_maybe_compressed(path) as fh:
        wrapped = io.TextIOWrapper(fh, encoding=encoding, errors="replace",
                                   newline="")
        for line in wrapped:
            yield line.rstrip("\r\n")


# --------------------------------------------------------------------------- #
# Streaming download
# --------------------------------------------------------------------------- #
def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(30.0, 2.0 ** attempt))


def resumable_download(url: str, dest: str | Path, *,
                       expected_bytes: int | None = None,
                       chunk: int = CHUNK,
                       timeout: int = 60,
                       retries: int = 4,
                       headers: dict[str, str] | None = None,
                       fetcher: Callable[..., bytes] | None = None,
                       label: str = "") -> Path:
    """Download ``url`` to ``dest``, resuming and retrying as needed.

    Returns ``dest``. Raises :class:`OfflineError` under ``WCP_OFFLINE=1`` and
    :class:`DownloadError` if the server never satisfies the request.
    """
    _require_network()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    if dest.exists() and (expected_bytes is None
                          or dest.stat().st_size >= expected_bytes):
        return dest

    if fetcher is not None:  # test / offline injection
        data = fetcher(url)
        part.write_bytes(data)
        part.replace(dest)
        return dest

    import requests

    base_headers = {"User-Agent": DEFAULT_USER_AGENT}
    base_headers.update(headers or {})
    last_error: Exception | None = None

    for attempt in range(retries):
        done = part.stat().st_size if part.exists() else 0
        if expected_bytes is not None and done >= expected_bytes:
            part.replace(dest)
            return dest
        request_headers = dict(base_headers)
        if done:
            request_headers["Range"] = f"bytes={done}-"
        try:
            with requests.get(url, headers=request_headers, stream=True,
                              timeout=timeout) as resp:
                if resp.status_code == 416:        # range unsatisfiable
                    part.unlink(missing_ok=True)
                    continue
                if done and resp.status_code != 206:
                    # server ignored Range -> restart from scratch
                    done = 0
                    part.unlink(missing_ok=True)
                elif resp.status_code >= 400:
                    raise DownloadError(f"HTTP {resp.status_code} for {url}")
                mode = "ab" if done else "wb"
                with part.open(mode) as fh:
                    for block in resp.iter_content(chunk_size=chunk):
                        if block:
                            fh.write(block)
            if expected_bytes is not None and part.stat().st_size < expected_bytes:
                last_error = DownloadError(
                    f"{label or url}: truncated at {part.stat().st_size}/"
                    f"{expected_bytes} bytes")
                _sleep_backoff(attempt)
                continue
            part.replace(dest)
            return dest
        except DownloadError as exc:
            last_error = exc
            _sleep_backoff(attempt)
        except Exception as exc:  # noqa: BLE001 - network is unreliable by nature
            last_error = exc
            _sleep_backoff(attempt)

    raise DownloadError(f"{label or url}: giving up after {retries} attempts "
                        f"({last_error})")


def fetch_bytes(url: str, *, timeout: int = 60,
                headers: dict[str, str] | None = None,
                fetcher: Callable[..., bytes] | None = None) -> bytes:
    """Small whole-body GET, used for index/list files (``wet.paths.gz``)."""
    if fetcher is not None:
        return fetcher(url)
    _require_network()
    import requests

    request_headers = {"User-Agent": DEFAULT_USER_AGENT}
    request_headers.update(headers or {})
    resp = requests.get(url, headers=request_headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def human_bytes(n: float) -> str:
    """``28_000_000_000`` -> ``'26.1 GiB'``."""
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < step or unit == "PiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= step
    return f"{value:.1f} PiB"
