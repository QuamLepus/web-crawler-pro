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
