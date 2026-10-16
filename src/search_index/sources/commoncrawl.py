"""Common Crawl WET files — the long tail, streamed one file at a time.

Curlie plus Wikipedia covers the *head* of the web beautifully and misses the
tail completely: a niche blog, a company site, a local newspaper. Common Crawl
is the only free corpus that covers it — ~2.17B pages per monthly crawl — and
its WET files are the extracted plain text, so we never touch HTML.

The arithmetic that makes this affordable (CC-MAIN-2026-39):

    WET files total 5.89 TiB compressed for 2.17B pages  =>  ~2.78 KB per page

so 10M pages is ~28 GB of download and 100M pages is ~280 GB. We never download
a whole crawl: ``wet.paths.gz`` lists ~72,000 WET files and a shard takes every
*Nth* one, streams it through a gzip decompressor as it arrives, and keeps only
pages that pass the same Gopher/C4 style rules the training pipeline uses.

Resumability is at *file* granularity: the builder records which WET paths it
has finished, so a runner killed at the six-hour mark restarts on the next file
and loses nothing.

Reuse: snapshot discovery and URL building come from
:mod:`src.ingestion.commoncrawl` (:func:`~src.ingestion.commoncrawl.list_snapshots`
and :func:`~src.ingestion.commoncrawl.cc_warc_url`), which already knew how to
talk to Common Crawl. What is new here is *streaming* — the existing
``WarcProcessor`` reads a whole file into memory, which is fine for a targeted
Range fetch of one record and impossible for a 100 MB WET file.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from src.ingestion.commoncrawl import cc_warc_url
from src.search_index.download import OfflineError, fetch_bytes
from src.search_index.models import Doc, host_of

logger = logging.getLogger(__name__)

CC_DATA_BASE = "https://data.commoncrawl.org"
WET_PATHS_TEMPLATE = "crawl-data/{snapshot}/wet.paths.gz"
READ_CHUNK = 1 << 20
MAX_HEADER_BYTES = 1 << 20


# --------------------------------------------------------------------------- #
# Byte-level plumbing: a buffered reader over an arbitrary chunk iterator
# --------------------------------------------------------------------------- #
class ChunkReader:
    """``read_until`` / ``read_exact`` over a stream of byte chunks.

    Lets the WARC parser below sit directly on top of an HTTP response body, a
    gzip stream, or a plain file without caring which.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._source = iter(chunks)
        self._buf = bytearray()
        self._pos = 0
        self._exhausted = False

    def _fill(self, need: int) -> bool:
        while len(self._buf) - self._pos < need:
            if self._exhausted:
                return False
            try:
                chunk = next(self._source)
            except StopIteration:
                self._exhausted = True
                return False
            if not chunk:
                continue
            if self._pos:
                del self._buf[: self._pos]
                self._pos = 0
            self._buf.extend(chunk)
        return True

    def read_until(self, separator: bytes, limit: int = MAX_HEADER_BYTES) -> bytes:
        """Return bytes up to *and including* ``separator`` (all leftovers at EOF).

        ``_fill`` reports failure when the source runs dry, but it may still
        have appended data on that last call — so the loop re-checks for the
        separator after every fill instead of bailing out on the return value.
        Getting this wrong silently swallowed all but the last WET record.
        """
        while True:
            index = self._buf.find(separator, self._pos)
            if index >= 0:
                end = index + len(separator)
                data = bytes(self._buf[self._pos:end])
                self._pos = end
                return data
            if len(self._buf) - self._pos > limit:
                break
            before = len(self._buf)
            self._fill(len(self._buf) - self._pos + READ_CHUNK)
            if len(self._buf) == before:      # nothing new arrived -> EOF
                break
        data = bytes(self._buf[self._pos:])
        self._pos = len(self._buf)
        return data

    def read_exact(self, count: int) -> bytes:
        """Return up to ``count`` bytes — fewer only when the stream ends."""
        if count <= 0:
            return b""
        self._fill(count)
        start = self._pos
        end = min(len(self._buf), self._pos + count)
        self._pos = end
        return bytes(self._buf[start:end])

    def at_eof(self) -> bool:
        return self._exhausted and self._pos >= len(self._buf)


def gunzip_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Decompress a (possibly multi-member) gzip byte stream incrementally.

    WET files are one gzip member, but WARC files are frequently concatenated
    members, so after each ``eof`` a fresh decompressor is started on whatever
    ``unused_data`` is left behind.
    """
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for chunk in chunks:
        if not chunk:
            continue
        pending = chunk
        while pending:
            if dec.eof:
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                out = dec.decompress(pending)
            except zlib.error:  # pragma: no cover - truncated download
                return
            if out:
                yield out
            pending = dec.unused_data if dec.eof else b""
    try:
        tail = dec.flush()
    except zlib.error:  # pragma: no cover
        return
    if tail:
        yield tail


# --------------------------------------------------------------------------- #
# WARC / WET records
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class WarcRecord:
    """The three fields we care about from a WET record."""

    record_type: str = ""
    target_uri: str = ""
    content_type: str = ""
    payload: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Decode a WET ``conversion`` payload (HTTP header block + text)."""
        head, sep, body = self.payload.partition(b"\r\n\r\n")
        raw = body if sep else head
        return raw.decode("utf-8", "replace")


def _parse_headers(block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.split(b"\r\n"):
        if not line or line.upper().startswith(b"WARC/"):
            continue
        key, sep, value = line.partition(b":")
        if sep:
            headers[key.decode("utf-8", "replace").strip()] = \
                value.decode("utf-8", "replace").strip()
    return headers


def iter_warc_records(chunks: Iterable[bytes], *,
                      max_records: int | None = None) -> Iterator[WarcRecord]:
    """Stream WARC/WET records out of (decompressed) bytes.

    Works for both WET (``conversion`` records carrying plain text) and plain
    WARC (``response`` records carrying raw HTTP). Consumes the stream lazily,
    so a 100 MB file never exists in memory at once.
    """
    reader = ChunkReader(chunks)
    emitted = 0
    while not reader.at_eof():
        header_block = reader.read_until(b"\r\n\r\n")
        if not header_block.strip():
            break
        headers = _parse_headers(header_block)
        try:
            length = int(headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        payload = reader.read_exact(length) if length else b""
        reader.read_until(b"\r\n\r\n")          # trailing record separator
        rec_type = headers.get("WARC-Type", "")
        if rec_type in ("", "warcinfo", "request", "metadata"):
            continue
        yield WarcRecord(record_type=rec_type,
                         target_uri=headers.get("WARC-Target-URI", ""),
                         content_type=headers.get("Content-Type", ""),
                         payload=payload, headers=headers)
        emitted += 1
        if max_records is not None and emitted >= max_records:
            return


def iter_records_from_file(path: str | Path, **kwargs) -> Iterator[WarcRecord]:
    """Read a local ``.warc`` / ``.warc.gz`` / ``.wet`` / ``.wet.gz`` file."""
    def _chunks() -> Iterator[bytes]:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(READ_CHUNK)
                if not block:
                    return
                yield block

    if str(path).endswith(".gz"):
        yield from iter_warc_records(gunzip_chunks(_chunks()), **kwargs)
    else:
        yield from iter_warc_records(_chunks(), **kwargs)


# --------------------------------------------------------------------------- #
# Snapshot / path discovery
# --------------------------------------------------------------------------- #
def wet_paths_url(snapshot: str) -> str:
    return cc_warc_url(WET_PATHS_TEMPLATE.format(snapshot=snapshot))


def list_wet_paths(snapshot: str, *, fetcher=None) -> list[str]:
    """Fetch ``wet.paths.gz`` for ``snapshot`` and return the WET object keys."""
    import gzip

    raw = fetch_bytes(wet_paths_url(snapshot), fetcher=fetcher)
    text = gzip.decompress(raw).decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def latest_snapshot(fetcher=None) -> str:
    """Name of the newest ``CC-MAIN-*`` crawl (e.g. ``CC-MAIN-2026-39``)."""
    from src.ingestion.commoncrawl import list_snapshots

    snapshots = list_snapshots(fetcher=fetcher)
    if not snapshots:
        raise RuntimeError("no CC-MAIN snapshots discovered")
    return max(s.name for s in snapshots)


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #
class CommonCrawlSource:
    """Streams :class:`Doc` objects out of Common Crawl WET files.

    Sharding is by file index (``shard_index`` of ``shard_count``) rather than
    by per-file byte ranges, because WET files are small enough (~80 MB each)
    that whole-file granularity is fine and it keeps resume bookkeeping to a
    list of names.
    """

    def __init__(self, snapshot: str | None = None, *, fetcher=None,
                 timeout: int = 120, min_text_chars: int = 400,
                 downloader=None) -> None:
        self.snapshot = snapshot or ""
        self._fetcher = fetcher
        self._timeout = timeout
        self.min_text_chars = int(min_text_chars)
        self._downloader = downloader
        self._paths: list[str] | None = None

    # -- discovery ----------------------------------------------------------- #
    def snapshot_name(self) -> str:
        if not self.snapshot:
            self.snapshot = latest_snapshot(fetcher=self._fetcher)
        return self.snapshot

    def paths(self) -> list[str]:
        if self._paths is None:
            self._paths = list_wet_paths(self.snapshot_name(), fetcher=self._fetcher)
        return self._paths

    def select_paths(self, shard_index: int = 0, shard_count: int = 1,
                     skip: set[str] | None = None) -> list[str]:
        """This shard's slice of the WET file list, minus already-done files."""
        selected = [p for i, p in enumerate(self.paths())
                    if shard_count <= 1 or i % shard_count == shard_index]
        if skip:
            selected = [p for p in selected if p not in skip]
        return selected

    # -- reading -------------------------------------------------------------- #
    def _stream(self, url: str) -> Iterable[bytes]:
        """Raw (still gzipped) bytes for one WET object."""
        if self._downloader is not None:   # test / local-cache injection
            data = self._downloader(url)
            if isinstance(data, (bytes, bytearray)):
                yield bytes(data)
                return
            yield from data  # already an iterator of chunks
            return
        from src.search_index.download import offline

        if offline():
            raise OfflineError("WCP_OFFLINE is set — refusing to stream WET files")
        import requests

        with requests.get(url, stream=True, timeout=self._timeout) as resp:
            resp.raise_for_status()
            yield from resp.iter_content(chunk_size=READ_CHUNK)

    def iter_file(self, path_key: str, *, max_pages: int | None = None
                  ) -> Iterator[Doc]:
        """Yield :class:`Doc` for every usable page in one WET file."""
        url = cc_warc_url(path_key)
        emitted = 0
        for record in iter_warc_records(gunzip_chunks(self._stream(url))):
            if record.record_type not in ("conversion", "response"):
                continue
            text = record.text.strip()
            if len(text) < self.min_text_chars:
                continue
            target = record.target_uri
            if not target.startswith(("http://", "https://")):
                continue
            host = host_of(target)
            if not host:
                continue
            yield Doc(url=target, title=_title_from_text(text, host),
                      snippet=text[:300], body=text[:20_000],
                      source="commoncrawl", lang="", host=host)
            emitted += 1
            if max_pages is not None and emitted >= max_pages:
                return

    def iter_docs(self, *, shard_index: int = 0, shard_count: int = 1,
                  max_pages: int | None = None,
                  skip: set[str] | None = None,
                  progress=None) -> Iterator[Doc]:
        """Yield documents across this shard's WET files."""
        emitted = 0
        for path_key in self.select_paths(shard_index, shard_count, skip):
            if progress:
                progress(path_key)
            try:
                for doc in self.iter_file(path_key):
                    yield doc
                    emitted += 1
                    if max_pages is not None and emitted >= max_pages:
                        return
            except Exception as exc:  # noqa: BLE001 - one bad file is not fatal
                logger.warning("WET file %s failed: %s", path_key, exc)


def _title_from_text(text: str, host: str) -> str:
    """Best-effort title for a page we only have the text of.

    WET records carry no ``<title>``, so we take the first sentence of the
    first line when it is short enough to be a headline and fall back to the
    host otherwise — a 500-character paragraph makes a useless title.
    """
    head = (text or "").strip().split("\n", 1)[0].strip()
    if not head:
        return host
    sentence = head.split(". ")[0].strip()
    candidate = sentence if 3 <= len(sentence) <= 120 else head[:120].strip()
    return candidate if 3 <= len(candidate) <= 120 else host


def wet_url(path_key: str) -> str:
    """Public HTTPS URL of a WET object (no AWS credentials, no egress fee)."""
    return cc_warc_url(path_key)
