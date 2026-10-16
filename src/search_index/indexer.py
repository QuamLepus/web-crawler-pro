"""``SearchIndexBuilder`` — the resumable pipeline that turns datasets into an index.

Everything here is built around one constraint: **the process will die before it
finishes.** A 100M-page build is days of work; GitHub Actions kills jobs at six
hours; a laptop gets closed. So the builder

* writes a checkpoint (``state.json``) after *every shard*, not at the end,
* records per-source progress (documents written for Curlie, block offset for
  Wikipedia, finished file list for Common Crawl), and
* re-runs are idempotent because the document store has ``url UNIQUE``.

Running ``wcp search-index build --level 2`` five times in a row does five times
the *useful* work and zero duplicate documents.

Layout of ``SEARCH_INDEX_DIR``::

    data/search-index/
        state.json            build checkpoint
        docs.sqlite3          every document, addressable by rowid
        index/
            shards.json       [{shard_id, source, doc_count, path}, ...]
            shard-00000/      vocab.json, indptr.npy, indices.npy, data.npy,
                              doclen.npy, rowids.npy, df.npy, meta.json
            shard-00001/
        raw/                  downloaded datasets (kept so re-indexing is free)
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

from src.search_index.bm25 import SHARDS_FILE, BM25Shard, ShardMeta
from src.search_index.incremental import (
    CHANGED,
    UNCHANGED,
    ChangeTracker,
    content_hash,
)
from src.search_index.models import Doc, IndexStats, LevelSpec, level_spec
from src.search_index.spell import SPELL_FILE, SpellChecker
from src.search_index.store import DocStore

logger = logging.getLogger(__name__)

STATE_VERSION = 2
STATE_FILE = "state.json"
DOCS_DB = "docs.sqlite3"
RAW_DIR = "raw"
INDEX_DIR = "index"
SHARD_PREFIX = "shard-"

#: Sources that ship long-form text and therefore get the Gopher/C4 treatment.
_LONG_TEXT_SOURCES = frozenset({"commoncrawl", "wikipedia"})
#: Rough page count of an English Wikipedia dump, used to shard it.
WIKIPEDIA_BLOCKS_ESTIMATE = 72_500


class MissingSourceError(RuntimeError):
    """A source has no local data and downloading is disabled or impossible."""


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BuildOptions:
    """Everything that changes what a build produces."""

    level: int = 0
    data_dir: Path = Path("data/search-index")
    sources: tuple[str, ...] = ()          # empty = whatever the level says
    shard_docs: int = 100_000
    max_pages: int = 0                     # Common Crawl budget; 0 = level default
    body_chars: int = 512                  # how much body text BM25 actually sees
    language: str = "en"
    shard_index: int = 0                   # this worker's slice
    shard_count: int = 1
    download: bool = True
    quality_filter: bool = True
    authority: bool = True
    keep_body: bool = True
    wikipedia_date: str = ""       # empty = newest available dump
    commoncrawl_snapshot: str = ""  # empty = newest CC-MAIN snapshot
    # Incremental refresh: skip pages whose indexed text has not changed.
    incremental: bool = True
    # Write per-document vectors into each shard so queries can be reranked
    # without running the model at query time.
    embeddings: bool = False
    embed_model: str = ""          # empty = src.training.semantic_dedupe default

    def resolved_sources(self) -> tuple[str, ...]:
        return self.sources or level_spec(self.level).sources

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "sources": list(self.resolved_sources()),
            "shard_docs": self.shard_docs,
            "max_pages": self.max_pages,
            "body_chars": self.body_chars,
            "language": self.language,
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "quality_filter": self.quality_filter,
            "authority": self.authority,
            "keep_body": self.keep_body,
            "incremental": self.incremental,
            "embeddings": self.embeddings,
        }


# --------------------------------------------------------------------------- #
# Quality gate
# --------------------------------------------------------------------------- #
def quick_quality(text: str, *, lang: str = "en", source: str) -> tuple[bool, float]:
    """Cheap accept/reject + 0-1 score, reusing the training pipeline's rules.

    Only the rules that cost O(words) are used. ``quality_score`` from
    :mod:`src.training.filters` is richer but it runs sentence splitting and
    lexical-diversity passes, which is fine for 10k curated documents and far
    too slow for 100M crawled pages.
    """
    from src.training.filters import (
        c4_cookie_rule,
        c4_lorem_rule,
        gopher_alpha_ratio,
        gopher_stopword_lang_rule,
        gopher_word_count,
    )

    if source in _LONG_TEXT_SOURCES:
        checks = (
            gopher_word_count(text, lo=40, hi=100_000).passed,
            gopher_alpha_ratio(text, min_ratio=0.7).passed,
            c4_cookie_rule(text).passed,
            c4_lorem_rule(text).passed,
        )
        passed = all(checks)
        if lang == "en" and passed:
            passed = gopher_stopword_lang_rule(text, lang).passed
    else:
        passed = bool(text and len(text.strip()) >= 16)

    words = text.split()
    diversity = (len(set(words)) / len(words)) if words else 0.0
    length_bonus = min(1.0, len(words) / 400.0)
    score = round(min(1.0, 0.25 + 0.45 * length_bonus + 0.30 * diversity), 4)
    return passed, (score if passed else 0.0)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
@dataclass
class _SourceProgress:
    status: str = "pending"          # pending | running | partial | done | failed
    docs: int = 0
    shards: int = 0
    cursor: int = 0                  # source-specific resume point
    files: list[str] = field(default_factory=list)   # Common Crawl: finished WET paths
    error: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "docs": self.docs, "shards": self.shards,
                "cursor": self.cursor, "files": self.files[-5000:], "error": self.error}

    @classmethod
    def from_dict(cls, data: dict) -> _SourceProgress:
        return cls(status=str(data.get("status", "pending")),
                   docs=int(data.get("docs", 0)), shards=int(data.get("shards", 0)),
                   cursor=int(data.get("cursor", 0)),
                   files=list(data.get("files", [])) or [],
                   error=str(data.get("error", "")))


class SearchIndexBuilder:
    """Builds (and can resume building) a sharded search index."""

    def __init__(self, data_dir: str | Path, *, options: BuildOptions | None = None,
                 fetcher=None, progress: Callable[[str], None] | None = None) -> None:
        self.options = options or BuildOptions(level=0, data_dir=Path(data_dir))
        self.options.data_dir = Path(data_dir)
        self.root = Path(data_dir)
        self.raw_dir = self.root / RAW_DIR
        self.index_dir = self.root / INDEX_DIR
        self.state_path = self.root / STATE_FILE
        self.fetcher = fetcher
        self.progress = progress or (lambda _msg: None)

        self.root.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.store = DocStore(self.root / DOCS_DB)
        self._metas: list[ShardMeta] = []
        self._state: dict = {}
        self._authority = None
        self._embedder = None
        self._embeddings_disabled = False
        self.tracker: ChangeTracker | None = (
            ChangeTracker(self.root / "incremental.sqlite3")
            if self.options.incremental else None)
        self.load()

    # -- state --------------------------------------------------------------- #
    def load(self) -> dict:
        data: dict = {}
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if data.get("version") != STATE_VERSION:
            data = {}
        self._state = data or {
            "version": STATE_VERSION,
            "level": self.options.level,
            "created_at": _now(),
            "updated_at": _now(),
            "sources": {},
            "options": self.options.to_dict(),
        }
        self._load_shards()
        return self._state

    def _load_shards(self) -> None:
        path = self.index_dir / SHARDS_FILE
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload.get("shards", []) if isinstance(payload, dict) else payload
            self._metas = [ShardMeta.from_dict(m) for m in raw]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            self._metas = []

    @property
    def state(self) -> dict:
        return self._state

    def save_state(self) -> None:
        self._state["updated_at"] = _now()
        self._state["level"] = self.options.level
        self._state["options"] = self.options.to_dict()
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _progress_of(self, source: str) -> _SourceProgress:
        raw = self._state.setdefault("sources", {}).get(source)
        progress = _SourceProgress.from_dict(raw) if isinstance(raw, dict) else _SourceProgress()
        self._state["sources"][source] = progress.to_dict()
        return progress

    def _write_progress(self, source: str, progress: _SourceProgress) -> None:
        self._state.setdefault("sources", {})[source] = progress.to_dict()
        self.save_state()

    # -- shards --------------------------------------------------------------- #
    def _next_shard_id(self) -> int:
        return (max((m.shard_id for m in self._metas), default=-1) + 1)

    def _write_shards(self) -> None:
        payload = {"version": STATE_VERSION, "count": len(self._metas),
                   "shards": [m.to_dict() for m in self._metas]}
        tmp = self.index_dir / (SHARDS_FILE + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.index_dir / SHARDS_FILE)

    def _index_text(self, doc: Doc) -> str:
        body = doc.body[: self.options.body_chars] if self.options.body_chars else doc.body
        return " ".join(p for p in (doc.title, doc.snippet, body) if p)

    def _flush(self, source: str, buffer: list[Doc]) -> int:
        """Insert ``buffer`` into the store and invert it into a shard."""
        if not buffer:
            return 0
        rowids = self.store.add_batch(buffer)
        pairs = [(rid, doc) for rid, doc in zip(rowids, buffer) if rid > 0]
        if not pairs:
            return 0
        surface: dict[str, Counter] = {}
        shard = BM25Shard().build(
            [rid for rid, _ in pairs],
            [self._index_text(doc) for _, doc in pairs],
            shard_id=self._next_shard_id(), source=source, surface=surface)
        directory = self.index_dir / f"{SHARD_PREFIX}{shard.meta.shard_id:05d}"
        # The surface map is per shard: each shard then carries only the forms
        # that occur in it, and the spelling dictionary merges them at the end.
        meta = shard.save(directory, surface=surface)
        if self.options.embeddings:
            self._write_embeddings(shard, directory,
                                   [doc for _, doc in pairs])
        self._metas.append(meta)
        self._write_shards()
        if self.tracker is not None:
            self.tracker.record_many(
                [(doc.url, rid, doc.content_hash, doc.snapshot, meta.shard_id)
                 for rid, doc in pairs])
        logger.info("shard %d: %d docs (%s)", meta.shard_id, len(pairs), source)
        return len(pairs)

    # -- embeddings --------------------------------------------------------- #
    def _embedder_instance(self):
        """The configured embedder, or ``None`` when only the lexical fallback
        is available (in which case writing vectors would be pointless)."""
        if self._embedder is None and not self._embeddings_disabled:
            from src.search_index.rerank import is_hashing_embedder
            from src.training.semantic_dedupe import make_embedder

            embedder = make_embedder(self.options.embed_model or None)
            if is_hashing_embedder(embedder):
                self._embeddings_disabled = True
                logger.warning(
                    "sentence-transformers is unavailable; skipping embeddings "
                    "(the hashing fallback is lexical and would not add signal)")
                return None
            self._embedder = embedder
        return None if self._embeddings_disabled else self._embedder

    def _write_embeddings(self, shard: BM25Shard, directory: Path,
                          docs: list[Doc]) -> bool:
        """Compute and store per-document vectors for one shard."""
        embedder = self._embedder_instance()
        if embedder is None:
            return False
        try:
            from src.search_index.rerank import EmbeddingReranker

            vectors = EmbeddingReranker(embedder=embedder).encode(
                [self._index_text(doc) for doc in docs])
            shard.save_embeddings(directory, vectors,
                                  model=str(getattr(embedder, "model_name",
                                                    "") or self.options.embed_model
                                            or "all-MiniLM-L6-v2"))
            return True
        except Exception as exc:  # noqa: BLE001 - embeddings must not fail a build
            logger.warning("embeddings for shard %d failed: %s",
                           shard.meta.shard_id, exc)
            return False

    def build_embeddings(self, shard_ids: Sequence[int] | None = None) -> int:
        """Add vectors to shards of an index that was built without them."""
        import numpy as np

        done = 0
        for meta in list(self._metas):
            if shard_ids is not None and meta.shard_id not in set(shard_ids):
                continue
            directory = self.index_dir / meta.path
            rowids = np.load(directory / "rowids.npy")
            docs = self.store.fetch_map([int(r) for r in rowids.tolist()])
            ordered = [docs[int(r)] for r in rowids.tolist() if int(r) in docs]
            if not ordered:
                continue
            shard = BM25Shard(meta)
            if self._write_embeddings(shard, directory, ordered):
                done += 1
        return done

    def build_spell(self, config=None) -> dict:
        """Train the spelling dictionary from this index's own vocabulary.

        ``config`` is a :class:`~src.search_index.spell.SpellConfig`; small
        indexes need a lower ``min_freq`` because every term occurs once.
        """
        checker = SpellChecker.from_index(self.index_dir, config)
        checker.save(self.index_dir / SPELL_FILE)
        logger.info("spell: %d terms, %d delete signatures",
                    len(checker), len(checker.deletes))
        return checker.stats()

    def add_documents(self, docs: Iterable[Doc], *, source: str = "seed") -> int:
        """Index an arbitrary stream of documents.

        This is the hook that lets the platform index *its own crawled data* —
        run a crawl, feed the results here, and tomorrow's discovery queries can
        find pages nobody else's index contains.
        """
        written = 0
        buffer: list[Doc] = []
        for doc in docs:
            if not doc.url:
                continue
            doc.source = doc.source or source
            if self.options.quality_filter:
                ok, score = quick_quality(self._index_text(doc),
                                          lang=doc.lang, source=doc.source)
                if not ok:
                    continue
                doc.quality = score
            if self._authority is not None:
                doc.authority = max(doc.authority, self._authority.get(doc.host))
            if not self.options.keep_body:
                doc.body = ""
            buffer.append(doc)
            if len(buffer) >= self.options.shard_docs:
                written += self._flush(source, buffer)
                buffer = []
        written += self._flush(source, buffer)
        return written

    # -- authority ------------------------------------------------------------ #
    def load_authority(self) -> int:
        """Load whichever authority files are already downloaded, if any."""
        if not self.options.authority:
            return 0
        from src.search_index.sources.authority import AuthorityTable

        table = AuthorityTable()
        for path in sorted(self.raw_dir.glob("*")):
            name = path.name.lower()
            if "top10million" in name or "openpagerank" in name or "domcop" in name:
                table.load_openpagerank(path)
            elif name.startswith("top-1m") or "tranco" in name:
                table.load_tranco(path)
        self._authority = table
        return len(table)

    def backfill_authority(self) -> int:
        """Re-score documents already in the store from the authority files.

        Used when an index was built before the authority lists were downloaded
        (or when a newer snapshot arrives). Works in chunks against a temporary
        table so it is one ``UPDATE`` per 100k hosts instead of 10M statements.
        """
        from src.search_index.sources.authority import AuthorityTable

        table = self._authority or AuthorityTable()
        if not len(table):
            for path in sorted(self.raw_dir.glob("*")):
                name = path.name.lower()
                if "top10million" in name or "openpagerank" in name or "domcop" in name:
                    table.load_openpagerank(path)
                elif name.startswith("top-1m") or "tranco" in name:
                    table.load_tranco(path)
        if not len(table):
            return 0
        updated = 0
        items = list(table.scores.items())
        for start in range(0, len(items), 100_000):
            chunk = items[start:start + 100_000]
            self.store.conn.execute("DROP TABLE IF EXISTS tmp_authority")
            self.store.conn.execute(
                "CREATE TEMP TABLE tmp_authority(host TEXT PRIMARY KEY, score REAL)")
            self.store.conn.executemany(
                "INSERT OR REPLACE INTO tmp_authority(host, score) VALUES (?,?)", chunk)
            cur = self.store.conn.execute(
                "UPDATE docs SET authority = MAX(authority, (SELECT score "
                "FROM tmp_authority WHERE tmp_authority.host = docs.host)) "
                "WHERE host IN (SELECT host FROM tmp_authority)")
            updated += int(cur.rowcount or 0)
            self.store.conn.commit()
        self.store.conn.execute("DROP TABLE IF EXISTS tmp_authority")
        return updated

    # -- sources -------------------------------------------------------------- #
    def _source_iterator(self, source: str) -> Iterator[Doc]:
        if source == "curlie":
            yield from self._curlie_docs()
        elif source == "wikipedia":
            yield from self._wikipedia_docs()
        elif source == "commoncrawl":
            yield from self._commoncrawl_docs()
        else:
            raise MissingSourceError(f"unknown source {source!r}")

    def _curlie_docs(self) -> Iterator[Doc]:
        from src.search_index.sources.curlie import download_curlie, iter_curlie_docs

        content = _first_glob(self.raw_dir, "rdf-*-c.tsv*")
        if content is None:
            if not self.options.download:
                raise MissingSourceError(
                    "no Curlie TSV in raw/ and downloading is disabled; "
                    "see https://curlie.org/download")
            self.progress("downloading Curlie directory (~200 MB)")
            _s, content = download_curlie(self.raw_dir, fetcher=self.fetcher)
        structure = _first_glob(self.raw_dir, "rdf-*-s.tsv*")
        docs = iter_curlie_docs(structure, content)
        if self.options.shard_count > 1:
            # Curlie has 2.9M rows and no natural split, so shard by line
            # index — every worker sees a disjoint slice and the merge step
            # drops any overlap.
            index = self.options.shard_index
            count = self.options.shard_count
            docs = (doc for i, doc in enumerate(docs) if i % count == index)
        yield from docs

    def _wikipedia_docs(self) -> Iterator[Doc]:
        from src.search_index.sources.wikipedia import (
            DUMP_URL_TEMPLATE,
            PAGES_PER_BLOCK,
            iter_wikipedia_pages,
            page_to_doc,
        )

        path = _first_glob(self.raw_dir, f"{self.options.language}wiki-*.xml.bz2")
        if path is None:
            if not self.options.download:
                raise MissingSourceError(
                    "no Wikipedia dump in raw/ and downloading is disabled")
            date = self.options.wikipedia_date or _latest_dump_date(self.fetcher)
            url = DUMP_URL_TEMPLATE.format(lang=self.options.language, date=date)
            dest = self.raw_dir / url.rsplit("/", 1)[-1]
            from src.search_index.download import resumable_download
            self.progress(f"downloading {url.name if hasattr(url,'name') else url}")
            resumable_download(url, dest, label="wikipedia dump", fetcher=self.fetcher)
            path = dest

        blocks_per_shard = -(-WIKIPEDIA_BLOCKS_ESTIMATE // max(1, self.options.shard_count))
        start_block = self.options.shard_index * blocks_per_shard
        self.progress(f"wikipedia blocks {start_block}..{start_block + blocks_per_shard}")
        pages = iter_wikipedia_pages(
            path, start_block=start_block,
            max_pages=blocks_per_shard * PAGES_PER_BLOCK)
        for page in pages:
            yield page_to_doc(page, lang=self.options.language)

    def _commoncrawl_docs(self) -> Iterator[Doc]:
        from src.search_index.sources.commoncrawl import CommonCrawlSource

        progress = self._progress_of("commoncrawl")
        source = CommonCrawlSource(self.options.commoncrawl_snapshot or None,
                                   fetcher=self.fetcher)
        budget = self.options.max_pages or level_spec(self.options.level).max_pages
        per_shard = max(1, budget // max(1, self.options.shard_count))
        done = set(progress.files)
        self.progress(f"commoncrawl shard {self.options.shard_index}/"
                      f"{self.options.shard_count}, {len(done)} files already done")

        def _on_file(path_key: str) -> None:
            progress.files.append(path_key)
            progress.cursor = len(progress.files)
            self._write_progress("commoncrawl", progress)

        yield from source.iter_docs(
            shard_index=self.options.shard_index,
            shard_count=self.options.shard_count,
            max_pages=per_shard, skip=done, progress=_on_file)

    # -- the build ------------------------------------------------------------ #
    def build(self, *, sources: Sequence[str] | None = None) -> IndexStats:
        """Run (or resume) the build. Returns :class:`IndexStats`."""
        names = tuple(sources or self.options.resolved_sources())
        logger.info("building search index level %d in %s: %s",
                    self.options.level, self.root, ", ".join(names))
        if self.options.authority:
            hosts = self.load_authority()
            if hosts:
                self.progress(f"authority table: {hosts:,} hosts")
            else:
                self.progress("no authority files in raw/ — authority stays at defaults")

        for name in names:
            progress = self._progress_of(name)
            if progress.status == "done":
                self.progress(f"{name}: already complete ({progress.docs:,} docs)")
                continue
            progress.status = "running"
            self._write_progress(name, progress)
            written = 0
            try:
                for count in self._consume(name):
                    written = count
                    progress.docs = count
                    progress.status = "partial"
                    self._write_progress(name, progress)
            except MissingSourceError as exc:
                progress.status = "failed"
                progress.error = str(exc)
                self._write_progress(name, progress)
                logger.error("source %s unavailable: %s", name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - resume must survive anything
                progress.status = "partial"
                progress.error = str(exc)
                self._write_progress(name, progress)
                logger.error("source %s failed after %d docs: %s", name, written, exc)
                continue
            progress.docs = written
            progress.shards = sum(1 for m in self._metas if m.source == name)
            progress.status = "done"
            progress.error = ""
            self._write_progress(name, progress)
            self.progress(f"{name}: {written:,} documents indexed")
            snapshot = self._snapshot_for(name)
            if self.tracker is not None and snapshot:
                self.tracker.record_snapshot(snapshot)

        self.save_state()
        return self.stats()

    def _snapshot_for(self, source: str) -> str:
        """Dataset version this source's documents belong to."""
        if source == "commoncrawl":
            return self.options.commoncrawl_snapshot or ""
        if source == "wikipedia":
            return self.options.wikipedia_date or ""
        return ""

    def refresh(self, snapshot: str | None = None, *,
                shard_index: int | None = None,
                shard_count: int | None = None,
                incremental: bool | None = None) -> IndexStats:
        """Ingest one Common Crawl snapshot into an existing index.

        With incremental tracking (the default) only pages whose indexed text
        actually changed are re-inverted, and only the shards that owned them
        are rebuilt at the end. Run it with ``incremental=False`` and you get
        the honest fallback: every page is re-read and every shard of this
        source is rebuilt.
        """
        use_tracker = self.options.incremental if incremental is None \
            else bool(incremental)
        if use_tracker:
            if self.tracker is None:
                self.tracker = ChangeTracker(self.root / "incremental.sqlite3")
            self.options.incremental = True
        elif self.tracker is not None:
            self.tracker.close()
            self.tracker = None
            self.options.incremental = False
        if snapshot:
            self.options.commoncrawl_snapshot = snapshot
        if not self.options.commoncrawl_snapshot:
            from src.search_index.sources.commoncrawl import latest_snapshot

            self.options.commoncrawl_snapshot = latest_snapshot(
                fetcher=self.fetcher)
        if shard_count is not None:
            self.options.shard_count = int(shard_count)
        if shard_index is not None:
            self.options.shard_index = int(shard_index)
        # a refresh re-reads a whole snapshot, so the "done" marker must go
        progress = self._progress_of("commoncrawl")
        progress.status = "partial"
        self._write_progress("commoncrawl", progress)

        self.progress(f"refreshing from {self.options.commoncrawl_snapshot}")
        stats = self.build(sources=("commoncrawl",))
        if self.tracker is not None:
            rebuilt = self.rebuild_dirty_shards()
            self.tracker.record_snapshot(self.options.commoncrawl_snapshot)
            counters = self.tracker.counters.to_dict()
            self.progress(f"refresh done: {counters} "
                          f"({rebuilt} shard(s) rebuilt)")
        else:
            # nothing tracked which shards changed, so redo all of this source
            rebuilt = self.rebuild_shards([meta.shard_id for meta in self._metas
                                           if meta.source == "commoncrawl"])
            counters = {"new": stats.docs, "changed": 0, "unchanged": 0}
            self.progress(f"refresh done (full): {stats.docs:,} documents, "
                          f"{rebuilt} shard(s) rebuilt")
        stats.state.setdefault("refresh", {})
        stats.state["refresh"] = {**counters,
                                  "shards_rebuilt": rebuilt,
                                  "incremental": self.tracker is not None,
                                  "snapshot": self.options.commoncrawl_snapshot}
        return stats

    def _consume(self, source: str) -> Iterator[int]:
        """Feed one source through the sharder, yielding a running total."""
        buffer: list[Doc] = []
        written = 0
        snapshot = self._snapshot_for(source)
        tracker = self.tracker
        for doc in self._source_iterator(source):
            if not doc.url:
                continue
            text = self._index_text(doc)
            if self.options.quality_filter:
                ok, score = quick_quality(text, lang=doc.lang or self.options.language,
                                          source=source)
                if not ok:
                    continue
                doc.quality = score
            if self._authority is not None:
                doc.authority = max(doc.authority, self._authority.get(doc.host))
            if tracker is not None:
                doc.snapshot = snapshot
                doc.content_hash = content_hash(text)
                state = tracker.classify(doc.url, doc.content_hash, snapshot)
                if state == UNCHANGED:
                    continue
                if state == CHANGED and self._apply_change(tracker, doc):
                    continue
            if not self.options.keep_body:
                doc.body = ""
            buffer.append(doc)
            if len(buffer) >= self.options.shard_docs:
                written += self._flush(source, buffer)
                buffer = []
                yield written
        written += self._flush(source, buffer)
        yield written

    def _apply_change(self, tracker: ChangeTracker, doc: Doc) -> bool:
        """Update a page that changed in place; mark its shard dirty.

        Returns ``True`` when the change was applied (so the caller should not
        also insert it), ``False`` when the row has gone and it must be indexed
        as new.
        """
        previous = tracker.get(doc.url)
        rowid = self.store.update_document(
            doc.url, doc, content_hash=doc.content_hash, snapshot=doc.snapshot)
        if rowid is None:
            tracker.forget(doc.url)
            return False
        shard_id = previous.shard_id if previous else -1
        tracker.record(doc.url, rowid, doc.content_hash, doc.snapshot, shard_id)
        tracker.mark_dirty(shard_id, reason="changed")
        return True

    # -- shard rebuild ------------------------------------------------------ #
    def rebuild_dirty_shards(self) -> int:
        """Re-invert every shard a refresh marked dirty. Returns the count."""
        if self.tracker is None:
            return 0
        dirty = self.tracker.dirty_shards()
        if not dirty:
            return 0
        rebuilt = self.rebuild_shards(dirty)
        self.tracker.clear_dirty(dirty)
        return rebuilt

    def rebuild_shards(self, shard_ids: Sequence[int]) -> int:
        """Re-invert the named shards from whatever is in the document store.

        Used after an incremental refresh: the document rows changed in place,
        so the postings that pointed at them are stale until the shard is
        rebuilt. Only the affected shards are touched.
        """
        import numpy as np

        wanted = {int(i) for i in shard_ids}
        rebuilt = 0
        for position, meta in enumerate(self._metas):
            if meta.shard_id not in wanted:
                continue
            directory = self.index_dir / meta.path
            if not (directory / "rowids.npy").exists():
                continue
            try:
                rowids = [int(r) for r in np.load(directory / "rowids.npy").tolist()]
            except (OSError, ValueError):
                continue
            docs = self.store.fetch_map(rowids)
            kept = [r for r in rowids if r in docs]
            if not kept:
                continue
            surface: dict[str, Counter] = {}
            shard = BM25Shard().build(
                kept, [self._index_text(docs[r]) for r in kept],
                shard_id=meta.shard_id, source=meta.source, surface=surface)
            shard.save(directory, surface=surface)
            if (directory / "embeddings.npy").exists():
                self._write_embeddings(shard, directory, [docs[r] for r in kept])
            self._metas[position] = shard.meta
            rebuilt += 1
            logger.info("rebuilt shard %d (%d docs)", meta.shard_id, len(kept))
        if rebuilt:
            self._write_shards()
        return rebuilt

    # -- reporting ------------------------------------------------------------ #
    def stats(self) -> IndexStats:
        sources = self.store.source_counts()
        self._load_shards()
        return IndexStats(
            index_dir=str(self.root),
            level=int(self._state.get("level", self.options.level)),
            docs=sum(m.doc_count for m in self._metas),
            shards=len(self._metas),
            terms=sum(m.term_count for m in self._metas),
            sources=sources,
            state={name: dict(data) for name, data in
                   (self._state.get("sources") or {}).items()},
            exists=self.state_path.exists(),
            ready=bool(self._metas),
        )

    def extras(self) -> dict:
        """State of the optional layers: embeddings, spelling, incremental."""
        embedded = sum(
            1 for m in self._metas
            if (self.index_dir / m.path / "embeddings.npy").exists())
        return {
            "shards": len(self._metas),
            "shards_with_embeddings": embedded,
            "spell": {
                "trained": (self.index_dir / SPELL_FILE).exists(),
                **(_load_spell_stats(self.index_dir)),
            },
            "incremental": (self.tracker.stats() if self.tracker is not None
                            else {"enabled": False}),
        }

    def level_info(self) -> LevelSpec:
        return level_spec(self.options.level)

    def close(self) -> None:
        if self.tracker is not None:
            self.tracker.close()
        self.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_spell_stats(index_dir: Path) -> dict:
    """Read just the header of ``spell.json.gz`` (cheap enough for ``status``)."""
    checker = SpellChecker.load(Path(index_dir) / SPELL_FILE)
    return checker.stats() if checker is not None else {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _first_glob(directory: Path, pattern: str) -> Path | None:
    matches = sorted(directory.glob(pattern))
    return matches[0] if matches else None


def _latest_dump_date(fetcher=None) -> str:
    """Newest ``YYYYMMDD`` Wikipedia dump directory (falls back to a known one)."""
    import re

    url = "https://dumps.wikimedia.org/enwiki/"
    try:
        from src.search_index.download import fetch_bytes

        html_text = fetch_bytes(url, fetcher=fetcher).decode("utf-8", "replace")
        dates = sorted(set(re.findall(r"/enwiki/(\d{8})/", html_text)))
        if dates:
            return dates[-1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not list dumps (%s); using fallback date", exc)
    return "20261001"


def open_builder(data_dir: str | Path, *, level: int | None = None,
                 **overrides) -> SearchIndexBuilder:
    """Open (and if needed create) a builder rooted at ``data_dir``."""
    directory = Path(data_dir)
    options = BuildOptions(level=level if level is not None else 0,
                           data_dir=directory)
    state_file = directory / STATE_FILE
    if state_file.exists() and level is None:
        try:
            options.level = int(json.loads(state_file.read_text()).get("level", 0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return SearchIndexBuilder(directory, options=replace(options, **overrides))
