"""Celery worker tasks — the distributed crawl loop.

Run a worker node (scales horizontally, just start more of these):

    celery -A src.celery_app worker -Q crawl.high,crawl.default,crawl.bulk -c 8

Tasks:
    seed_frontier(urls)        -> push seeds into the Redis URL Frontier
    drain_frontier(batch)      -> pop ready URLs and fan out fetch_page tasks
    fetch_page(url, depth)     -> fetch one page, extract links, enqueue items,
                                  re-feed discovered URLs to the frontier
"""

from __future__ import annotations

import os
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.celery_app import app
from src.config import get_config
from src.frontier.url_frontier import URLFrontier
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
cfg = get_config()

FRONTIER_NS = os.getenv("FRONTIER_NAMESPACE", "frontier")


def get_frontier() -> URLFrontier:
    return URLFrontier(
        redis_url=cfg.REDIS_URL,
        namespace=FRONTIER_NS,
        politeness_delay=cfg.DELAY_BETWEEN_REQUESTS,
        max_depth=cfg.MAX_PAGES_PER_CRAWL and 10,
    )


@app.task(name="workers.seed_frontier")
def seed_frontier(urls: list[str]) -> int:
    """Push seed URLs into the frontier. Returns number newly enqueued."""
    return get_frontier().seed(urls)


@app.task(name="workers.drain_frontier", bind=True, max_retries=3)
def drain_frontier(self, batch: int = 20) -> int:
    """Pop a batch of polite-ready URLs and dispatch fetch_page per URL."""
    frontier = get_frontier()
    popped = frontier.pop(batch=batch)
    for cu in popped:
        fetch_page.delay(cu.url, depth=0)
    logger.info("drained %d urls from frontier", len(popped))
    return len(popped)


@app.task(name="workers.fetch_page", bind=True, max_retries=3,
          default_retry_delay=30)
def fetch_page(self, url: str, depth: int = 0) -> dict:
    """Fetch a single page, extract links + images, emit crawl item."""
    import requests

    frontier = get_frontier()
    headers = {"User-Agent": cfg.USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=cfg.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - retry any transient network error
        frontier.nack(url)
        raise self.retry(exc=exc)

    content_type = resp.headers.get("Content-Type", "")
    item = {
        "url": url,
        "depth": depth,
        "status_code": resp.status_code,
        "content_type": content_type,
        "fetched_at": __import__("datetime").datetime.utcnow().isoformat(),
    }

    links: list[str] = []
    if "html" in content_type:
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        item["title"] = title
        item["text"] = soup.get_text(separator=" ", strip=True)[:5000]
        item["images"] = [urljoin(url, img["src"])
                          for img in soup.find_all("img") if img.has_attr("src")]
        for a in soup.find_all("a", href=True):
            absolute = urljoin(url, a["href"])
            if urlparse(absolute).netloc == urlparse(url).netloc and depth + 1 < 10:
                if frontier.add(absolute, priority=1000.0 - (depth + 1) * 10,
                                depth=depth + 1, parent=url):
                    links.append(absolute)

    process_item.delay(item)
    frontier.ack(url)
    return {"url": url, "links_discovered": len(links), "status": resp.status_code}


@app.task(name="workers.process_item")
def process_item(item: dict) -> None:
    """Route a crawl item through the quality pipeline + storage backends."""
    from src.pipeline.cleaning import normalize_item
    from src.pipeline.dedupe import MinHashDeduper
    from src.pipeline.storage.json_exporter import JSONExporter

    cleaned = normalize_item(item)
    if MinHashDeduper.is_duplicate(cleaned):
        logger.debug("duplicate skipped: %s", cleaned.get("url"))
        return
    JSONExporter().save(cleaned)


# --------------------------------------------------------------------------- #
# Job-backed crawl — what the public API actually dispatches
# --------------------------------------------------------------------------- #
# ``SchedulerService._submit_celery`` maps job kinds onto these task names, and
# ``/v1/jobs/crawl`` publishes ``workers.crawl_tasks.crawl_domain``. Until now
# none of those tasks existed in this module, so Celery accepted the message,
# found no matching task and dropped it: every job submitted through the API
# sat in "queued" forever and nothing was ever crawled.
# --------------------------------------------------------------------------- #

JOB_KEY = "wcp:v1:job:{job_id}"
CRAWL_OUTPUT_DIR = os.getenv("CRAWL_OUTPUT_DIR", "output/crawl")


def _job_redis():
    """Client for the job records the API writes under ``wcp:v1:job:*``."""
    import redis as redis_lib

    return redis_lib.Redis.from_url(cfg.REDIS_URL, decode_responses=True)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(items) -> str:
    """Order-independent digest of a crawl result set.

    ``/v1/marketplace/datasets`` refuses to publish a listing unless the source
    job record carries ``data_sha256``. Nothing in the pipeline ever wrote that
    field, so the marketplace's core flow returned 422 for every request. The
    digest covers the canonical JSON of each item, sorted by URL, so crawling
    the same pages in a different order produces the same value.
    """
    import hashlib
    import json

    h = hashlib.sha256()
    for item in sorted(items, key=lambda d: str(d.get("url", ""))):
        blob = json.dumps(item, sort_keys=True, separators=(",", ":"),
                          default=str).encode("utf-8")
        h.update(blob)
        h.update(b"\x00")
    return h.hexdigest()


def _plain_fetch(url: str):
    """Fetch one URL; returns ``(html, status_code)``."""
    import requests

    resp = requests.get(url, headers={"User-Agent": cfg.USER_AGENT},
                        timeout=cfg.REQUEST_TIMEOUT)
    return resp.text, int(resp.status_code)


def _stealth_fetch(url: str):
    """Fetch through the stealth ladder; returns ``(html, status_code)``."""
    from src.middleware.stealth_middleware import StealthFetcher

    result = StealthFetcher().fetch(url)
    return result.html, int(result.status or 200)


def _watch_fetch(url: str):
    """Fetch a watch target with DNS pinning and a bounded response body."""
    from src.utils.public_http import fetch_public_html

    return fetch_public_html(url, timeout=cfg.REQUEST_TIMEOUT)


def _watch_robots_allows(url: str) -> bool:
    """Check robots.txt through the same pinned public-fetch path.

    For watches, an unreachable or unsafe robots URL fails closed so network
    errors cannot silently turn a monitoring request into an unrestricted one.
    """
    from urllib.parse import urlparse
    from urllib.robotparser import RobotFileParser

    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        text, status = _watch_fetch(robots_url)
        if status >= 500:
            return False
        if status >= 400:
            return True
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        return parser.can_fetch(cfg.USER_AGENT, url)
    except Exception as exc:  # noqa: BLE001 - fail closed on unsafe/unreachable robots
        logger.info("watch robots check blocked %s (%s)", url, exc)
        return False


def _robots_allows(url: str) -> bool:
    """Best-effort robots.txt check; fails open (allow) when unreachable."""
    from urllib.parse import urlparse
    from urllib.robotparser import RobotFileParser

    import requests

    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = requests.get(robots_url, headers={"User-Agent": cfg.USER_AGENT},
                            timeout=cfg.REQUEST_TIMEOUT)
        if resp.status_code < 400:
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser.can_fetch(cfg.USER_AGENT, url)
    except Exception as exc:  # noqa: BLE001 - unreadable robots must not block
        logger.debug("robots.txt unavailable for %s (%s); allowing", url, exc)
    return True


def _extract_item(url: str, html: str, status: int) -> dict:
    """Pull the fields the quality pipeline expects out of a raw response."""
    from bs4 import BeautifulSoup

    item = {
        "url": url,
        "status_code": status,
        "content_type": "text/html",
        "fetched_at": _utcnow_iso(),
    }
    soup = BeautifulSoup(html, "html.parser")
    item["title"] = (
        soup.title.string.strip() if soup.title and soup.title.string else "")
    item["text"] = soup.get_text(separator=" ", strip=True)[:5000]
    item["images"] = [urljoin(url, img["src"])
                      for img in soup.find_all("img") if img.has_attr("src")]
    return item


def _same_domain_links(url: str, html: str) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        absolute = urljoin(url, a["href"])
        if urlparse(absolute).netloc == urlparse(url).netloc:
            out.append(absolute)
    return out


def _store_item(item: dict) -> dict | None:
    """Normalize, dedupe and persist one item. Returns the cleaned item."""
    from src.pipeline.cleaning import normalize_item
    from src.pipeline.dedupe import MinHashDeduper
    from src.pipeline.storage.json_exporter import JSONExporter

    cleaned = normalize_item(item)
    if MinHashDeduper.is_duplicate(cleaned):
        return None
    JSONExporter(CRAWL_OUTPUT_DIR).save(cleaned)
    return cleaned


def _update_job(r, job_id: str, fields: dict) -> None:
    if not job_id or r is None:
        return
    try:
        r.hset(JOB_KEY.format(job_id=job_id),
               mapping={**{k: str(v) for k, v in fields.items()},
                        "updated_at": _utcnow_iso()})
    except Exception as exc:  # noqa: BLE001 - progress is best-effort
        logger.warning("could not update job %s: %s", job_id, exc)


def run_crawl(payload: dict, redis_client=None, fetcher=None) -> dict:
    """Crawl one domain for a tenant job and keep its record up to date.

    Separated from the Celery wrapper so it can be driven with a fake Redis
    and a fake fetcher in tests.
    """
    start_url = payload["url"]
    max_pages = max(1, int(payload.get("max_pages", 1) or 1))
    tenant = payload.get("tenant", "")
    job_id = payload.get("job_id", "")
    obey_robots = bool(payload.get("obey_robots", True))

    r = redis_client if redis_client is not None else _job_redis()
    is_watch = bool(payload.get("watch_id"))
    if fetcher is None:
        fetcher = (_watch_fetch if is_watch else
                   (_stealth_fetch if payload.get("stealth") else _plain_fetch))
    robots_check = _watch_robots_allows if is_watch else _robots_allows

    _update_job(r, job_id, {"state": "running"})

    queue = [start_url]
    seen = {start_url}
    collected: list[dict] = []
    pages_done = 0
    failures: list[str] = []

    while queue and pages_done < max_pages:
        url = queue.pop(0)
        if obey_robots and not robots_check(url):
            logger.debug("robots.txt disallows %s", url)
            continue
        try:
            html, status = fetcher(url)
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the job
            failures.append(f"{url}: {exc}")
            logger.warning("fetch failed for %s: %s", url, exc)
            continue

        pages_done += 1
        item = _extract_item(url, html, status)
        watch_id = payload.get("watch_id")
        if watch_id:
            try:
                from src.intelligence.watch import observe
                change = observe(r, tenant=tenant, watch_id=str(watch_id), item=item)
                if change:
                    webhook_change = {key: value for key, value in change.items()
                                      if key not in {"old_text_excerpt", "new_text_excerpt"}}
                    _emit(tenant, "web.changed", webhook_change, redis_client=r)
            except Exception as exc:  # monitoring must not fail the crawl
                logger.warning("watch observation failed (%s): %s", watch_id, exc)
        try:
            kept = _store_item(item)
        except Exception as exc:  # noqa: BLE001 - pipeline errors are non-fatal
            logger.warning("pipeline failed for %s: %s", url, exc)
            kept = None
        if kept is not None:
            collected.append(kept)

        for link in _same_domain_links(url, html):
            if link not in seen:
                seen.add(link)
                queue.append(link)

        _update_job(r, job_id, {"state": "running", "pages_done": pages_done,
                                "items_collected": len(collected)})

    digest = canonical_sha256(collected)
    result = {
        "job_id": job_id, "url": start_url, "pages_done": pages_done,
        "items": len(collected), "failures": failures[:10],
        "data_sha256": digest,
        # Callers that need the data itself (the topic-discovery CLI, dataset
        # exports) read it straight off the result; Celery serialises it with
        # the task result only when a result backend is configured.
        "collected": collected,
    }

    state = "failed" if (not collected and failures) else "succeeded"
    _update_job(r, job_id, {
        "state": state, "pages_done": pages_done,
        "items_collected": len(collected), "data_sha256": digest,
        "error": "; ".join(failures)[:500],
    })
    _emit(tenant, "crawl.failed" if state == "failed" else "crawl.completed",
          result, redis_client=r)
    return result


def _emit(tenant: str, event: str, data: dict, redis_client=None) -> None:
    """Best-effort webhook delivery; never let a bad endpoint fail a crawl."""
    if not tenant:
        return
    try:
        from src.gateway.webhooks.service import WebhookService

        r = redis_client if redis_client is not None else _job_redis()
        WebhookService(r).emit(tenant, event, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook emit failed (%s): %s", event, exc)


@app.task(name="workers.crawl_tasks.crawl_domain", bind=True, max_retries=3,
          default_retry_delay=30)
def crawl_domain(self, payload: dict) -> dict:
    """Crawl a domain for a job submitted through ``POST /v1/jobs/crawl``."""
    try:
        return run_crawl(payload)
    except Exception as exc:  # noqa: BLE001
        job_id = (payload or {}).get("job_id", "")
        try:
            _update_job(_job_redis(), job_id,
                        {"state": "failed", "error": str(exc)[:500]})
            _emit((payload or {}).get("tenant", ""), "crawl.failed",
                  {"job_id": job_id, "error": str(exc)[:500]})
        except Exception as inner:  # noqa: BLE001 - never mask the real error
            logger.warning("could not record failure for job %s: %s",
                           job_id, inner)
        raise self.retry(exc=exc)


@app.task(name="workers.crawl_tasks.run_source_refresh", bind=True,
          max_retries=3, default_retry_delay=60)
def run_source_refresh(self, payload: dict) -> dict:
    """Refresh a registered data source and seed the frontier with its URLs."""
    from src.ingestion.registry import DataSourceRegistry

    tenant = payload.get("tenant", "")
    source_id = payload.get("source_id") or payload.get("id", "")
    registry = DataSourceRegistry(_job_redis())
    record = registry.get(tenant, source_id) if source_id else None
    if record is None:
        raise ValueError(f"unknown data source {source_id!r} for tenant {tenant}")
    urls = registry.expand_to_urls(
        record, max_urls=int(payload.get("max_urls", 5_000)))
    seeded = get_frontier().seed(urls) if urls else 0
    registry.record_run(source_id, "ok", {"urls": len(urls), "seeded": seeded})
    return {"source_id": source_id, "urls": len(urls), "seeded": seeded}


@app.task(name="workers.crawl_tasks.tokenize_stage", bind=True, max_retries=3,
          default_retry_delay=60)
def tokenize_stage(self, payload: dict) -> dict:
    """Tokenize a crawl result into training shards."""
    from src.training.pipeline import TokenizeStage, load_jsonl
    from src.training.tokenizer import TokenizerEngine

    paths = list(payload.get("inputs") or [])
    if not paths:
        raise ValueError("tokenize_stage requires payload['inputs']")
    docs = []
    for path in paths:
        docs.extend(load_jsonl(path))
    stage = TokenizeStage(TokenizerEngine())
    stats = stage.run(docs, out_dir=payload.get("out_dir", "output/shards"),
                      prefix=payload.get("prefix", "shard"),
                      format=payload.get("format", "causal"))
    _update_job(_job_redis(), payload.get("job_id", ""),
                {"state": "succeeded",
                 "tokens_generated": int(stats.get("tokens", 0))})
    return {"job_id": payload.get("job_id", ""), **stats}


@app.task(name="workers.crawl_tasks.dedupe_stage", bind=True, max_retries=3,
          default_retry_delay=60)
def dedupe_stage(self, payload: dict) -> dict:
    """Near-duplicate pass over a crawl result using the MinHash LSH index."""
    from src.training.minhash_dedupe import make_deduper
    from src.training.pipeline import load_jsonl

    paths = list(payload.get("inputs") or [])
    if not paths:
        raise ValueError("dedupe_stage requires payload['inputs']")
    deduper = make_deduper(payload.get("backend", "auto"))
    seen: set[str] = set()
    kept, dropped = 0, 0
    for path in paths:
        for doc in load_jsonl(path):
            text = doc.get("text", "") or ""
            doc_id = doc.get("url") or doc.get("id") or str(kept + dropped)
            if deduper.is_duplicate(doc_id, text):
                dropped += 1
                continue
            seen.add(doc_id)
            kept += 1
    _update_job(_job_redis(), payload.get("job_id", ""),
                {"state": "succeeded", "items_collected": kept})
    return {"job_id": payload.get("job_id", ""), "kept": kept,
            "dropped": dropped}


@app.task(name="workers.crawl_tasks.watch_tick")
def watch_tick(limit: int = 50) -> int:
    """Dispatch due monitored URLs at most once per interval."""
    import hashlib
    import json
    import time
    from src.intelligence.watch import DUE, WATCH

    r = _job_redis()
    now = time.time()
    ids = r.zrangebyscore(DUE, "-inf", now, start=0, num=max(1, min(limit, 200)))
    queued = 0
    dispatched_hosts: set[str] = set()
    for raw_id in ids:
        watch_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        if not r.zrem(DUE, watch_id):
            continue
        raw = r.get(WATCH.format(watch_id=watch_id))
        if not raw:
            continue
        record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if not record.get("active"):
            continue
        tenant = record["tenant"]
        interval = max(15, int(record.get("interval_minutes", 60)))
        host = urlparse(record["url"]).netloc.lower()
        if host in dispatched_hosts:
            r.zadd(DUE, {watch_id: now + 60})
            continue
        dispatched_hosts.add(host)
        lock_key = f"wcp:watch:hostlock:{hashlib.sha256(host.encode()).hexdigest()}"
        if not r.set(lock_key, watch_id, nx=True, ex=60):
            r.zadd(DUE, {watch_id: now + 60})
            continue
        r.zadd(DUE, {watch_id: now + interval * 60})
        try:
            watch_crawl.delay(tenant, watch_id)
            queued += 1
        except Exception as exc:
            r.delete(lock_key)
            r.zadd(DUE, {watch_id: now + min(interval * 60, 300)})
            logger.warning("could not queue watch %s: %s", watch_id, exc)
    return queued


@app.task(name="workers.crawl_tasks.watch_crawl", bind=True, max_retries=2,
          default_retry_delay=60)
def watch_crawl(self, tenant: str, watch_id: str) -> dict:
    """Fetch one watched URL and persist its snapshot/change event."""
    import json
    from src.intelligence.watch import WATCH

    r = _job_redis()
    raw = r.get(WATCH.format(watch_id=watch_id))
    if not raw:
        return {"watch_id": watch_id, "skipped": "missing"}
    record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    if record.get("tenant") != tenant or not record.get("active"):
        return {"watch_id": watch_id, "skipped": "inactive_or_wrong_tenant"}
    return run_crawl({"url": record["url"], "max_pages": 1,
                      "obey_robots": True, "stealth": False,
                      "tenant": tenant, "job_id": "", "watch_id": watch_id},
                     redis_client=r)
