"""Configuration management for Web Crawler Pro"""

import os
from functools import lru_cache


class Config:
    """Central configuration. Values can be overridden via environment variables."""

    # HTTP settings
    USER_AGENT = os.getenv(
        "CRAWLER_USER_AGENT",
        "Mozilla/5.0 (compatible; WebCrawlerPro/1.0)"
    )
    REQUEST_TIMEOUT = int(os.getenv("CRAWLER_REQUEST_TIMEOUT", "30"))
    RETRY_ATTEMPTS = int(os.getenv("CRAWLER_RETRY_ATTEMPTS", "3"))

    # Politeness / rate limiting
    DELAY_BETWEEN_REQUESTS = float(os.getenv("CRAWLER_DELAY", "1.0"))
    MAX_CONCURRENT_REQUESTS = int(os.getenv("CRAWLER_MAX_CONCURRENT", "10"))

    # Scope
    MAX_PAGES_PER_CRAWL = int(os.getenv("CRAWLER_MAX_PAGES", "100"))
    ALLOWED_DOMAINS = os.getenv("CRAWLER_ALLOWED_DOMAINS", "").split(",") if os.getenv("CRAWLER_ALLOWED_DOMAINS") else []

    # Logging
    LOG_LEVEL = os.getenv("CRAWLER_LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("CRAWLER_LOG_FILE", "")  # empty => console only

    # Storage
    OUTPUT_DIR = os.getenv("CRAWLER_OUTPUT_DIR", "output")

    # Redis (for future frontier / Celery)
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return a cached singleton Config instance."""
    return Config()
