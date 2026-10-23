"""SQLite-backed document store.

Why SQLite and not Parquet or Elasticsearch:

* it is in the standard library, so this package adds **no new dependency**;
* rows are addressable by ``rowid``, which is exactly what a BM25 shard needs
  (``rowids.npy`` in the shard maps postings back to rows here);
* ``url TEXT NOT NULL UNIQUE`` gives de-duplication for free — re-running a
  half-finished Common Crawl job cannot double-index anything;
* it handles 100M rows on a single file, and WAL mode lets the builder write
  while a query process reads.

Elasticsearch would buy us distributed sharding and a query DSL we do not need,
at the cost of a JVM and a cluster. DuckDB is used automatically for analytics
if it happens to be installed, but nothing depends on it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Self

from src.search_index.models import Doc, host_of

COLUMNS = ("id", "url", "host", "title", "snippet", "body", "source",
           "authority", "quality", "lang", "indexed_at",
           "content_hash", "snapshot")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id        INTEGER PRIMARY KEY,
    url       TEXT    NOT NULL UNIQUE,
    host      TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL DEFAULT '',
    snippet   TEXT    NOT NULL DEFAULT '',
    body      TEXT    NOT NULL DEFAULT '',
    source    TEXT    NOT NULL DEFAULT '',
    authority REAL    NOT NULL DEFAULT 0.0,
    quality   REAL    NOT NULL DEFAULT 1.0,
    lang      TEXT    NOT NULL DEFAULT '',
    indexed_at TEXT   NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_docs_host   ON docs(host);
CREATE INDEX IF NOT EXISTS idx_docs_source ON docs(source);
"""

#: Columns added after the first release of the index; applied on open so an
#: index built before incremental refresh existed keeps working.
_MIGRATIONS = (
    ("content_hash", "ALTER TABLE docs ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"),
    ("snapshot", "ALTER TABLE docs ADD COLUMN snapshot TEXT NOT NULL DEFAULT ''"),
)

_PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("temp_store", "MEMORY"),
    ("cache_size", "-262144"),      # ~256 MB
    ("mmap_size", "268435456"),     # 256 MB
)


class DocStore:
    """Append-mostly document store with random access by ``rowid``."""

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._readonly = readonly
        self._conn: sqlite3.Connection | None = None
        self.duplicates = 0
        if not readonly:
            self._connect()

    # -- lifecycle ---------------------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0,
                               check_same_thread=False)
        for key, value in _PRAGMAS:
            try:
                conn.execute(f"PRAGMA {key}={value}")
            except sqlite3.DatabaseError:  # pragma: no cover - locked/unsupported
                pass
        conn.executescript(_SCHEMA)
        present = {row[1] for row in conn.execute("PRAGMA table_info(docs)")}
        for column, statement in _MIGRATIONS:
            if column not in present:
                conn.execute(statement)
        conn.commit()
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self._readonly:
                if not self.path.exists():
                    raise FileNotFoundError(f"no document store at {self.path}")
                uri = f"file:{self.path}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            except sqlite3.DatabaseError:  # pragma: no cover
                pass
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- writes -------------------------------------------------------------- #
    def add_batch(self, docs: Sequence[Doc]) -> list[int]:
        """Insert ``docs`` and return the ``rowid`` of each (``-1`` if refused).

        Refused = the URL was already indexed. Callers must therefore build the
        BM25 shard from the *returned* rowids, not from the input order.
        """
        conn = self.conn
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rowids: list[int] = []
        sql = ("INSERT OR IGNORE INTO docs "
               "(url, host, title, snippet, body, source, authority, quality, "
               "lang, indexed_at, content_hash, snapshot) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        try:
            with conn:  # one transaction per batch
                for doc in docs:
                    cur = conn.execute(sql, (
                        doc.url,
                        doc.host or host_of(doc.url),
                        doc.title[:1024],
                        doc.snippet,
                        doc.body,
                        doc.source,
                        float(doc.authority),
                        float(doc.quality),
                        doc.lang,
                        now,
                        getattr(doc, "content_hash", "") or "",
                        getattr(doc, "snapshot", "") or "",
                    ))
                    if cur.lastrowid and cur.rowcount:
                        rowids.append(int(cur.lastrowid))
                    else:
                        rowids.append(-1)
                        self.duplicates += 1
        except sqlite3.Error as exc:  # pragma: no cover - disk full / locked
            raise RuntimeError(f"doc store insert failed: {exc}") from exc
        return rowids

    def update_document(self, url: str, doc: Doc, *, content_hash: str = "",
                        snapshot: str = "") -> int | None:
        """Replace the content of an existing row, keeping its ``rowid``.

        This is the incremental-refresh path: the document store's ids are what
        the BM25 shards point at, so refreshing a page must reuse the id rather
        than insert a new row (which would orphan every posting that referenced
        the old one).
        """
        cur = self.conn.execute(
            "UPDATE docs SET host=?, title=?, snippet=?, body=?, source=?, "
            "authority=?, quality=?, lang=?, indexed_at=?, content_hash=?, "
            "snapshot=? WHERE url=?",
            (doc.host or host_of(doc.url), doc.title[:1024], doc.snippet, doc.body,
             doc.source, float(doc.authority), float(doc.quality), doc.lang,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             content_hash, snapshot, url))
        self.conn.commit()
        if not cur.rowcount:
            return None
        row = self.conn.execute("SELECT id FROM docs WHERE url=?", (url,)).fetchone()
        return int(row[0]) if row else None

    def set_authority(self, host: str, authority: float) -> int:
        """Backfill authority for every document of ``host`` (Open PageRank pass)."""
        cur = self.conn.execute("UPDATE docs SET authority=? WHERE host=?",
                                (float(authority), host.lower()))
        self.conn.commit()
        return int(cur.rowcount or 0)

    def vacuum(self) -> None:
        try:
            self.conn.execute("VACUUM")
        except sqlite3.DatabaseError:  # pragma: no cover
            pass

    # -- reads --------------------------------------------------------------- #
    def fetch(self, rowids: Iterable[int]) -> list[Doc]:
        """Fetch documents by ``rowid``; missing ids are skipped."""
        ids = [int(i) for i in rowids if i and i > 0]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT {','.join(COLUMNS)} FROM docs WHERE id IN ({placeholders})", ids)
        out = [Doc.from_mapping(dict(zip(COLUMNS, r))) for r in rows]
        return out

    def fetch_map(self, rowids: Iterable[int]) -> dict[int, Doc]:
        """``{rowid: Doc}`` — the shape the query path needs.

        ``fetch`` returns rows in sqlite's own (ascending rowid) order, which is
        *not* the relevance order the caller asked in; mapping by id keeps the
        caller's ordering intact.
        """
        ids = [int(i) for i in rowids if i and i > 0]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT {','.join(COLUMNS)} FROM docs WHERE id IN ({placeholders})", ids)
        return {int(r[0]): Doc.from_mapping(dict(zip(COLUMNS, r))) for r in rows}

    def fetch_one(self, rowid: int) -> Doc | None:
        docs = self.fetch([rowid])
        return docs[0] if docs else None

    def get_by_url(self, url: str) -> Doc | None:
        row = self.conn.execute(
            f"SELECT {','.join(COLUMNS)} FROM docs WHERE url=?", (url,)).fetchone()
        return Doc.from_mapping(dict(zip(COLUMNS, row))) if row else None

    def count(self) -> int:
        try:
            return int(self.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
        except sqlite3.DatabaseError:
            return 0

    def source_counts(self) -> dict[str, int]:
        try:
            rows = self.conn.execute(
                "SELECT source, COUNT(*) FROM docs GROUP BY source ORDER BY 2 DESC")
        except sqlite3.DatabaseError:
            return {}
        return {str(src): int(n) for src, n in rows}

    def analytics(self, *, group_by: str, metric: str, source: str = "",
                  host: str = "", lang: str = "", indexed_from: str = "",
                  indexed_to: str = "", limit: int = 50, offset: int = 0) -> dict:
        """Run a bounded, allow-listed aggregate over the complete document table.

        Only SQL fragments selected from fixed maps are interpolated. User values
        are always bound parameters; raw SQL and document bodies are never returned.
        """
        group_exprs = {
            "source": "COALESCE(NULLIF(source, ''), '(unknown)')",
            "host": "COALESCE(NULLIF(host, ''), '(unknown)')",
            "lang": "COALESCE(NULLIF(lang, ''), '(unknown)')",
            "snapshot": "COALESCE(NULLIF(snapshot, ''), '(unknown)')",
            "day": "substr(indexed_at, 1, 10)",
        }
        metric_exprs = {
            "count": "COUNT(*)",
            "avg_authority": "AVG(authority)",
            "avg_quality": "AVG(quality)",
            "avg_body_chars": "AVG(length(body))",
        }
        if group_by not in group_exprs or metric not in metric_exprs:
            raise ValueError("unsupported analytics dimension or metric")
        if not 1 <= int(limit) <= 100 or not 0 <= int(offset) <= 5000:
            raise ValueError("analytics pagination out of range")
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (("source", source), ("host", host), ("lang", lang)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if indexed_from:
            clauses.append("substr(indexed_at, 1, 10) >= ?")
            params.append(indexed_from)
        if indexed_to:
            clauses.append("substr(indexed_at, 1, 10) <= ?")
            params.append(indexed_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        group_expr = group_exprs[group_by]
        metric_expr = metric_exprs[metric]
        if not self.path.exists():
            raise FileNotFoundError(f"no document store at {self.path}")
        # Use a per-call read-only connection so concurrent API requests cannot
        # share/contend over transaction state on the query facade's connection.
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30.0)
        try:
            conn.execute("BEGIN")
            rows = conn.execute(
                "WITH grouped AS ("
                f"SELECT {group_expr} AS label, {metric_expr} AS value, "
                f"COUNT(*) AS group_documents FROM docs{where} GROUP BY {group_expr}"
                "), metadata AS ("
                "SELECT COALESCE(SUM(group_documents), 0) AS matched_documents, "
                "COUNT(*) AS group_count FROM grouped"
                "), page AS ("
                "SELECT label, value, 1 AS present FROM grouped "
                "ORDER BY value DESC, label ASC LIMIT ? OFFSET ?"
                ") "
                "SELECT page.label, page.value, metadata.matched_documents, "
                "metadata.group_count, page.present FROM metadata "
                "LEFT JOIN page ON 1=1 ORDER BY page.value DESC, page.label ASC",
                [*params, int(limit), int(offset)]).fetchall()
            matched = int(rows[0][2]) if rows else 0
            groups = int(rows[0][3]) if rows else 0
            rows = [(label, value) for label, value, _matched, _groups, present in rows
                    if present]
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"matched_documents": matched, "group_count": groups,
                "group_by": group_by, "metric": metric,
                "rows": [{"label": str(label or "(unknown)"),
                          "value": (int(value) if metric == "count" else
                                    round(float(value or 0.0), 4))}
                         for label, value in rows],
                "limit": int(limit), "offset": int(offset)}

    def iter_all(self, batch: int = 1000) -> Iterator[Doc]:
        """Stream every document (used by the merge/compaction path)."""
        if not self.path.exists():
            return
        last = 0
        while True:
            rows = self.conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM docs WHERE id > ? ORDER BY id LIMIT ?",
                (last, batch)).fetchall()
            if not rows:
                return
            for row in rows:
                yield Doc.from_mapping(dict(zip(COLUMNS, row)))
            last = int(rows[-1][0])

    def sample(self, limit: int = 5) -> list[Doc]:
        rows = self.conn.execute(
            f"SELECT {','.join(COLUMNS)} FROM docs ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [Doc.from_mapping(dict(zip(COLUMNS, r))) for r in rows]

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total
