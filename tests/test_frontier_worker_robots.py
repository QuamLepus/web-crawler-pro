from __future__ import annotations

from workers import crawl_tasks


class _Frontier:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, url: str) -> None:
        self.acked.append(url)


def test_frontier_worker_does_not_fetch_robots_disallowed_url(monkeypatch) -> None:
    url = "https://example.com/private"
    frontier = _Frontier()
    monkeypatch.setattr(crawl_tasks, "get_frontier", lambda: frontier)
    monkeypatch.setattr(crawl_tasks, "_robots_allows", lambda _url: False)

    result = crawl_tasks.fetch_page.run(url)

    assert result == {
        "url": url,
        "links_discovered": 0,
        "status": "skipped_robots",
    }
    assert frontier.acked == [url]
