from __future__ import annotations

from workers.crawl_tasks import _same_domain_links


def test_same_domain_links_strip_fragments_and_deduplicate() -> None:
    html = """
    <a href="/guide#intro">Intro</a>
    <a href="/guide#examples">Examples</a>
    <a href="#top">Top</a>
    <a href="/guide">Guide</a>
    <a href="https://other.example/page#section">External</a>
    """

    assert _same_domain_links("https://example.com/start", html) == [
        "https://example.com/guide",
        "https://example.com/start",
    ]


def test_job_crawl_strips_fragment_from_seed(monkeypatch) -> None:
    import fakeredis
    from workers import crawl_tasks

    requested: list[str] = []
    monkeypatch.setattr(crawl_tasks, "_store_item", lambda item: item)

    def fetch(url: str):
        requested.append(url)
        return "<html><body>Page text</body></html>", 200

    result = crawl_tasks.run_crawl(
        {"url": "https://example.com/start#section", "max_pages": 1,
         "obey_robots": False},
        redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
        fetcher=fetch,
    )

    assert requested == ["https://example.com/start"]
    assert result["url"] == "https://example.com/start"
