"""Worker/job regressions: the API must actually be able to crawl.

`SchedulerService._submit_celery` maps job kinds onto Celery task names. Those
tasks used to be referenced but never defined, so Celery accepted the message
and dropped it — every `POST /v1/jobs/crawl` stayed "queued" forever. These
tests pin the wiring and the crawl loop that backs it.
"""

from __future__ import annotations

import fakeredis
import pytest
import redis as real_redis


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        real_redis, "from_url",
        lambda url, **kw: fakeredis.FakeStrictRedis(
            server=server, decode_responses=kw.get("decode_responses", True)))
    monkeypatch.setattr(
        real_redis.Redis, "from_url",
        classmethod(lambda cls, url, **kw: real_redis.from_url(url, **kw)))
    yield server


# --------------------------------------------------------------------------- #
# task wiring
# --------------------------------------------------------------------------- #
def test_every_scheduler_task_is_registered():
    """No job kind may point at a task that does not exist."""
    from src.compute.scheduler import SchedulerService
    from workers.crawl_tasks import app as celery_app

    for kind in ("crawl_job", "ingest_source", "tokenize", "dedupe"):
        task_name = SchedulerService._TASK_MAP[kind]
        assert task_name in celery_app.tasks, (
            f"kind {kind!r} maps to {task_name!r}, which is not a registered "
            f"task — jobs of this kind would be silently dropped")


def test_config_exposes_redis_settings():
    """`Config` used to be defined twice; the survivor had no REDIS_URL."""
    from src.config import Config, get_config

    cfg = get_config()
    assert isinstance(cfg, Config), "get_config() must return an instance"
    assert cfg.REDIS_URL
    assert cfg.MAX_PAGES_PER_CRAWL > 0


# --------------------------------------------------------------------------- #
# the crawl loop
# --------------------------------------------------------------------------- #
PAGES = {
    "https://example.com/": (
        "<html><title>Home</title><body>Welcome to the home page."
        "<a href='/about'>About</a><a href='https://other.com/x'>Ext</a>"
        "</body></html>"),
    "https://example.com/about": (
        "<html><title>About</title><body>About us, we crawl things.</body></html>"),
}


def _fake_fetch(url: str):
    if url not in PAGES:
        raise RuntimeError(f"not found: {url}")
    return PAGES[url], 200


@pytest.fixture()
def crawl_env(monkeypatch, tmp_path):
    """Point crawl output at tmp_path; return the fakeredis job client."""
    import workers.crawl_tasks as wt

    monkeypatch.setattr(wt, "CRAWL_OUTPUT_DIR", str(tmp_path))
    return wt, fakeredis.FakeStrictRedis(decode_responses=True)


def test_run_crawl_updates_job_record_and_digest(crawl_env):
    wt, r = crawl_env
    job_id = "job_crawltest"
    r.hset(f"wcp:v1:job:{job_id}", mapping={"tenant": "t1", "state": "queued"})

    result = wt.run_crawl(
        {"url": "https://example.com/", "max_pages": 5, "tenant": "t1",
         "job_id": job_id, "obey_robots": False},
        redis_client=r, fetcher=_fake_fetch)

    assert result["pages_done"] == 2
    assert result["items"] == 2
    assert result["failures"] == []

    rec = r.hgetall(f"wcp:v1:job:{job_id}")
    from src.api.schemas import JobState
    assert JobState(rec["state"]) is JobState.completed
    assert rec["pages_done"] == "2"
    assert rec["items_collected"] == "2"
    # Completed crawl metadata carries the digest required for publication.
    assert rec["data_sha256"] == result["data_sha256"]
    assert result["preview_available"] is True
    from src.marketplace.preview import JOB_PREVIEW_KEY, PREVIEW_RETENTION_SECONDS
    preview_key = JOB_PREVIEW_KEY.format(job_id=job_id,
                                         digest=result["data_sha256"])
    preview_raw = r.get(preview_key)
    assert preview_raw
    import json
    assert len(json.loads(preview_raw)) == 2
    assert 0 < r.ttl(preview_key) <= PREVIEW_RETENTION_SECONDS


def test_run_crawl_stays_on_the_start_domain(crawl_env):
    wt, r = crawl_env

    result = wt.run_crawl(
        {"url": "https://example.com/", "max_pages": 1, "tenant": "t1",
         "job_id": "job_scope", "obey_robots": False},
        redis_client=r, fetcher=_fake_fetch)

    # max_pages=1 stops after the seed, so the off-domain link never fires.
    assert result["pages_done"] == 1


def test_run_crawl_marks_failure_when_every_page_errors(crawl_env):
    wt, r = crawl_env

    def always_down(url):
        raise RuntimeError("connection refused")

    result = wt.run_crawl(
        {"url": "https://example.com/", "max_pages": 3, "tenant": "t1",
         "job_id": "job_down", "obey_robots": False},
        redis_client=r, fetcher=always_down)

    assert result["pages_done"] == 0
    assert result["failures"]
    rec = r.hgetall("wcp:v1:job:job_down")
    assert rec["state"] == "failed"
    assert rec["error"]


def test_canonical_sha256_ignores_item_order():
    from workers.crawl_tasks import canonical_sha256

    a = [{"url": "https://a/", "text": "one"}, {"url": "https://b/", "text": "two"}]
    b = list(reversed(a))
    assert canonical_sha256(a) == canonical_sha256(b)
    assert canonical_sha256(a) != canonical_sha256(a[:1])


def test_run_crawl_stops_after_current_page_when_cancelled(crawl_env, monkeypatch):
    wt, redis = crawl_env
    job_id = "job_cancel"
    job_key = f"wcp:v1:job:{job_id}"
    redis.hset(job_key, mapping={"state": "running"})
    monkeypatch.setattr(wt, "_store_item", lambda item: item)
    fetched: list[str] = []
    events: list[str] = []
    monkeypatch.setattr(
        wt, "_emit",
        lambda tenant, event, data, redis_client=None: events.append(event),
    )

    def fetch(url: str):
        fetched.append(url)
        redis.hset(job_key, "state", "cancelled")
        return "<a href='/next'>Next</a>", 200

    result = wt.run_crawl(
        {"url": "https://example.com/start", "max_pages": 3,
         "tenant": "tenant-a", "job_id": job_id, "obey_robots": False},
        redis_client=redis, fetcher=fetch)

    assert fetched == ["https://example.com/start"]
    assert result["state"] == "cancelled"
    assert result["pages_done"] == 1
    assert redis.hget(job_key, "state") == "cancelled"
    assert events == ["crawl.cancelled"]

    wt._update_job(redis, job_id, {"state": "completed"})
    assert redis.hget(job_key, "state") == "cancelled"


def test_run_crawl_does_not_start_a_pre_cancelled_job(crawl_env):
    wt, redis = crawl_env
    job_id = "job_already_cancelled"
    job_key = f"wcp:v1:job:{job_id}"
    redis.hset(job_key, mapping={"state": "cancelled"})

    def unexpected_fetch(_url: str):
        raise AssertionError("pre-cancelled job must not fetch")

    result = wt.run_crawl(
        {"url": "https://example.com/start", "max_pages": 1,
         "tenant": "", "job_id": job_id, "obey_robots": False},
        redis_client=redis, fetcher=unexpected_fetch)

    assert result["state"] == "cancelled"
    assert result["pages_done"] == 0
    assert redis.hget(job_key, "state") == "cancelled"


def test_run_crawl_does_not_store_http_error_pages(crawl_env, monkeypatch):
    wt, redis = crawl_env
    monkeypatch.setattr(
        wt, "_store_item",
        lambda _item: pytest.fail("HTTP error page must not be stored"),
    )

    result = wt.run_crawl(
        {"url": "https://example.com/missing", "max_pages": 1,
         "tenant": "", "job_id": "job_http_error", "obey_robots": False},
        redis_client=redis,
        fetcher=lambda _url: ("<html><title>Not found</title></html>", 404),
    )

    assert result["pages_done"] == 1
    assert result["items"] == 0
    assert result["failures"] == ["https://example.com/missing: HTTP 404"]
    assert redis.hget("wcp:v1:job:job_http_error", "state") == "failed"
