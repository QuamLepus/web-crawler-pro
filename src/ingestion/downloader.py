"""Distributed async downloader: aiohttp + aioboto3 with S3 streaming.

Design goals
------------
* **Bounded concurrency** - a semaphore caps simultaneous connections so we
  can run 10k+ in-flight requests without exhausting file descriptors.
* **Per-host politeness** - token-bucket limiter per domain, mirroring the
  frontier's guarantees for one-shot bulk downloads.
* **Retries with backoff + jitter** - transient 429/5xx/network errors are
  retried; permanent errors (404, 403) fail fast.
* **S3 direct streaming** - ``aioboto3`` transfers go straight to disk or a
  MinIO/S3 sink without loading whole objects into RAM (multipart upload).
* **Circuit breaker** - repeated host failures trip an open circuit so we
  stop burning proxy quota on dead origins.

The public entry points are :class:`AsyncDownloader` (HTTP) and
:class:`S3Streamer` (object storage); both are used by Celery tasks in
``workers/crawl_tasks.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Per-host token bucket
# --------------------------------------------------------------------------- #
class TokenBucket:
    """Classic token bucket; async-friendly via short sleeps."""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep((n - self._tokens) / self.rate)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
@dataclass
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures for ``recovery`` seconds."""

    threshold: int = 5
    recovery: float = 60.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def allow(self, host: str) -> bool:
        opened = self._opened_at.get(host)
        if opened is None:
            return True
        if time.monotonic() - opened >= self.recovery:
            # half-open: let one probe through, reset bookkeeping
            self._opened_at.pop(host, None)
            self._failures[host] = 0
            return True
        return False

    def record_success(self, host: str) -> None:
        self._failures[host] = 0
        self._opened_at.pop(host, None)

    def record_failure(self, host: str) -> None:
        self._failures[host] = self._failures.get(host, 0) + 1
        if self._failures[host] >= self.threshold and host not in self._opened_at:
            self._opened_at[host] = time.monotonic()
            logger.warning("circuit OPEN for host %s", host)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class DownloadResult:
    url: str
    status: int | None
    ok: bool
    body: bytes | None
    path: str | None = None
    error: str | None = None
    attempts: int = 0
    elapsed_ms: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# HTTP downloader
# --------------------------------------------------------------------------- #
class AsyncDownloader:
    """High-concurrency HTTP fetcher with politeness, retries and breakers."""

    def __init__(
        self,
        *,
        max_concurrency: int = int(os.getenv("DL_MAX_CONCURRENCY", "200")),
        per_host_rate: float = float(os.getenv("DL_PER_HOST_RPS", "2")),
        timeout: float = float(os.getenv("DL_TIMEOUT", "30")),
        retries: int = int(os.getenv("DL_RETRIES", "3")),
        user_agent: str = os.getenv(
            "DL_USER_AGENT", "Mozilla/5.0 (compatible; WebCrawlerPro-Collector/1.0)"
        ),
        proxy: str | None = None,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.per_host_rate = per_host_rate
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.proxy = proxy
        self._buckets: dict[str, TokenBucket] = {}
        self.breaker = CircuitBreaker()

    def _bucket_for(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self.per_host_rate, max(1, int(self.per_host_rate)))
        return self._buckets[host]

    async def download(self, url: str, *, dest_path: str | None = None) -> DownloadResult:
        """Fetch one URL honoring all limits. Safe to call from gather()."""
        import aiohttp

        host = urlparse(url).netloc
        start = time.monotonic()
        result = DownloadResult(url=url, status=None, ok=False, body=None, path=dest_path)
        if not self.breaker.allow(host):
            result.error = "circuit-open"
            return result
        await self._bucket_for(host).acquire()
        sem = getattr(self, "_sem", None)
        if sem is None:  # created lazily bound to the running loop
            sem = self._sem = asyncio.Semaphore(self.max_concurrency)
        connector = aiohttp.TCPConnector(limit=self.max_concurrency, ssl=False)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with sem:
            async for attempt in _attempts(self.retries):
                result.attempts = attempt + 1
                try:
                    async with aiohttp.ClientSession(
                        connector=connector, timeout=timeout
                    ) as session:
                        async with session.get(
                            url,
                            proxy=self.proxy,
                            headers={"User-Agent": self.user_agent},
                        ) as resp:
                            result.status = resp.status
                            result.headers = dict(resp.headers)
                            if resp.status in RETRY_STATUSES and attempt < self.retries - 1:
                                delay = _backoff_delay(attempt, resp.headers.get("Retry-After"))
                                await asyncio.sleep(delay)
                                continue
                            if resp.status >= 400:
                                result.error = f"http-{resp.status}"
                                self.breaker.record_failure(host)
                                break
                            body = await resp.read()
                            if dest_path:
                                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                                with open(dest_path, "wb") as fh:
                                    fh.write(body)
                                result.body = None
                            else:
                                result.body = body
                            result.ok = True
                            self.breaker.record_success(host)
                            break
                except Exception as exc:  # noqa: BLE001 - network layer catch-all
                    result.error = f"{type(exc).__name__}: {exc}"
                    self.breaker.record_failure(host)
                    if attempt < self.retries - 1:
                        await asyncio.sleep(_backoff_delay(attempt))
                        continue
                    break
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    async def download_many(
        self, urls: Iterable[str], *, on_result: Callable[[DownloadResult], Awaitable[None]] | None = None
    ) -> list[DownloadResult]:
        """Fan out downloads; returns results in completion order preserved list."""
        tasks = [asyncio.create_task(self.download(u)) for u in urls]
        results: list[DownloadResult] = []
        for coro in asyncio.as_completed(tasks):
            res = await coro
            results.append(res)
            if on_result:
                await on_result(res)
        ok = sum(1 for r in results if r.ok)
        logger.info("download_many: %d/%d ok", ok, len(results))
        return results

    def run(self, urls: Sequence[str]) -> list[DownloadResult]:  # sync convenience
        return asyncio.run(self.download_many(urls))


def _backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Exponential backoff with full jitter; honors Retry-After when sane."""
    if retry_after:
        try:
            ra = float(retry_after)
            if 0 <= ra <= 300:
                return ra
        except ValueError:
            pass
    base = min(2 ** attempt, 30)
    return random.uniform(0, base)  # noqa: S311 - jitter, not security


async def _attempts(n: int):
    for i in range(max(1, n)):
        yield i


# --------------------------------------------------------------------------- #
# S3 / MinIO streaming
# --------------------------------------------------------------------------- #
class S3Streamer:
    """Stream large objects with aioboto3 without buffering whole bodies.

    * ``download_to_file`` uses ``get_object`` + chunked read into disk.
    * ``upload_from_file`` uses managed transfer config (multipart, parallel).
    Works against any SigV4 endpoint: AWS, MinIO cluster, Cloudflare R2.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        region: str = os.getenv("AWS_REGION", "us-east-1"),
        access_key: str | None = None,
        secret_key: str | None = None,
        part_size_mb: int = 64,
        concurrency: int = 8,
    ) -> None:
        self.endpoint_url = endpoint_url or os.getenv("S3_ENDPOINT_URL") or None
        self.region = region
        self.access_key = access_key or os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_key = secret_key or os.getenv("AWS_SECRET_ACCESS_KEY")
        self.part_size = part_size_mb * 1024 * 1024
        self.concurrency = concurrency

    def _session(self):
        import aioboto3  # lazy: optional dependency

        return aioboto3.Session(
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    async def download_to_file(self, bucket: str, key: str, local_path: str) -> str:
        from aioboto3 import s3  # noqa: F401  (ensures module availability)

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        async with self._session().client(
            "s3", endpoint_url=self.endpoint_url
        ) as client:
            obj = await client.get_object(Bucket=bucket, Key=key)
            tmp = local_path + ".part"
            with open(tmp, "wb") as fh:
                async for chunk in obj["Body"].iter_chunks(ChunkSize=1 << 20):
                    fh.write(chunk)
            os.replace(tmp, local_path)  # atomic publish
        logger.info("s3://%s/%s -> %s", bucket, key, local_path)
        return local_path

    async def upload_from_file(self, bucket: str, key: str, local_path: str) -> str:
        import aioboto3
        from boto3.s3.transfer import TransferConfig  # boto3 ships sync config obj

        config = TransferConfig(
            multipart_threshold=self.part_size,
            multipart_chunksize=self.part_size,
            max_concurrency=self.concurrency,
            use_threads=False,
        )
        async with self._session().client("s3", endpoint_url=self.endpoint_url) as client:
            fut = await client.upload_file(local_path, bucket, key, Config=config)
            if hasattr(fut, "__await__"):  # defensive: some versions return awaitable
                await fut
        del aioboto3  # silence linters about unused lazy import pattern
        logger.info("%s -> s3://%s/%s", local_path, bucket, key)
        return f"s3://{bucket}/{key}"

    async def list_keys(self, bucket: str, prefix: str, max_keys: int = 10_000) -> list[str]:
        keys: list[str] = []
        async with self._session().client("s3", endpoint_url=self.endpoint_url) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    keys.append(item["Key"])
                    if len(keys) >= max_keys:
                        return keys
        return keys
