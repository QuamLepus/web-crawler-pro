"""Data Source Registry: pluggable connectors for custom data sources.

Users (SaaS tenants) register sources; the scheduler turns each source into
URL batches pushed onto the Redis frontier, or into dedicated ingestion jobs.

Supported kinds
---------------
* ``rss`` / ``atom``     - feed polling with etag/last-modified tracking.
* ``sitemap``            - sitemap.xml (+ index files, recursive one level).
* ``api``                - paginated JSON APIs (cursor or page-number style).
* ``s3``                 - bucket prefix scans streamed through S3Streamer.
* ``commoncrawl``        - CC index query executed periodically.
* ``website``            - plain domain seed + crawl depth budget.

State lives in Redis so every API node and worker sees the same registry:
  ``sources:{tenant}``          -> HASH source_id -> json(DataSourceRecord)
  ``sources:runs:{source_id}``  -> LIST of recent run summaries (capped)
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


class DataSourceKind(str, Enum):
    RSS = "rss"
    SITEMAP = "sitemap"
    API = "api"
    S3 = "s3"
    COMMONCRAWL = "commoncrawl"
    WEBSITE = "website"


@dataclass
class DataSourceRecord:
    """One registered source. ``config`` payload is kind-specific."""

    id: str
    tenant: str
    name: str
    kind: DataSourceKind
    enabled: bool = True
    schedule_cron: str | None = None          # e.g. "0 * * * *" (Celery beat style)
    config: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_run_at: str | None = None
    last_status: str | None = None

    def validate(self) -> None:
        """Raise ValueError when required per-kind config keys are missing."""
        cfg = self.config
        if self.kind == DataSourceKind.RSS:
            _require(cfg, "url")
            _assert_url(cfg["url"])
        elif self.kind == DataSourceKind.SITEMAP:
            _require(cfg, "url")
            _assert_url(cfg["url"])
        elif self.kind == DataSourceKind.API:
            _require(cfg, "url", "items_path")
            _assert_url(cfg["url"])
            if "url_field" not in cfg and "list_selector" not in cfg:
                raise ValueError("API source needs 'url_field' or 'list_selector'")
        elif self.kind == DataSourceKind.S3:
            _require(cfg, "bucket")
        elif self.kind == DataSourceKind.COMMONCRAWL:
            _require(cfg, "match_text")
        elif self.kind == DataSourceKind.WEBSITE:
            _require(cfg, "seed_url")
            _assert_url(cfg["seed_url"])

    def to_json(self) -> str:
        d = asdict(self)
        d["kind"] = self.kind.value
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "DataSourceRecord":
        d = json.loads(raw)
        d["kind"] = DataSourceKind(d["kind"])
        return cls(**d)


def _require(cfg: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise ValueError(f"missing config keys: {', '.join(missing)}")


def _assert_url(value: str) -> None:
    p = urlparse(value)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"not a valid http(s) URL: {value!r}")


# --------------------------------------------------------------------------- #
# Feed / sitemap parsers (pure functions - easy to unit test)
# --------------------------------------------------------------------------- #
# The `>` belongs *outside* the alternation. It used to sit inside the `url>`
# and `guid[^>]*>` branches but not after `loc`, so `<loc>https://x</loc>` was
# captured as ">https://x" and then rejected by the http(s) prefix check below
# — every SITEMAP source silently expanded to zero URLs.
# `link` (RSS 2.0) belongs here too: the href-only fallback below only ever
# handled Atom, so a plain `<link>https://…</link>` item yielded nothing.
_LOC_RE = re.compile(
    r"<(?:loc|url|link|guid[^>]*)>\s*(?:<!\[CDATA\[)?([^<\]]+)", re.I)
_SITEMAP_INDEX_RE = re.compile(r"<sitemap>", re.I)


def parse_rss_urls(xml_text: str) -> list[str]:
    """Extract entry links from RSS 2.0 / Atom feeds (namespace-tolerant)."""
    urls: list[str] = []
    seen: set[str] = set()
    for m in _LOC_RE.finditer(xml_text):
        u = m.group(1).strip()
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            urls.append(u)
    # Atom <link href="..."> fallback
    for m in re.finditer(r'<link[^>]*href=["\']([^"\']+)["\']', xml_text, re.I):
        u = m.group(1).strip()
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def parse_sitemap(xml_text: str, base_url: str) -> tuple[list[str], list[str]]:
    """Return ``(leaf_urls, child_sitemap_urls)`` for a sitemap or index file."""
    entries = parse_rss_urls(xml_text)
    if _SITEMAP_INDEX_RE.search(xml_text):
        children = [urljoin(base_url, e) for e in entries]
        return [], children
    leaves = [urljoin(base_url, e) for e in entries]
    return leaves, []


def extract_api_urls(payload: Any, items_path: str, url_field: str) -> list[str]:
    """Walk dotted ``items_path`` into a JSON response and pull ``url_field``.

    ``items_path`` example: ``data.results.items`` ; supports lists at leaf.
    """
    node = payload
    for part in items_path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return []
    if not isinstance(node, list):
        return []
    out = []
    for item in node:
        if isinstance(item, dict):
            val = item.get(url_field)
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                out.append(val)
    return out


# --------------------------------------------------------------------------- #
# Registry (Redis-backed)
# --------------------------------------------------------------------------- #
class DataSourceRegistry:
    """CRUD + discovery for tenant-isolated data sources."""

    MAX_RUN_HISTORY = 50

    def __init__(self, redis_client) -> None:
        self.r = redis_client

    # -- key helpers --------------------------------------------------------- #
    def _hkey(self, tenant: str) -> str:
        return f"sources:{tenant}"

    def _runs_key(self, source_id: str) -> str:
        return f"sources:runs:{source_id}"

    # -- CRUD ---------------------------------------------------------------- #
    def register(
        self,
        tenant: str,
        name: str,
        kind: DataSourceKind | str,
        config: dict[str, Any],
        *,
        schedule_cron: str | None = None,
        source_id: str | None = None,
    ) -> DataSourceRecord:
        rec = DataSourceRecord(
            id=source_id or uuid.uuid4().hex[:12],
            tenant=tenant,
            name=name,
            kind=DataSourceKind(kind),
            config=config,
            schedule_cron=schedule_cron,
        )
        rec.validate()  # fail fast on bad config
        if self.r.hexists(self._hkey(tenant), rec.id):
            raise ValueError(f"source id already exists: {rec.id}")
        self.r.hset(self._hkey(tenant), rec.id, rec.to_json())
        logger.info("registered source %s (%s) for tenant %s", rec.id, rec.kind.value, tenant)
        return rec

    def update(self, tenant: str, source_id: str, **changes: Any) -> DataSourceRecord:
        rec = self.get(tenant, source_id)
        for k, v in changes.items():
            if k == "config":
                rec.config.update(v)
            elif hasattr(rec, k):
                setattr(rec, k, v)
        if isinstance(rec.kind, str):
            rec.kind = DataSourceKind(rec.kind)
        rec.validate()
        self.r.hset(self._hkey(tenant), rec.id, rec.to_json())
        return rec

    def get(self, tenant: str, source_id: str) -> DataSourceRecord:
        raw = self.r.hget(self._hkey(tenant), source_id)
        if raw is None:
            raise KeyError(f"unknown source {source_id} for tenant {tenant}")
        return DataSourceRecord.from_json(raw)

    def delete(self, tenant: str, source_id: str) -> bool:
        self.r.delete(self._runs_key(source_id))
        return bool(self.r.hdel(self._hkey(tenant), source_id))

    def list_sources(self, tenant: str, *, enabled_only: bool = False) -> list[DataSourceRecord]:
        out: list[DataSourceRecord] = []
        for raw in self.r.hvals(self._hkey(tenant)):
            rec = DataSourceRecord.from_json(raw)
            if enabled_only and not rec.enabled:
                continue
            out.append(rec)
        return sorted(out, key=lambda x: x.created_at)

    # -- run bookkeeping ------------------------------------------------------ #
    def record_run(self, source_id: str, status: str, detail: dict[str, Any]) -> None:
        entry = {"ts": time.time(), "status": status, **detail}
        pipe = self.r.pipeline()
        pipe.lpush(self._runs_key(source_id), json.dumps(entry))
        pipe.ltrim(self._runs_key(source_id), 0, self.MAX_RUN_HISTORY - 1)
        pipe.execute()

    def recent_runs(self, source_id: str, limit: int = 10) -> list[dict[str, Any]]:
        raw = self.r.lrange(self._runs_key(source_id), 0, limit - 1)
        return [json.loads(x) for x in raw]

    # -- expansion: turn a source into URLs ------------------------------------ #
    def expand_to_urls(
        self,
        rec: DataSourceRecord,
        fetcher=None,
        *,
        max_urls: int = 5_000,
    ) -> list[str]:
        """Discover fresh URLs for one source execution.

        ``fetcher(url)->bytes`` injectable for tests; defaults to requests.
        """
        fetcher = fetcher or _default_fetch
        urls: list[str] = []
        try:
            if rec.kind == DataSourceKind.RSS:
                urls = parse_rss_urls(fetcher(rec.config["url"]).decode("utf-8", "replace"))
            elif rec.kind == DataSourceKind.SITEMAP:
                stack = [rec.config["url"]]
                while stack and len(urls) < max_urls:
                    sm = stack.pop()
                    text = fetcher(sm).decode("utf-8", "replace")
                    leaves, children = parse_sitemap(text, sm)
                    urls.extend(leaves)
                    stack.extend(children)  # one-level recursion guard by max_urls
            elif rec.kind == DataSourceKind.API:
                urls = self._expand_api(rec, fetcher, max_urls)
            elif rec.kind == DataSourceKind.WEBSITE:
                urls = [rec.config["seed_url"]]
            elif rec.kind == DataSourceKind.COMMONCRAWL:
                from src.ingestion.commoncrawl import query_cc_index

                hits = query_cc_index(
                    rec.config["match_text"],
                    max_hits=rec.config.get("max_hits", 1000),
                    fetcher=lambda u: fetcher(u),
                )
                urls = [h.url for h in hits]
            elif rec.kind == DataSourceKind.S3:
                import asyncio

                from src.ingestion.downloader import S3Streamer

                streamer = S3Streamer(endpoint_url=rec.config.get("endpoint_url"))
                keys = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                    streamer.list_keys(
                        rec.config["bucket"], rec.config.get("prefix", ""), max_urls
                    )
                ) if not _loop_running() else []
                urls = [f"s3://{rec.config['bucket']}/{k}" for k in keys]
        except Exception as exc:  # noqa: BLE001 - registry must never crash scheduler
            logger.exception("expansion failed for %s: %s", rec.id, exc)
            self.record_run(rec.id, "error", {"message": str(exc)})
            return []
        urls = urls[:max_urls]
        self.record_run(rec.id, "ok", {"urls": len(urls)})
        return urls

    def _expand_api(self, rec: DataSourceRecord, fetcher, max_urls: int) -> list[str]:
        cfg = rec.config
        items_path = cfg["items_path"]
        url_field = cfg.get("url_field", "url")
        cursor_param = cfg.get("cursor_param")      # cursor-style pagination
        page_param = cfg.get("page_param")         # page-number-style pagination
        page_size = cfg.get("page_size", 100)
        urls: list[str] = []
        session = _make_session(cfg.get("headers"), cfg.get("api_key"), cfg.get("api_key_header", "Authorization"))
        cursor: Any = None
        page = 1
        for _ in range(cfg.get("max_pages", 50)):
            params = dict(cfg.get("params", {}))
            if cursor_param and cursor is not None:
                params[cursor_param] = cursor
            if page_param:
                params[page_param] = page
                params.setdefault(cfg.get("size_param", "limit"), page_size)
            resp = session.get(cfg["url"], params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            batch = extract_api_urls(payload, items_path, url_field)
            if not batch:
                break
            urls.extend(batch)
            if len(urls) >= max_urls:
                break
            if cursor_param:
                cursor = _dig(payload, cfg.get("cursor_path", "meta.next_cursor"))
                if not cursor:
                    break
            elif page_param:
                page += 1
            else:
                break
        return urls


def _dig(payload: Any, dotted: str) -> Any:
    node = payload
    for part in dotted.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _make_session(headers: dict | None, api_key: str | None, key_header: str):
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": "WebCrawlerPro-Registry/1.0"})
    if headers:
        s.headers.update(headers)
    if api_key:
        if key_header.lower() == "authorization":
            s.headers["Authorization"] = f"Bearer {api_key}"
        else:
            s.headers[key_header] = api_key
    return s


def _loop_running() -> bool:
    try:
        asyncio_get = __import__("asyncio").get_running_loop
        asyncio_get()
        return True
    except RuntimeError:
        return False


def _default_fetch(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=60, headers={"User-Agent": "WebCrawlerPro-Registry/1.0"})
    resp.raise_for_status()
    return resp.content
