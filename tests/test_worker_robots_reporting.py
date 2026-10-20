from __future__ import annotations

import fakeredis
import pytest


@pytest.fixture
def crawl_env(monkeypatch, tmp_path):
    from workers import crawl_tasks

    monkeypatch.setattr(crawl_tasks, "CRAWL_OUTPUT_DIR", str(tmp_path))
    return crawl_tasks, fakeredis.FakeStrictRedis(decode_responses=True)


def test_run_crawl_reports_robots_blocked_pages(crawl_env, monkeypatch) -> None:
    worker, redis = crawl_env
    monkeypatch.setattr(worker, "_robots_allows", lambda _url: False)

    def unexpected_fetch(_url: str):
        raise AssertionError("robots-blocked URL must not be fetched")

    result = worker.run_crawl(
        {"url": "https://example.com/private", "max_pages": 1,
         "tenant": "", "job_id": "job_robots", "obey_robots": True},
        redis_client=redis,
        fetcher=unexpected_fetch,
    )

    assert result["pages_done"] == 0
    assert result["robots_blocked"] == 1
    record = redis.hgetall("wcp:v1:job:job_robots")
    assert record["robots_blocked"] == "1"
