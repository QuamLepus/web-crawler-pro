from __future__ import annotations

import fakeredis

from workers import crawl_tasks


def test_run_crawl_honors_robots_crawl_delay(monkeypatch) -> None:
    import requests

    class Response:
        status_code = 200
        text = "User-agent: *\nCrawl-delay: 2\n"

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(crawl_tasks.cfg, "DELAY_BETWEEN_REQUESTS", 0.25)
    monkeypatch.setattr(crawl_tasks, "_store_item", lambda item: item)
    events: list[tuple[str, object]] = []

    def fetch(url: str):
        events.append(("fetch", url))
        if url.endswith("/start"):
            return "<a href='/next'>Next</a>", 200
        return "<html><body>Next page</body></html>", 200

    def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    crawl_tasks.run_crawl(
        {"url": "https://example.com/start", "max_pages": 2,
         "obey_robots": True},
        redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
        fetcher=fetch,
        sleeper=sleep,
    )

    assert events == [
        ("sleep", 2.0),
        ("fetch", "https://example.com/start"),
        ("sleep", 2.0),
        ("fetch", "https://example.com/next"),
    ]


def test_run_crawl_applies_configured_delay_between_page_fetches(monkeypatch) -> None:
    monkeypatch.setattr(crawl_tasks.cfg, "DELAY_BETWEEN_REQUESTS", 0.25)
    monkeypatch.setattr(crawl_tasks, "_robots_allows",
                        lambda _url, cache=None: True)
    monkeypatch.setattr(crawl_tasks, "_store_item", lambda item: item)
    events: list[tuple[str, object]] = []

    def fetch(url: str):
        events.append(("fetch", url))
        if url.endswith("/start"):
            return "<a href='/next'>Next</a>", 200
        return "<html><body>Next page</body></html>", 200

    def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))

    crawl_tasks.run_crawl(
        {"url": "https://example.com/start", "max_pages": 2,
         "obey_robots": True},
        redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
        fetcher=fetch,
        sleeper=sleep,
    )

    assert events == [
        ("fetch", "https://example.com/start"),
        ("sleep", 0.25),
        ("fetch", "https://example.com/next"),
    ]
