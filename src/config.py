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
