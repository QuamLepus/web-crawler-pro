from __future__ import annotations

import pytest
import requests

from workers import crawl_tasks


class _Response:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None):
        self._chunks = chunks
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.status_code = 200
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def test_bounded_request_streams_body_and_closes_response(monkeypatch) -> None:
    response = _Response(
        [b"<title>Bounded</title>"],
        {"Content-Type": "text/html; charset=utf-8", "Content-Length": "22"},
    )
    calls = {}

    def get(url, **kwargs):
        calls.update(url=url, **kwargs)
        return response

    monkeypatch.setattr(requests, "get", get)

    text, status, content_type = crawl_tasks._request_text_bounded(
        "https://example.com/page")

    assert "Bounded" in text
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert calls["stream"] is True
    assert response.closed


def test_bounded_request_stops_at_body_limit_and_closes_response(monkeypatch) -> None:
    response = _Response([b"12345", b"67890"])
    monkeypatch.setattr(crawl_tasks, "MAX_CRAWL_RESPONSE_BYTES", 8)
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(crawl_tasks.CrawlResponseTooLarge):
        crawl_tasks._request_text_bounded("https://example.com/large")

    assert response.closed
