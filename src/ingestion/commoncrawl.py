"""Common Crawl ingestion: index queries + WARC/WET/WAT processing.

Common Crawl publishes ~monthly crawls of the entire web as compressed
WARC (raw records), WET (extracted plain text) and WAT (metadata-only JSON)
files on S3 (`s3://commoncrawl`) and HTTPS (`https://data.commoncrawl.org`).

This module provides:

* ``CCSnapshot`` / ``list_snapshots``  - discover available crawl snapshots
  by parsing CC's ``collisions/cc-index/collections/*/indexes/*.txt`` files.
* ``query_cc_index``                   - column-restricted index search that
  returns matching WARC offsets (the classic "fetch only what you need" trick).
* ``WarcProcessor``                    - streaming WARC/WET/WAT reader built on
  ``warcio`` with a pure-python gzip fallback so unit tests run anywhere.
* ``iter_snapshot_records``            - high-level generator: given a snapshot
  prefix list, stream normalized document dicts ready for the cleaning pipeline.

All network access is funneled through small injectable helpers so the code is
fully testable offline (see ``tests/test_ingestion.py``).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

CC_S3_BASE = "https://data.commoncrawl.org"
CC_INDEX_BASE = "https://data.commoncrawl.org"
DEFAULT_COLUMNS = (
    "url,domain,content_type,timestamp,warc_filename,warc_record_offset,warc_length"
)


# --------------------------------------------------------------------------- #
# Snapshot discovery
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CCSnapshot:
    """A single Common Crawl crawl snapshot (e.g. ``CC-MAIN-2024-10``)."""

    name: str
    index_files: tuple[str, ...] = ()
    segment_count: int = 0

    def warc_prefix(self, segment: int) -> str:
        """Return the key prefix of one crawl segment's WARC files."""
        return f"crawl-data/{self.name}/warcs/segments/{segment:09d}"


def parse_collections_file(raw: str) -> list[CCSnapshot]:
    """Parse ``collisions/cc-index/collections/collections.txt``.

    The file lists one collection name per line; lines may be blank or
    contain trailing whitespace.  We keep only ``CC-MAIN-*`` entries which
    are the full web crawls (news/companies crawls use other prefixes).
    """
    out: list[CCSnapshot] = []
    for line in raw.splitlines():
        name = line.strip()
        if not name or not name.startswith("CC-MAIN-"):
            continue
        out.append(CCSnapshot(name=name))
    return out


def parse_segment_list(raw: str) -> list[str]:
    """Parse a CC ``indexes/*-segmented_urls_in_crawl.txt`` listing.

    Each line is a tab-separated record whose *last two* fields carry the
    number of URLs in the segment and the byte length of the segment index
    file.  We return the segment index filenames themselves.
    """
    segments: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        # Format: <segment-file-name>\t<num_urls>\t<bytes>
        candidate = parts[0]
        if candidate.endswith(".txt.gz") or re.match(r".*-urls_in_crawl_\d+\.txt\.gz", candidate):
            segments.append(candidate)
    return segments


def build_snapshot(collections_raw: str, segments_raw_by_name: dict[str, str]) -> list[CCSnapshot]:
    """Combine collections + per-collection segment listings into snapshots."""
    snaps: list[CCSnapshot] = []
    for snap in parse_collections_file(collections_raw):
        segs = parse_segment_list(segments_raw_by_name.get(snap.name, ""))
        snaps.append(
            CCSnapshot(name=snap.name, index_files=tuple(segs), segment_count=len(segs))
        )
    return snaps


def list_snapshots(fetcher: Callable[[str], bytes] | None = None) -> list[CCSnapshot]:
    """Discover all public CC-MAIN snapshots.

    ``fetcher`` is injectable for testing; production uses an HTTP GET.
    """
    fetcher = fetcher or _default_fetcher
    base = f"{CC_INDEX_BASE}/collisions/cc-index/collections/"
    collections_raw = fetcher(base + "collections.txt").decode("utf-8", "replace")
    result: list[CCSnapshot] = []
    for snap in parse_collections_file(collections_raw):
        idx_base = f"{base}{snap.name}/indexes/"
        try:
            listing = fetcher(idx_base + f"{snap.name}-segmented_urls_in_crawl.txt.gz")
            segments_raw = gzip.decompress(listing).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - best-effort discovery
            logger.warning("snapshot %s: cannot read index listing: %s", snap.name, exc)
            continue
        segs = parse_segment_list(segments_raw)
        result.append(CCSnapshot(name=snap.name, index_files=tuple(segs), segment_count=len(segs)))
    return result


# --------------------------------------------------------------------------- #
# Column-restricted index query
# --------------------------------------------------------------------------- #
@dataclass
class IndexHit:
    """One row returned by a CC index query, mapped to its WARC location."""

    url: str
    domain: str
    content_type: str
    timestamp: str
    warc_filename: str
    warc_offset: int
    warc_length: int

    @property
    def http_url(self) -> str:
        return f"{CC_S3_BASE}/{self.warc_filename}"

    @property
    def s3_uri(self) -> str:
        return f"s3://commoncrawl/{self.warc_filename}"


_HIT_RE = re.compile(
    r"^(?P<url>\S+)\t(?P<domain>\S*)\t(?P<ctype>[^/]*/[^;/]*)(?:;[^/]*)?/(?P<ts>\d{14})"
    r"\t(?P<warc>\S+)\t(?P<offset>\d+)\t(?P<length>\d+)"
)


def parse_index_line(line: str) -> IndexHit | None:
    """Parse one TSV row produced by a column-restricted index query."""
    line = line.rstrip("\n")
    m = _HIT_RE.match(line)
    if not m:
        return None
    return IndexHit(
        url=m.group("url"),
        domain=m.group("domain"),
        content_type=m.group("ctype"),
        timestamp=m.group("ts"),
        warc_filename=m.group("warc"),
        warc_offset=int(m.group("offset")),
        warc_length=int(m.group("length")),
    )


def query_cc_index(
    match_text: str,
    *,
    columns: str = DEFAULT_COLUMNS,
    max_hits: int = 1000,
    filters: dict[str, str] | None = None,
    fetcher: Callable[[str], bytes] | None = None,
) -> list[IndexHit]:
    """Query the CC index API for ``match_text`` (a URL/domain regex).

    Uses the documented column-restriction syntax so responses contain only
    what we need::

        ?output=text&fl=url,domain,...&filter=content_type:application/pdf&limit=100

    See https://commoncrawl.github.io/cc-collaboration-documents/Trace/1-Common-Crawl-API
    """
    fetcher = fetcher or _default_fetcher
    from urllib.parse import quote, urlencode

    params: dict[str, str] = {"output": "text", "fl": columns, "showNum": str(max_hits)}
    if filters:
        params["filter"] = ",".join(f"{k}:{v}" for k, v in filters.items())
    qs = urlencode(params)
    url = f"{CC_INDEX_BASE}/search/?q={quote(match_text)}&{qs}"
    raw = fetcher(url).decode("utf-8", "replace")
    hits: list[IndexHit] = []
    for line in raw.splitlines():
        hit = parse_index_line(line)
        if hit:
            hits.append(hit)
        if len(hits) >= max_hits:
            break
    logger.info("CC index query %r -> %d hits", match_text, len(hits))
    return hits


# --------------------------------------------------------------------------- #
# WARC / WET / WAT processing
# --------------------------------------------------------------------------- #
@dataclass
class WarcRecord:
    """Normalized view of one WARC record regardless of parser backend."""

    record_type: str          # response | request | metadata | conversion | warcinfo
    target_uri: str
    date: str
    mime: str
    payload: bytes
    envelope_fields: dict[str, str] = field(default_factory=dict)
    json_metadata: dict | None = None   # populated for WAT records


class WarcProcessor:
    """Streaming processor for WARC/WET/WAT files.

    Prefers ``warcio`` (production) but transparently falls back to a
    minimal pure-python gzip+line parser so environments without warcio can
    still consume WET files and unit-test synthetic WARCs.
    """

    def __init__(self, fetcher: Callable[[str], bytes] | None = None) -> None:
        self._fetcher = fetcher or _default_fetcher
        self._have_warcio = True
        try:  # pragma: no cover - depends on environment
            import warcio  # noqa: F401
        except ImportError:
            self._have_warcio = False

    # -- public API --------------------------------------------------------- #
    def process_file(self, path_or_url: str, *, limit: int | None = None) -> Iterator[WarcRecord]:
        """Yield records from a local file or remote (http/s3) WARC/WET/WAT."""
        data = self._read(path_or_url)
        yield from self.process_bytes(data, limit=limit)

    def process_range(self, url: str, offset: int, length: int) -> WarcRecord | None:
        """Fetch exactly one record via HTTP Range request (CC supports this).

        This is how petabyte-scale extraction works: never download whole
        WARC files, only the byte ranges the index told us about.
        """
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        data = self._fetcher(url, headers=headers)  # type: ignore[call-arg]
        for rec in self.process_bytes(data, limit=1):
            return rec
        return None

    def process_bytes(self, data: bytes, *, limit: int | None = None) -> Iterator[WarcRecord]:
        if self._have_warcio:
            yield from self._process_warcio(data, limit)
        else:
            yield from self._process_fallback(data, limit)

    # -- backends ------------------------------------------------------------ #
    def _process_warcio(self, data: bytes, limit: int | None) -> Iterator[WarcRecord]:
        from warcio.archiveiterator import ArchiveIterator  # lazy import

        count = 0
        stream = io.BytesIO(data)
        try:
            for record in ArchiveIterator(stream):
                rec = WarcRecord(
                    record_type=record.rec_type or "unknown",
                    target_uri=record.rec_headers.get("WARC-Target-URI", ""),
                    date=record.rec_headers.get("WARC-Date", ""),
                    mime=(
                        record.http_headers.get_header("Content-Type")
                        if record.http_headers
                        else record.rec_headers.get("Content-Type", "")
                    )
                    or "",
                    payload=record.content_stream().read(),
                    envelope_fields=dict(record.rec_headers.headers),
                )
                if rec.record_type == "metadata" and rec.mime.startswith("application/json"):
                    try:
                        rec.json_metadata = json.loads(rec.payload.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        pass
                yield rec
                count += 1
                if limit and count >= limit:
                    return
        except Exception as exc:  # noqa: BLE001 - truncated/garbage archives happen
            logger.warning("warcio stopped early (%s); %d records emitted", exc, count)

    def _process_fallback(self, data: bytes, limit: int | None) -> Iterator[WarcRecord]:
        """Very small manual WARC parser (gzip-aware) used when warcio is absent."""
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        # Records are separated by 4 CRLF; header block ends at double CRLF.
        chunks = re.split(rb"\r\n\r\n\r\n\r\n|WARC/1\.0\r\n", data)
        count = 0
        for chunk in chunks:
            if not chunk.strip():
                continue
            head, _, body = chunk.partition(b"\r\n\r\n")
            headers = {}
            first = ""
            for i, line in enumerate(head.decode("utf-8", "replace").splitlines()):
                if i == 0:
                    first = line
                    continue
                k, sep, v = line.partition(": ")
                if sep:
                    headers[k] = v
            rec_type = first.split()[0] if first.upper().startswith("WARC/") else "response"
            payload = body.rstrip(b"\r\n")
            if headers.get("WARC-Block-Digest") or rec_type != "warcinfo":
                yield WarcRecord(
                    record_type=headers.get("WARC-Type", rec_type),
                    target_uri=headers.get("WARC-Target-URI", ""),
                    date=headers.get("WARC-Date", ""),
                    mime=headers.get("Content-Type", ""),
                    payload=payload,
                    envelope_fields=headers,
                )
                count += 1
                if limit and count >= limit:
                    return

    def _read(self, path_or_url: str) -> bytes:
        if path_or_url.startswith(("http://", "https://", "s3://")):
            return self._fetcher(path_or_url)
        with open(path_or_url, "rb") as fh:
            return fh.read()


# --------------------------------------------------------------------------- #
# High-level snapshot iteration
# --------------------------------------------------------------------------- #
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def html_to_text(html: bytes) -> str:
    """Cheap HTML->text extraction (boilerplate stripping lives in pipeline.cleaning)."""
    text = _SCRIPT_RE.sub(" ", html.decode("utf-8", "replace"))
    text = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def iter_snapshot_records(
    hits: Iterable[IndexHit],
    processor: WarcProcessor | None = None,
    *,
    min_text_len: int = 100,
) -> Iterator[dict]:
    """Turn index hits into normalized document dicts for the cleaning pipeline.

    Only the exact byte range of each hit is fetched (HTTP Range), which keeps
    bandwidth proportional to useful output — the core trick behind CC-scale
    ingestion.
    """
    processor = processor or WarcProcessor()
    for hit in hits:
        try:
            rec = processor.process_range(hit.http_url, hit.warc_offset, hit.warc_length)
        except Exception as exc:  # noqa: BLE001
            logger.warning("range fetch failed for %s: %s", hit.url, exc)
            continue
        if rec is None:
            continue
        if "html" in rec.mime:
            text = html_to_text(rec.payload)
        elif rec.mime.startswith(("text/plain", "application/json")):
            text = rec.payload.decode("utf-8", "replace")
        else:
            continue
        if len(text) < min_text_len:
            continue
        yield {
            "url": hit.url,
            "domain": hit.domain,
            "text": text,
            "source": "commoncrawl",
            "snapshot": hit.timestamp,
            "warc_file": hit.warc_filename,
            "warc_offset": hit.warc_offset,
            "warc_length": hit.warc_length,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


# --------------------------------------------------------------------------- #
# Default network fetcher (lazy imports so tests never touch the socket)
# --------------------------------------------------------------------------- #
def _default_fetcher(url: str, headers: dict[str, str] | None = None) -> bytes:
    import requests

    resp = requests.get(url, headers=headers or {}, timeout=60)
    resp.raise_for_status()
    return resp.content


def cc_warc_url(key: str) -> str:
    """Join helper exposed for tests / callers building CC object URLs."""
    return urljoin(CC_S3_BASE + "/", key.lstrip("/"))
