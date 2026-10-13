"""Massive-scale data ingestion subsystem (LLM training focus).

Modules:
    commoncrawl  - Common Crawl Index querying + WARC/WET/WAT processing (warcio).
    hf_datasets  - Hugging Face Datasets streaming-mode connector.
    downloader   - aiohttp + aioboto3 distributed async downloader with S3 streaming.
    registry     - Data Source Registry (RSS, sitemaps, APIs, S3 buckets, ...).
"""

from src.ingestion.registry import (
    DataSourceKind,
    DataSourceRecord,
    DataSourceRegistry,
)

__all__ = [
    "DataSourceKind",
    "DataSourceRecord",
    "DataSourceRegistry",
]
