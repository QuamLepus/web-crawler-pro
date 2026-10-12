"""Parquet writer + MinIO/S3 uploader.

Items are buffered and flushed to partitioned Parquet files
(`dataset/year=YYYY/month=MM/day=DD/part-*.parquet`), then optionally
pushed to a MinIO bucket (S3 API).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


class ParquetWriter:
    def __init__(self, dataset_dir: str = "data/lake", flush_every: int = 1000):
        self.dataset_dir = dataset_dir
        self.flush_every = flush_every
        self._buffer: list[dict] = []

    def add(self, item: dict) -> str | None:
        self._buffer.append(item)
        if len(self._buffer) >= self.flush_every:
            return self.flush()
        return None

    def flush(self) -> str | None:
        if not self._buffer:
            return None
        import polars as pl

        now = datetime.now(timezone.utc)
        part_dir = os.path.join(self.dataset_dir,
                                f"year={now:%Y}", f"month={now:%m}", f"day={now:%d}")
        os.makedirs(part_dir, exist_ok=True)
        path = os.path.join(
            part_dir,
            f"part-{now:%H%M%S}-{os.getpid()}.parquet",
        )
        df = pl.DataFrame(self._buffer)
        # normalize list columns to JSON strings for stable schema
        for col in df.columns:
            if df[col].dtype == pl.List(pl.Utf8):
                df = df.with_columns(df[col].cast(pl.String))
        df.write_parquet(path)
        self._buffer.clear()
        return path


class MinIOUploader:
    """Upload local files/dirs to an S3-compatible bucket (MinIO)."""

    def __init__(self, endpoint: str | None = None, access_key: str | None = None,
                 secret_key: str | None = None, bucket: str = "crawl-data",
                 secure: bool = False):
        self.endpoint = endpoint or os.getenv("MINIO_ENDPOINT", "localhost:9000")
        self.access_key = access_key or os.getenv("MINIO_ACCESS_KEY", "minioadmin")
        self.secret_key = secret_key or os.getenv("MINIO_SECRET_KEY", "minioadmin")
        self.bucket = bucket
        self.secure = secure
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from minio import Minio

            self._client = Minio(self.endpoint, access_key=self.access_key,
                                 secret_key=self.secret_key, secure=self.secure)
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        return self._client

    def upload_file(self, local_path: str, object_name: str | None = None) -> str:
        object_name = object_name or os.path.basename(local_path)
        self.client.fput_object(self.bucket, object_name, local_path)
        return f"s3://{self.bucket}/{object_name}"

    def upload_directory(self, local_dir: str, prefix: str = "") -> list[str]:
        uploaded = []
        for root, _, files in os.walk(local_dir):
            for f in files:
                lp = os.path.join(root, f)
                rel = os.path.relpath(lp, local_dir)
                uploaded.append(self.upload_file(lp, f"{prefix}{rel}"))
        return uploaded
