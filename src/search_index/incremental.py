"""Incremental Common Crawl indexing — re-index only what actually changed.

Common Crawl publishes a fresh crawl roughly monthly. Re-running a level-2 build
from scratch to pick up one month of changes means downloading ~28 GB and
re-inverting 10M pages to discover that 99% of them did not change. This module
makes the second run proportional to *what is different*.

How it works
------------
Every indexed document gets a row in a small side database keyed by a 64-bit
hash of its URL:

    url_hash -> rowid, content_hash, snapshot, shard_id

On a new run each page is classified before it reaches the document store:

``new``
    URL never seen — index it normally.
``unchanged``
    URL seen and the indexed text hashes the same — **skip it entirely**. This
    is the win: no insert, no posting, no new shard entry.
``changed``
    URL seen, hash differs — update the existing row in place and mark the shard
    that owns it *dirty*.

Dirty shards are rebuilt at the end of the run (re-read their documents from the
store, re-invert them, overwrite the shard directory). Rebuilding one 100k-doc
shard takes seconds and touches nothing else, so a refresh of 10M pages with a
5% change rate costs ~500k shard rebuilds' worth of work spread over ~100
shards — not 10M.

What this cannot do
-------------------
Common Crawl has no "changed since" API: there is no way to ask which URLs are
new before fetching them, so the *download* cost of a refresh is still the full
snapshot. What gets cheaper is everything after it: storage, indexing, and
re-uploading artifacts. If you want to skip downloads too, keep the previous
snapshot's WET files and diff the URL lists (``wet.paths.gz``) yourself — the
``snapshots`` table here records what you have already ingested so you can tell.

Also note the hash is over the *indexed text* (title + snippet + first
``body_chars`` characters), not the whole page, so a page whose footer changed
but whose content did not will (correctly) count as unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

logger = logging.getLogger(__name__)

STATE_DB = "incremental.sqlite3"
NEW, UNCHANGED, CHANGED = "new", "unchanged", "changed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS indexed (
    url_hash     INTEGER PRIMARY KEY,
    url          TEXT    NOT NULL,
    rowid        INTEGER NOT NULL,
    content_hash TEXT    NOT NULL,
    snapshot     TEXT    NOT NULL DEFAULT '',
    shard_id     INTEGER NOT NULL DEFAULT -1,
    updated_at   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_indexed_snapshot ON indexed(snapshot);
CREATE TABLE IF NOT EXISTS dirty_shards (
    shard_id INTEGER PRIMARY KEY,
    reason   TEXT NOT NULL DEFAULT '',
    marked_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    name       TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL,
    docs       INTEGER NOT NULL DEFAULT 0,
    new        INTEGER NOT NULL DEFAULT 0,
    changed    INTEGER NOT NULL DEFAULT 0,
    unchanged  INTEGER NOT NULL DEFAULT 0
);
"""


def url_hash(url: str) -> int:
    """Stable signed 64-bit hash of a URL (SQLite INTEGER is signed)."""
    digest = hashlib.blake2b(url.encode("utf-8", "replace"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def content_hash(text: str) -> str:
    """Short digest of the text we actually index."""
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()


@dataclass(slots=True)
class DocState:
    url: str
    rowid: int
    content_hash: str
    snapshot: str = ""
    shard_id: int = -1


@dataclass
class RunCounters:
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    shards_rebuilt: int = 0

    def to_dict(self) -> dict:
        return {"new": self.new, "changed": self.changed,
                "unchanged": self.unchanged, "shards_rebuilt": self.shards_rebuilt}

    @property
    def total(self) -> int:
        return self.new + self.changed + self.unchanged


class ChangeTracker:
    """What have we already indexed, and is it still the same?"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.counters = RunCounters()

    def close(self) -> None:
        try:
            self._conn.commit()
        except sqlite3.DatabaseError:  # pragma: no cover
            pass
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- classification ------------------------------------------------------ #
    def get(self, url: str) -> DocState | None:
        row = self._conn.execute(
            "SELECT url, rowid, content_hash, snapshot, shard_id FROM indexed "
            "WHERE url_hash=?", (url_hash(url),)).fetchone()
        if row is None:
            return None
        return DocState(url=row[0], rowid=int(row[1]), content_hash=row[2],
                        snapshot=row[3] or "", shard_id=int(row[4]))

    def classify(self, url: str, digest: str, snapshot: str = "") -> str:
        """``new`` / ``unchanged`` / ``changed`` — and count it.

        The **content** decides, not the snapshot. Common Crawl re-releases the
        whole web every few weeks under a new ``CC-MAIN-…`` name, so treating a
        new snapshot as a change would mark every page dirty on every refresh
        and save nothing. The snapshot is still stored — it tells you when a
        page was last seen — but it never forces a re-index.
        """
        existing = self.get(url)
        if existing is None:
            self.counters.new += 1
            return NEW
        if existing.content_hash == digest:
            self.counters.unchanged += 1
            return UNCHANGED
        self.counters.changed += 1
        return CHANGED

    # -- recording ----------------------------------------------------------- #
    def record(self, url: str, rowid: int, digest: str, snapshot: str = "",
               shard_id: int = -1) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO indexed "
            "(url_hash, url, rowid, content_hash, snapshot, shard_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (url_hash(url), url, int(rowid), digest, snapshot, int(shard_id),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self._conn.commit()

    def record_many(self, rows) -> None:
        """Bulk ``(url, rowid, digest, snapshot, shard_id)`` insert."""
        payload = [(url_hash(u), u, int(r), d, s, int(sid),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                   for u, r, d, s, sid in rows]
        if not payload:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO indexed "
                "(url_hash, url, rowid, content_hash, snapshot, shard_id, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", payload)

    def forget(self, url: str) -> None:
        self._conn.execute("DELETE FROM indexed WHERE url_hash=?", (url_hash(url),))
        self._conn.commit()

    # -- dirty shards -------------------------------------------------------- #
    def mark_dirty(self, shard_id: int, reason: str = "changed") -> None:
        if shard_id is None or int(shard_id) < 0:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO dirty_shards(shard_id, reason, marked_at) "
            "VALUES (?,?,?)",
            (int(shard_id), reason,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self._conn.commit()

    def dirty_shards(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT shard_id FROM dirty_shards ORDER BY shard_id").fetchall()
        return [int(r[0]) for r in rows]

    def clear_dirty(self, shard_ids) -> None:
        ids = [(int(i),) for i in shard_ids]
        if not ids:
            return
        with self._conn:
            self._conn.executemany("DELETE FROM dirty_shards WHERE shard_id=?", ids)

    # -- snapshots ----------------------------------------------------------- #
    def record_snapshot(self, name: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots(name, indexed_at, docs, new, "
            "changed, unchanged) VALUES (?,?,?,?,?,?)",
            (name, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             self.counters.total, self.counters.new, self.counters.changed,
             self.counters.unchanged))
        self._conn.commit()

    def snapshots(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM snapshots ORDER BY name").fetchall()
        return [str(r[0]) for r in rows]

    def last_snapshot(self) -> str:
        names = self.snapshots()
        return names[-1] if names else ""

    # -- reporting ----------------------------------------------------------- #
    def stats(self) -> dict:
        try:
            tracked = int(self._conn.execute(
                "SELECT COUNT(*) FROM indexed").fetchone()[0])
        except sqlite3.DatabaseError:
            tracked = 0
        return {
            "tracked_documents": tracked,
            "dirty_shards": len(self.dirty_shards()),
            "snapshots": self.snapshots(),
            "last_snapshot": self.last_snapshot(),
            "this_run": self.counters.to_dict(),
        }


def snapshots_since(tracker: ChangeTracker, available) -> list[str]:
    """Names in ``available`` that we have not ingested yet (sorted oldest first)."""
    done = set(tracker.snapshots())
    return sorted(name for name in available if name not in done)
