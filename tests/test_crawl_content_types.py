from __future__ import annotations

import fakeredis

from workers import crawl_tasks


def test_run_crawl_preserves_content_type_and_only_discovers_html_links(
    monkeypatch,
) -> None:
    monkeypatch.setattr(crawl_tasks, "_store_item", lambda item: item)
    body = '{"html":"<a href=\\"/leak\\">not a link</a>"}'
    fetched: list[str] = []

    def fetch(url: str):
        fetched.append(url)
        if url.endswith("/start"):
            return body, 200, "application/json"
        return "<html><body>Unexpected page</body></html>", 200, "text/html"

    result = crawl_tasks.run_crawl(
        {"url": "https://example.com/start", "max_pages": 2,
         "obey_robots": False},
        redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
        fetcher=fetch,
    )

    assert fetched == ["https://example.com/start"]
    assert result["pages_done"] == 1
    item = result["collected"][0]
    assert item["content_type"] == "application/json"
    assert item["text"] == body
    assert "title" not in item
    assert "images" not in item


def test_extract_item_only_parses_html_media() -> None:
    html_item = crawl_tasks._extract_item(
        "https://example.com/", "<title>Home</title><p>Welcome</p>", 200,
        "text/html; charset=utf-8")
    binary_item = crawl_tasks._extract_item(
        "https://example.com/image", "\ufffd\ufffd", 200, "image/png")

    assert html_item["content_type"] == "text/html; charset=utf-8"
    assert html_item["title"] == "Home"
    assert "Welcome" in html_item["text"]
    assert "text" not in binary_item
    assert binary_item["content_type"] == "image/png"
