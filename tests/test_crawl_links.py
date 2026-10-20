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
