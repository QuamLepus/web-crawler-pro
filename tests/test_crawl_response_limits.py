from __future__ import annotations

from src.utils import public_http
from workers import crawl_tasks


def test_worker_fetch_uses_public_dns_pinned_transport(monkeypatch) -> None:
    calls = {}

    def fetch(url: str, **kwargs):
        calls.update(url=url, **kwargs)
        return "<title>Bounded</title>", 200, "text/html; charset=utf-8"

    monkeypatch.setattr(public_http, "fetch_public_response", fetch)

    result = crawl_tasks._request_text_bounded("https://example.com/page")

    assert result == ("<title>Bounded</title>", 200, "text/html; charset=utf-8")
    assert calls["url"] == "https://example.com/page"
    assert calls["timeout"] == crawl_tasks.cfg.REQUEST_TIMEOUT
    assert calls["user_agent"] == crawl_tasks.cfg.USER_AGENT
    assert calls["max_response_bytes"] == crawl_tasks.MAX_CRAWL_RESPONSE_BYTES
