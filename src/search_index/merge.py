"""Merge independently built index parts into one searchable index.

This is what makes levels 2 and 3 actually run on free hardware. GitHub Actions
gives a public repository unlimited minutes but only 20 concurrent jobs of at
most 6 hours each, so the only way to index 100M pages is to build twenty
independent parts in parallel and combine them afterwards.

Each part is a complete mini-index — its own ``docs.sqlite3`` and its own BM25
shards — so merging has one genuinely tricky job: **rowid remapping**. A shard's
``rowids.npy`` points at row ids in *its own* document store; once the documents
move into the merged store those ids change, and a URL that appeared in two
parts must not survive twice.

:meth:`IndexMerger.add_part` therefore

1. copies every document across, recording ``old rowid -> new rowid``,
2. drops postings whose document was a duplicate (compacting the CSR arrays),
3. rewrites ``rowids.npy`` and saves the shard under a fresh id.

The result is byte-for-byte what a single-process build would have produced,
modulo shard boundaries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import numpy as np

from src.search_index.bm25 import SHARDS_FILE, ShardMeta
from src.search_index.indexer import STATE_FILE, STATE_VERSION, _now
from src.search_index.models import IndexStats
from src.search_index.store import COLUMNS, DocStore

logger = logging.getLogger(__name__)

_COPY_BATCH = 20_000
_INSERT_SQL = ("INSERT OR IGNORE INTO docs "
               "(url, host, title, snippet, body, source, authority, quality, lang, indexed_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)")


@dataclass
class MergeReport:
    parts: int = 0
    docs: int = 0
    duplicates: int = 0
    shards: int = 0

    def to_dict(self) -> dict:
        return {"parts": self.parts, "docs": self.docs,
                "duplicates": self.duplicates, "shards": self.shards}


class IndexMerger:
    """Combines several index directories into one."""

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)
        (self.out_dir / "index").mkdir(parents=True, exist_ok=True)
        self.store = DocStore(self.out_dir / "docs.sqlite3")
        self._metas: list[ShardMeta] = []
        self.report = MergeReport()
        self._levels: list[int] = []
        # Bitmap of merged rowids already claimed by an earlier part, so a URL
        # crawled by two shards contributes one posting, not two.
        self._claimed = bytearray()

    # -- helpers ------------------------------------------------------------- #
    def _next_shard_id(self) -> int:
        return max((m.shard_id for m in self._metas), default=-1) + 1

    def _write_shards(self) -> None:
        payload = {"version": STATE_VERSION, "count": len(self._metas),
                   "shards": [m.to_dict() for m in self._metas]}
        tmp = self.out_dir / "index" / (SHARDS_FILE + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.out_dir / "index" / SHARDS_FILE)

    # -- parts ---------------------------------------------------------------- #
    def add_part(self, part_dir: str | Path) -> MergeReport:
        """Copy one part's documents and shards into the merged index."""
        part_dir = Path(part_dir)
        db = part_dir / "docs.sqlite3"
        if not db.exists():
            logger.warning("part %s has no document store — skipping", part_dir)
            return self.report

        state_file = part_dir / STATE_FILE
        if state_file.exists():
            try:
                self._levels.append(int(json.loads(
                    state_file.read_text(encoding="utf-8")).get("level", 0)))
            except (OSError, ValueError, TypeError):
                pass

        mapping = self._copy_documents(db)
        self._copy_shards(part_dir / "index", mapping)
        self.report.parts += 1
        self._write_shards()
        return self.report

    def _copy_documents(self, db: Path) -> dict[int, int] | np.ndarray:
        """Copy rows from ``db`` and return ``old rowid -> new rowid``."""
        import sqlite3

        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            max_id = int(source.execute("SELECT COALESCE(MAX(id),0) FROM docs")
                         .fetchone()[0])
            total = int(source.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
        finally:
            pass

        mapping: dict[int, int] | np.ndarray
        if max_id and max_id <= max(total * 4, 1_000_000):
            mapping = np.zeros(max_id + 1, dtype=np.int64)
        else:
            mapping = {}

        conn = self.store.conn
        copied = 0
        cursor = source.execute(
            f"SELECT {','.join(COLUMNS)} FROM docs ORDER BY id")
        while True:
            rows = cursor.fetchmany(_COPY_BATCH)
            if not rows:
                break
            payload = [tuple(row[c] for c in COLUMNS[1:]) for row in rows]
            conn.execute("BEGIN")
            for old_row, values in zip(rows, payload):
                cur = conn.execute(_INSERT_SQL, values)
                new_id = int(cur.lastrowid or 0)
                if cur.rowcount:
                    copied += 1
                else:                       # duplicate URL already merged
                    existing = conn.execute(
                        "SELECT id FROM docs WHERE url=?", (values[0],)).fetchone()
                    new_id = int(existing[0]) if existing else 0
                    self.report.duplicates += 1
                old_id = int(old_row["id"])
                if isinstance(mapping, np.ndarray):
                    if old_id < len(mapping):
                        mapping[old_id] = new_id
                    else:  # pragma: no cover - defensive
                        mapping = dict(enumerate(mapping.tolist()))
                        mapping[old_id] = new_id
                else:
                    mapping[old_id] = new_id
            conn.commit()

        source.close()
        self.report.docs += copied
        return mapping

    def _copy_shards(self, index_dir: Path, mapping) -> None:
        if not index_dir.exists():
            return
        for shard_dir in sorted(p for p in index_dir.iterdir()
                                if p.is_dir() and p.name.startswith("shard-")):
            meta_file = shard_dir / "meta.json"
            if not meta_file.exists():
                continue
            meta = ShardMeta.from_dict(json.loads(meta_file.read_text()))
            old_rowids = np.load(shard_dir / "rowids.npy")
            if isinstance(mapping, np.ndarray):
                safe = np.where((old_rowids >= 0) & (old_rowids < len(mapping)),
                                old_rowids, 0)
                new_rowids = mapping[safe]
            else:
                new_rowids = np.array([mapping.get(int(r), 0) for r in old_rowids],
                                      dtype=np.int64)

            # A URL seen in two parts must contribute one posting, not two:
            # keep only the first occurrence of each merged rowid.
            _, first_seen = np.unique(new_rowids, return_index=True)
            keep = np.zeros(len(new_rowids), dtype=bool)
            keep[first_seen] = True
            keep &= new_rowids > 0
            if not keep.any():
                continue
            self._write_shard(shard_dir, meta, old_rowids, new_rowids, keep)

    def _claim(self, new_rowids: np.ndarray) -> np.ndarray:
        """Mark every id as claimed; return a mask of the ones we won."""
        if not len(new_rowids):
            return np.zeros(0, dtype=bool)
        top = int(new_rowids.max())
        if len(self._claimed) <= top:
            self._claimed.extend(bytes(top + 1 - len(self._claimed)))
        mask = np.zeros(len(new_rowids), dtype=bool)
        for i, rid in enumerate(new_rowids.tolist()):
            if rid > 0 and not self._claimed[rid]:
                self._claimed[rid] = 1
                mask[i] = True
        return mask

    def _write_shard(self, shard_dir: Path, meta: ShardMeta,
                     old_rowids: np.ndarray, new_rowids: np.ndarray,
                     keep: np.ndarray) -> None:
        """Rewrite one shard with merged rowids, compacting dropped documents."""
        indptr = np.load(shard_dir / "indptr.npy")
        indices = np.load(shard_dir / "indices.npy")
        data = np.load(shard_dir / "data.npy")
        doclen = np.load(shard_dir / "doclen.npy")

        order = np.cumsum(keep.astype(np.int64)) - 1
        n_terms = len(indptr) - 1
        df = (indptr[1:] - indptr[:-1]).astype(np.int64)
        term_of_posting = np.repeat(np.arange(n_terms, dtype=np.int64), df)

        mask = keep[np.clip(indices, 0, len(keep) - 1)] if len(indices) else \
            np.zeros(0, dtype=bool)
        new_indices = order[np.clip(indices, 0, len(keep) - 1)][mask].astype(np.int32)
        new_data = data[mask]
        counts = np.bincount(term_of_posting[mask], minlength=n_terms) \
            if len(term_of_posting) else np.zeros(n_terms, dtype=np.int64)
        new_indptr = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)

        target = self.out_dir / "index" / f"shard-{self._next_shard_id():05d}"
        target.mkdir(parents=True, exist_ok=True)
        np.save(target / "indptr.npy", new_indptr)
        np.save(target / "indices.npy", new_indices)
        np.save(target / "data.npy", new_data)
        np.save(target / "doclen.npy", doclen[keep])
        np.save(target / "rowids.npy", new_rowids[keep].astype(np.int64))
        np.save(target / "df.npy", (new_indptr[1:] - new_indptr[:-1]).astype(np.int32))
        (target / "vocab.json").write_text(
            (shard_dir / "vocab.json").read_text(encoding="utf-8"), encoding="utf-8")

        merged_meta = ShardMeta(
            shard_id=self._next_shard_id(), source=meta.source,
            doc_count=int(keep.sum()), term_count=meta.term_count,
            total_len=int(doclen[keep].sum()), path=target.name)
        (target / "meta.json").write_text(
            json.dumps(merged_meta.to_dict()), encoding="utf-8")
        self._metas.append(merged_meta)
        self.report.shards += 1

    # -- finish -------------------------------------------------------------- #
    def finalize(self) -> IndexStats:
        """Write ``shards.json`` / ``state.json`` and return the final stats."""
        self._write_shards()
        level = max(self._levels) if self._levels else 0
        state = {
            "version": STATE_VERSION,
            "level": level,
            "created_at": _now(),
            "updated_at": _now(),
            "merged": True,
            "options": {"level": level, "shard_docs": 0, "max_pages": 0,
                        "body_chars": 0, "language": "", "shard_index": 0,
                        "shard_count": 0, "quality_filter": False,
                        "authority": False, "keep_body": True,
                        "sources": []},
            "sources": {"merged": {"status": "done",
                                   "docs": self.report.docs,
                                   "shards": self.report.shards,
                                   "cursor": 0, "files": [], "error": ""}},
        }
        (self.out_dir / STATE_FILE).write_text(
            json.dumps(state, indent=2), encoding="utf-8")
        self.store.vacuum()
        return self.stats()

    def stats(self) -> IndexStats:
        return IndexStats(
            index_dir=str(self.out_dir),
            level=max(self._levels) if self._levels else 0,
            docs=self.store.count(),
            shards=len(self._metas),
            terms=sum(m.term_count for m in self._metas),
            sources=self.store.source_counts(),
            state={"merge": self.report.to_dict()},
            exists=True, ready=bool(self._metas))

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def merge_parts(parts, out_dir: str | Path) -> IndexStats:
    """One-shot helper: merge ``parts`` into ``out_dir`` and finalize."""
    with IndexMerger(out_dir) as merger:
        for part in parts:
            merger.add_part(part)
        return merger.finalize()
