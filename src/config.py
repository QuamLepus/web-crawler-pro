"""Configuration management for Web Crawler Pro.

This module used to define ``Config``/``get_config`` twice — once for the
distributed platform and once for the older single-node crawler — with the
second pair silently shadowing the first. The surviving definition had no
``REDIS_URL`` and no ``MAX_PAGES_PER_CRAWL``, and ``get_config()`` returned a
*class* instead of an instance. Anything reading ``cfg.REDIS_URL`` therefore
died with ``AttributeError``: the URL frontier could not be built and the
shared MinHash dedupe index silently fell back to a per-process one.

Everything is merged into one class here, and ``get_config()`` returns a
cached instance as its annotation promises.
"""

import os
from functools import cache

try:  # optional; settings simply come from the real environment without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        return False


# Must run *before* the class body below reads os.getenv, otherwise values from
# .env never reach the settings (they were previously loaded after the class).
load_dotenv()


def _env(name: str, default=None):
    """Read ``CRAWLER_<NAME>`` first, then the bare ``<NAME>``."""
    if os.getenv(f"CRAWLER_{name}") is not None:
        return os.getenv(f"CRAWLER_{name}")
    if os.getenv(name) is not None:
        return os.getenv(name)
    return default


class Config:
    """Central configuration. Every value is overridable via environment."""

    DEBUG = False

    # -- HTTP ---------------------------------------------------------------- #
    BASE_URL = _env("BASE_URL", "https://example.com")
    USER_AGENT = _env("USER_AGENT", "Mozilla/5.0 (compatible; WebCrawlerPro/1.0)")
    REQUEST_TIMEOUT = int(_env("REQUEST_TIMEOUT", 30))
    RETRY_ATTEMPTS = int(_env("RETRY_ATTEMPTS", 3))

    # -- politeness / rate limiting ------------------------------------------ #
    DELAY_BETWEEN_REQUESTS = float(_env("DELAY", 1.0))
    MAX_CONCURRENT_REQUESTS = int(_env("MAX_CONCURRENT", 10))

    # -- scope ---------------------------------------------------------------- #
    MAX_PAGES_PER_CRAWL = int(_env("MAX_PAGES", 100))
    ALLOWED_DOMAINS = (
        _env("ALLOWED_DOMAINS", "").split(",") if _env("ALLOWED_DOMAINS") else []
    )

    # -- storage -------------------------------------------------------------- #
    OUTPUT_DIR = _env("OUTPUT_DIR", "output")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///crawler.db")

    # -- distributed crawling ------------------------------------------------- #
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # -- proxy ---------------------------------------------------------------- #
    USE_PROXY = os.getenv("USE_PROXY", "False").lower() == "true"
    PROXY_URL = os.getenv("PROXY_URL", "")

    # -- credentials ---------------------------------------------------------- #
    API_KEY = os.getenv("API_KEY", "")

    # -- logging -------------------------------------------------------------- #
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    # Empty means "console only" — src/utils/logger.py skips the file handler.
    LOG_FILE = _env("LOG_FILE", "logs/crawler.log")

    # -- topic discovery (src/discovery) -------------------------------------- #
    # Comma-separated provider list. "duckduckgo" needs no API key; the rest are
    # enabled automatically when their key is present in the environment.
    DISCOVERY_PROVIDERS = _env(
        "DISCOVERY_PROVIDERS", "duckduckgo").replace(" ", "").split(",")
    DISCOVERY_MAX_CANDIDATES = int(_env("DISCOVERY_MAX_CANDIDATES", 300))
    DISCOVERY_MAX_QUERIES = int(_env("DISCOVERY_MAX_QUERIES", 6))
    DISCOVERY_TIMEOUT = int(_env("DISCOVERY_TIMEOUT", 20))
    DISCOVERY_MAX_PAGES_PER_SITE = int(_env("DISCOVERY_MAX_PAGES_PER_SITE", 5))
    # Comma-separated domains that must never be proposed (parked/SEO farms).
    DISCOVERY_BLOCKLIST = [
        d for d in _env("DISCOVERY_BLOCKLIST", "").replace(" ", "").split(",") if d
    ]
    # Optional: let an LLM propose well-known sites for the topic. Off by
    # default so discovery stays free and offline-capable.
    DISCOVERY_USE_LLM = os.getenv("DISCOVERY_USE_LLM", "").lower() == "true"
    DISCOVERY_USE_COMMONCRAWL = (
        os.getenv("DISCOVERY_USE_COMMONCRAWL", "").lower() == "true")

    # Search-provider credentials — all optional. Without any of them the
    # DuckDuckGo HTML endpoint (no key, no cost) is still used.
    BRAVE_API_KEY = _env("BRAVE_API_KEY", "")
    SERPAPI_KEY = _env("SERPAPI_KEY", "")
    TAVILY_API_KEY = _env("TAVILY_API_KEY", "")


class DevelopmentConfig(Config):
    DEBUG = True
    LOG_LEVEL = "DEBUG"


class ProductionConfig(Config):
    DEBUG = False
    LOG_LEVEL = "INFO"


class TestingConfig(Config):
    DEBUG = True
    DATABASE_URL = "sqlite:///:memory:"
    LOG_LEVEL = "DEBUG"


_CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


@cache
def get_config(env=None) -> Config:
    """Return a cached singleton ``Config`` instance.

    ``env`` selects the profile (``development`` / ``production`` /
    ``testing``); it defaults to ``$ENVIRONMENT`` and then to development.
    """
    if env is None:
        env = os.getenv("ENVIRONMENT", "development")
    return _CONFIGS.get(env, DevelopmentConfig)()
