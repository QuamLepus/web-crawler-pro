"""Celery worker tasks — the distributed crawl loop.

Run a worker node (scales horizontally, just start more of these):

    celery -A src.celery_app worker -Q crawl.default,crawl.bulk -c 8

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
