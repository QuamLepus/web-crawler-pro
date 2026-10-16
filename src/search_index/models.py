"""Data types shared by the whole search-index pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


def host_of(url: str) -> str:
    """Lower-cased, ``www.``-stripped host of ``url`` (``""`` when unusable)."""
    try:
        host = (urlparse(url).netloc or "").strip().lower()
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    # drop credentials and port; port matters for nothing here
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    return host.split(":", 1)[0]


@dataclass(slots=True)
class Doc:
    """One indexed document.

    ``authority`` is normalised to the Open PageRank scale (0-10) whichever
    source it came from, so the ranker can blend popularity lists that use
    wildly different units (Tranco ships a *rank*, PageRank ships a *score*).
    """

    url: str
    title: str = ""
    snippet: str = ""          # editorial description, or first lines of body
    body: str = ""             # may be dropped after indexing (see BodyPolicy)
    source: str = ""           # curlie | wikipedia | commoncrawl | seed
    authority: float = 0.0     # 0-10
    quality: float = 1.0       # 0-1, from src.training.filters
    lang: str = "en"
    host: str = ""

    def __post_init__(self) -> None:
        if not self.host:
            self.host = host_of(self.url)

    @property
    def index_text(self) -> str:
        """Text the BM25 shard actually sees: title, description, then body."""
        parts = [self.title, self.snippet, self.body]
        return " ".join(p for p in parts if p)

    def display_snippet(self, width: int = 240) -> str:
        text = (self.snippet or "").strip()
        if not text:
            text = (self.body or "").strip()
        text = " ".join(text.split())
        return text[:width] + ("…" if len(text) > width else "")

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "host": self.host,
            "title": self.title,
            "snippet": self.snippet,
            "body": self.body,
            "source": self.source,
            "authority": round(self.authority, 4),
            "quality": round(self.quality, 4),
            "lang": self.lang,
        }

    @classmethod
    def from_mapping(cls, row: dict) -> Doc:
        """Rebuild from a row dict keyed by :data:`~src.search_index.store.COLUMNS`."""
        return cls(
            url=row.get("url", ""),
            title=row.get("title", "") or "",
            snippet=row.get("snippet", "") or "",
            body=row.get("body", "") or "",
            source=row.get("source", "") or "",
            authority=float(row.get("authority") or 0.0),
            quality=float(row.get("quality") or 1.0),
            lang=row.get("lang", "") or "en",
            host=row.get("host", "") or "",
        )


@dataclass(slots=True)
class IndexStats:
    """Everything ``wcp search-index status`` and ``GET /search-index/stats`` show."""

    index_dir: str = ""
    level: int = 0
    docs: int = 0
    shards: int = 0
    terms: int = 0
    sources: dict[str, int] = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    exists: bool = False
    ready: bool = False

    def to_dict(self) -> dict:
        return {
            "index_dir": self.index_dir,
            "level": self.level,
            "docs": self.docs,
            "shards": self.shards,
            "terms": self.terms,
            "sources": dict(self.sources),
            "state": dict(self.state),
            "exists": self.exists,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class LevelSpec:
    """One row of the :data:`LEVELS` roadmap."""

    level: int
    name: str
    sources: tuple[str, ...]
    max_pages: int          # Common Crawl page budget; 0 = source default
    download: str           # human-readable download size
    disk: str               # human-readable on-disk size after indexing
    effort: str             # honest solo-developer estimate
    summary: str

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "name": self.name,
            "sources": list(self.sources),
            "max_pages": self.max_pages,
            "download": self.download,
            "disk": self.disk,
            "effort": self.effort,
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# The $0 roadmap
# --------------------------------------------------------------------------- #
LEVELS: dict[int, LevelSpec] = {
    0: LevelSpec(
        level=0,
        name="Curlie + Open PageRank + Tranco",
        sources=("curlie",),
        max_pages=0,
        download="~330 MB",
        disk="~3 GB",
        effort="2-3 days",
        summary=("2.9M human-edited sites with descriptions and category paths, "
                 "ranked by free Open PageRank authority. No crawling at all — "
                 "this alone replaces DuckDuckGo for topic discovery."),
    ),
    1: LevelSpec(
        level=1,
        name="Level 0 + Wikipedia",
        sources=("curlie", "wikipedia"),
        max_pages=0,
        download="~27 GB",
        disk="~40 GB",
        effort="3-5 days",
        summary=("Adds all 7.2M English Wikipedia articles: encyclopedic, "
                 "clean, and it makes definitional queries actually work."),
    ),
    2: LevelSpec(
        level=2,
        name="Level 1 + Common Crawl (10M pages)",
        sources=("curlie", "wikipedia", "commoncrawl"),
        max_pages=10_000_000,
        download="~55 GB",
        disk="~150 GB",
        effort="1-2 weeks",
        summary=("Streams Common Crawl WET files and keeps 10M English pages "
                 "that pass Gopher/C4 quality rules. Long-tail coverage; "
                 "runs as a sharded GitHub Actions matrix for free."),
    ),
    3: LevelSpec(
        level=3,
        name="Level 2 + Common Crawl (100M pages)",
        sources=("curlie", "wikipedia", "commoncrawl"),
        max_pages=100_000_000,
        download="~310 GB",
        disk="0.5-1 TB",
        effort="1-2 months",
        summary=("Ten times level 2. This is where $0 stops: beyond ~100M "
                 "pages the cost is disk and servers, not software."),
    ),
}

#: Sources that need a network download, by name.
DOWNLOADABLE_SOURCES = ("curlie", "wikipedia", "commoncrawl")


def level_spec(level: int) -> LevelSpec:
    """Return the spec for ``level``, clamping outside 0-3."""
    if level < 0:
        return LEVELS[0]
    if level > max(LEVELS):
        return LEVELS[max(LEVELS)]
    return LEVELS[level]
