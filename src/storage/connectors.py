"""Cloud object-storage connectors: S3/MinIO, GCS, Azure Blob, Backblaze B2.

One small interface (``StorageConnector``) with four implementations so the
rest of the platform never branches on provider. All methods are sync; async
bulk transfers live in ``src.ingestion.downloader.S3Streamer``.

Credentials come from environment per provider; each connector also supports
explicit constructor args for multi-tenant BYO-bucket setups.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class StorageConnector(Protocol):
    provider: str

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list_prefix(self, prefix: str) -> Iterator[str]: ...
    def signed_url(self, key: str, expires_seconds: int = 3600) -> str: ...
    def walk(self, prefix: str) -> Iterator[tuple[str, int]]: ...  # (key, size)


# --------------------------------------------------------------------------- #
# S3 / MinIO
# --------------------------------------------------------------------------- #
@dataclass
class S3Connector:
    provider: str = "s3"
    bucket: str = os.getenv("S3_BUCKET", "crawler-data")
    endpoint_url: str | None = os.getenv("S3_ENDPOINT_URL")  # MinIO sets this
    region: str = os.getenv("AWS_REGION", "us-east-1")
    access_key: str | None = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        self._client().put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        return self._client().get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._client().delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client().head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 botocore ClientError
            return False

    def list_prefix(self, prefix: str) -> Iterator[str]:
        client = self._client()
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for item in resp.get("Contents", []):
                yield item["Key"]
            token = resp.get("NextContinuationToken")
            if not token:
                return

    def walk(self, prefix: str) -> Iterator[tuple[str, int]]:
        client = self._client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"], item["Size"]

    def signed_url(self, key: str, expires_seconds: int = 3600) -> str:
        return self._client().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


# --------------------------------------------------------------------------- #
# Google Cloud Storage
# --------------------------------------------------------------------------- #
class GCSConnector:
    provider = "gcs"

    def __init__(self, bucket: str | None = None, project: str | None = None) -> None:
        self.bucket_name = bucket or os.getenv("GCS_BUCKET", "crawler-data")
        self.project = project or os.getenv("GCP_PROJECT")

    def _bucket(self):
        from google.cloud import storage

        client = storage.Client(project=self.project)
        return client.bucket(self.bucket_name)

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        blob = self._bucket().blob(key)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{key}"

    def get(self, key: str) -> bytes:
        return self._bucket().blob(key).download_as_bytes()

    def delete(self, key: str) -> None:
        self._bucket().blob(key).delete()

    def exists(self, key: str) -> bool:
        return self._bucket().blob(key).exists()

    def list_prefix(self, prefix: str) -> Iterator[str]:
        from google.cloud import storage

        client = storage.Client(project=self.project)
        for b in client.list_blobs(self.bucket_name, prefix=prefix):
            yield b.name

    def walk(self, prefix: str) -> Iterator[tuple[str, int]]:
        from google.cloud import storage

        client = storage.Client(project=self.project)
        for b in client.list_blobs(self.bucket_name, prefix=prefix):
            yield b.name, b.size or 0

    def signed_url(self, key: str, expires_seconds: int = 3600) -> str:
        from google.cloud import storage

        client = storage.Client(project=self.project)
        blob = client.bucket(self.bucket_name).blob(key)
        return blob.generate_signed_url(expiration=timedelta(seconds=expires_seconds))


# --------------------------------------------------------------------------- #
# Azure Blob
# --------------------------------------------------------------------------- #
class AzureBlobConnector:
    provider = "azure"

    def __init__(self, container: str | None = None, connection_string: str | None = None) -> None:
        self.container = container or os.getenv("AZURE_CONTAINER", "crawler-data")
        self.conn_str = connection_string or os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

    def _client(self):
        from azure.storage.blob import BlobServiceClient

        return BlobServiceClient.from_connection_string(self.conn_str)

    def _c(self):
        return self._client().container_client(self.container)

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        self._c().upload_blob(key, data, content_type=content_type, overwrite=True)
        return f"azure://{self.container}/{key}"

    def get(self, key: str) -> bytes:
        return self._c().download_blob(key).readall()

    def delete(self, key: str) -> None:
        self._c().delete_blob(key)

    def exists(self, key: str) -> bool:
        return self._c().get_blob_client(key).exists()

    def list_prefix(self, prefix: str) -> Iterator[str]:
        for b in self._c().list_blobs(name_starts_with=prefix):
            yield b.name

    def walk(self, prefix: str) -> Iterator[tuple[str, int]]:
        for b in self._c().list_blobs(name_starts_with=prefix):
            yield b.name, b.size

    def signed_url(self, key: str, expires_seconds: int = 3600) -> str:
        from azure.storage.blob import generate_blob_sas

        account_name = next(
            p.split("=", 1)[1]
            for p in self.conn_str.split(";")
            if p.startswith("AccountName")
        )
        account_key = next(
            p.split("=", 1)[1]
            for p in self.conn_str.split(";")
            if p.startswith("AccountKey")
        )
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=self.container,
            blob_name=key,
            account_key=account_key,
            permission="r",
            expiry=datetime_now_plus(expires_seconds),
        )
        return f"https://{account_name}.blob.core.windows.net/{self.container}/{key}?{sas}"


def datetime_now_plus(seconds: int):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# Backblaze B2 (S3-compatible API)
# --------------------------------------------------------------------------- #
class B2Connector(S3Connector):
    """Backblaze B2 exposes an S3-compatible endpoint - reuse S3 code path."""

    provider = "b2"

    def __init__(self, bucket: str | None = None, key_id: str | None = None,
                 app_key: str | None = None, region: str | None = None) -> None:
        super().__init__(
            bucket=bucket or os.getenv("B2_BUCKET", "crawler-data"),
            endpoint_url=os.getenv("B2_ENDPOINT", "https://s3.us-west.backblazeb2.com"),
            region=region or os.getenv("B2_REGION", "us-west-004"),
            access_key=key_id or os.getenv("B2_KEY_ID"),
            secret_key=app_key or os.getenv("B2_APP_KEY"),
        )


CONNECTORS: dict[str, type] = {
    "s3": S3Connector,
    "minio": S3Connector,   # MinIO speaks S3; set S3_ENDPOINT_URL
    "gcs": GCSConnector,
    "azure": AzureBlobConnector,
    "b2": B2Connector,
}


def get_connector(provider: str, **kwargs) -> StorageConnector:
    try:
        cls = CONNECTORS[provider.lower()]
    except KeyError:
        raise ValueError(f"unknown provider {provider!r}; options: {sorted(CONNECTORS)}") from None
    return cls(**kwargs)
