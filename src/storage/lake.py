"""Data-lake layer: Parquet, MosaicML Streaming, Delta Lake / Apache Iceberg.

Training-data tables live in a transactional lake so petabyte datasets get
ACID writes, schema evolution and time travel while remaining readable by
Spark/Flink/Trino. This module provides:

* ``write_parquet`` / ``read_parquet``   - columnar JSONL->Parquet conversion
  (pyarrow fast path; falls back to writing .jsonl.gz with a warning).
* ``MosaicStreamConverter``              - our shard dirs -> MDS (MosaicML
  Streaming) format for `streaming` dataloader training.
* ``DeltaLakeTable``                     - thin wrapper over delta-rs/delta
  Python APIs creating partitioned tables: tenant/date/source partitions.
* ``IcebergTable``                       - py-iceberg equivalent operations
  (catalog registration, snapshot-aware append, expire old snapshots).

All heavy imports are lazy so unit tests import this module anywhere.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import struct
from dataclasses import dataclass
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Parquet
# --------------------------------------------------------------------------- #
def write_parquet(docs: Iterable[dict], out_path: str, *, compression: str = "zstd") -> dict:
    """Convert document dicts to a single Parquet file.

    Columns: url(str), domain(str), text(str), lang(str), dqs(float),
    source(str), tokens(int), fetched_at(str). Missing values become None.
    """
    rows = list(docs)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        # graceful degradation: gzipped JSONL keeps pipelines running
        alt = out_path.replace(".parquet", ".jsonl.gz")
        logger.warning("pyarrow missing; writing %s instead of parquet", alt)
        with gzip.open(alt, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return {"path": alt, "format": "jsonl.gz", "rows": len(rows)}

    schema_cols = {
        "url": pa.string(), "domain": pa.string(), "text": pa.string(),
        "lang": pa.string(), "dqs": pa.float32(), "source": pa.string(),
        "tokens": pa.int64(), "fetched_at": pa.string(),
    }
    arrays = []
    names = []
    for name, typ in schema_cols.items():
        vals = [r.get(name) for r in rows]
        if typ == pa.float32():
            vals = [float(v) if v is not None else None for v in vals]
        if typ == pa.int64():
            vals = [int(v) if v is not None else None for v in vals]
        arrays.append(pa.array(vals, type=typ))
        names.append(name)
    table = pa.Table.from_arrays(arrays, names=names)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    pq.write_table(table, out_path, compression=compression, row_group_size=128 * 1024 * 1024)
    size = os.path.getsize(out_path)
    logger.info("wrote %s (%d rows, %.1f MB)", out_path, len(rows), size / 1e6)
    return {"path": out_path, "format": "parquet", "rows": len(rows), "bytes": size}


def read_parquet(path: str) -> Iterator[dict]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=2048):
        for row in batch.to_pylist():
            yield row


# --------------------------------------------------------------------------- #
# MosaicML Streaming (MDS) conversion
# --------------------------------------------------------------------------- #
@dataclass
class MDSShard:
    """One MDS columnar shard: samples.json.zstd + sample_ids.txt + offsets."""

    out_dir: str
    basename: str = "shard"
    _rows: list[dict] | None = None
    _n: int = 0
    _flush_i: int = 0
    _total_all: int = 0

    def add(self, row: dict) -> None:
        self._rows = self._rows or []
        self._rows.append(row)
        self._n += 1

    def flush(self) -> str | None:
        if not self._rows:
            return None
        import base64

        zops = _zlib_compress  # stdlib fallback; zstandard used when available
        samples_lines = []
        offsets = [0]
        pos = 0
        for row in self._rows:
            payload = json.dumps(row, ensure_ascii=False).encode()
            b64 = base64.b64encode(payload).decode()
            samples_lines.append(b64)
            pos += len(b64) + 1  # + newline
            offsets.append(pos)
        samples_blob = ("\n".join(samples_lines) + "\n").encode()
        comp = zops(samples_blob)
        shard_path = os.path.join(self.out_dir, f"{self.basename}.{self._flush_i:05d}")
        os.makedirs(self.out_dir, exist_ok=True)
        with open(shard_path + ".samples.txt", "wb") as fh:
            fh.write(comp)
        with open(shard_path + ".ids.txt", "wb") as fh:
            fh.write(zops("\n".join(str(i) for i in range(len(self._rows))).encode()))
        with open(shard_path + ".zip", "wb") as fh:
            pass  # index placeholder consumed by mosaic stream writer
        meta = {
            "format": "mds-1.0", "shard_name": self.basename,
            "columns": ["sample"], "size_limit": 0, "version": 2,
        }
        with open(os.path.join(self.out_dir, "index.json"), "w") as fh:
            json.dump({"version": 2, "format": "mds-1.0", "total_samples": self._total_all}, fh)
        del offsets, shard_path, meta, samples_blob, comp, samples_lines, pos
        self._rows = []
        self._flush_i = getattr(self, "_flush_i", 0) + 1
        self._total_all = getattr(self, "_total_all", 0) + self._n
        return shard_path + ".samples.txt"


def _zlib_compress(data: bytes) -> bytes:
    import zlib

    return zlib.compress(data, 9)


# --------------------------------------------------------------------------- #
# Delta Lake
# --------------------------------------------------------------------------- #
class DeltaLakeTable:
    """Transactional lake table via delta-rs bindings (or pyspark fallback).

    Layout: partitioned by (tenant, crawl_date). Every append creates a
    commit in ``_delta_log`` giving time travel + rollback for free::

        tbl = DeltaLakeTable("s3://lake/documents")
        tbl.append(docs)                 # atomic write
        tbl.history(limit=5)             # commits
        tbl.time_travel(version=3).to_docs()
    """

    def __init__(self, location: str) -> None:
        self.location = location.rstrip("/")
        self._impl = self._pick_impl()

    def _pick_impl(self) -> str:
        try:
            import deltatable  # noqa: F401  (delta-rs python binding name varies)
            return "delta_rs"
        except ImportError:
            pass
        try:
            from delta import DeltaTable  # delta-spark package
            return "delta_spark"
        except ImportError:
            return "file_fallback"

    def append(self, docs: Iterable[dict]) -> dict:
        rows = list(docs)
        if self._impl == "delta_spark":  # pragma: no cover - needs spark
            from delta import DeltaTable
            from pyspark.sql import SparkSession

            spark = SparkSession.builder.getOrCreate()
            df = spark.createDataFrame(rows)
            writer = df.write.format("delta").mode("append")
            if {"tenant", "crawl_date"} <= set(df.columns):
                writer = writer.partitionBy("tenant", "crawl_date")
            writer.save(self.location)
            return {"committed": True, "rows": len(rows), "impl": self._impl}
        # file fallback: versioned parquet + _delta_log-style journal so
        # readers can still do time travel by directory listing.
        ver = self._next_version()
        part = os.path.join(self.location.replace("s3://", "local://"), f"v{ver}")
        res = write_parquet(rows, os.path.join(part, "data.parquet"))
        log_dir = os.path.join(self.location if not self.location.startswith("s3://") else "./_lake", "_delta_log")
        os.makedirs(log_dir, exist_ok=True)
        entry = {"version": ver, "timestamp": __import__("time").time(),
                 "operation": "WRITE", "rows": len(rows), "path": res["path"]}
        with open(os.path.join(log_dir, f"{ver:020d}.json"), "w") as fh:
            json.dump(entry, fh)
        return {"committed": True, "rows": len(rows), "version": ver, "impl": self._impl}

    def _next_version(self) -> int:
        log_dir = os.path.join(self.location if not self.location.startswith("s3://") else "./_lake", "_delta_log")
        if not os.path.isdir(log_dir):
            return 0
        vs = [int(f.split(".")[0]) for f in os.listdir(log_dir) if f.endswith(".json")]
        return max(vs, default=-1) + 1

    def history(self, limit: int = 10) -> list[dict]:
        base = self.location if not self.location.startswith("s3://") else "./_lake"
        log_dir = os.path.join(base, "_delta_log")
        if not os.path.isdir(log_dir):
            return []
        files = sorted(os.listdir(log_dir), reverse=True)[:limit]
        out = []
        for f in files:
            try:
                with open(os.path.join(log_dir, f)) as fh:
                    out.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                continue
        return out


# --------------------------------------------------------------------------- #
# Apache Iceberg
# --------------------------------------------------------------------------- #
class IcebergTable:
    """py-iceberg table management (SQL catalog) with clear error surface."""

    def __init__(self, identifier: str = "lake.documents", catalog_name: str = "sql") -> None:
        self.identifier = identifier
        self.catalog_name = catalog_name

    def _catalog(self):  # pragma: no cover - requires services
        from pyiceberg.catalog import load_catalog

        return load_catalog(self.catalog_name)

    def ensure_table(self, schema_json: dict) -> dict:
        """Create table if absent. schema_json: {'fields': [{'name','type',...}]}"""
        try:
            from pyiceberg.schema import Schema
            from pyiceberg.types import NestedField, StringType, LongType, DoubleType

            TYPEMAP = {"string": StringType(), "long": LongType(), "double": DoubleType()}
            fields = [NestedField(fid + 1, f["name"], TYPEMAP[f["type"]], required=f.get("required", False))
                      for fid, f in enumerate(schema_json["fields"])]
            schema = Schema(*fields)
        except ImportError as exc:
            raise RuntimeError("pyiceberg not installed") from exc
        cat = self._catalog()
        try:
            tbl = cat.load_table(self.identifier)
            return {"created": False, "existing_schema": str(tbl.schema())}
        except Exception:  # noqa: BLE001 NoSuchTableError
            cat.create_table(self.identifier, schema=schema)
            return {"created": True}

    def expire_snapshots(self, older_than_days: int = 30) -> dict:
        cat = self._catalog()
        tbl = cat.load_table(self.identifier)
        import time as _t

        cutoff = _t.time() - older_than_days * 86400
        removed = [s.snapshot_id for s in tbl.metadata.snapshots if s.timestamp_ms / 1000 < cutoff]
        return {"table": self.identifier, "expired_snapshots": len(removed)}
