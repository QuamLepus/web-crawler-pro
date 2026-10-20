from __future__ import annotations

import requests

from workers import crawl_tasks


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def test_robots_policy_blocks_disallowed_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: _Response(
            200, "User-agent: *\nDisallow: /private\n"
        ),
    )

    assert not crawl_tasks._robots_allows("https://example.com/private/data")
    assert crawl_tasks._robots_allows("https://example.com/public/data")


def test_robots_policy_allows_client_error_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response(404))

    assert crawl_tasks._robots_allows("https://example.com/page")


def test_robots_policy_blocks_server_errors(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response(503))

    assert not crawl_tasks._robots_allows("https://example.com/page")


def test_robots_policy_blocks_network_errors(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", fail)

    assert not crawl_tasks._robots_allows("https://example.com/page")


def test_run_crawl_fetches_robots_once_per_origin(monkeypatch) -> None:
    import fakeredis

    policy_requests: list[str] = []
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kwargs: (
            policy_requests.append(url)
            or _Response(200, "User-agent: *\nDisallow: /private\n")
        ),
    )
    monkeypatch.setattr(crawl_tasks, "_store_item", lambda item: item)
    fetched: list[str] = []

    def fetch(url: str):
        fetched.append(url)
        if url.endswith("/start"):
            return ("<a href='/private'>private</a>"
                    "<a href='/public'>public</a>"), 200
        return "<html><body>page</body></html>", 200

    result = crawl_tasks.run_crawl(
        {"url": "https://example.com/start", "max_pages": 2,
         "obey_robots": True},
        redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
        fetcher=fetch,
    )

    assert fetched == ["https://example.com/start", "https://example.com/public"]
    assert result["robots_blocked"] == 1
    assert policy_requests == ["https://example.com/robots.txt"]
