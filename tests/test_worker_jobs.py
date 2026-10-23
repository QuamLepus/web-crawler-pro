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


def test_marketplace_artifact_jsonl_is_deterministic_and_bounded():
    import hashlib
    from src.marketplace.artifacts import ArtifactError, canonical_jsonl

    records = [{"url": "https://b.example", "title": "B"},
               {"title": "A", "url": "https://a.example"},
               {"url": "https://a.example", "title": "A second page"}]
    payload, digest, count = canonical_jsonl(records)
    reversed_payload, reversed_digest, reversed_count = canonical_jsonl(list(reversed(records)))

    assert payload == reversed_payload
    assert digest == reversed_digest == hashlib.sha256(payload).hexdigest()
    assert count == reversed_count == 3
    assert payload.endswith(b"\n")
    with pytest.raises(ArtifactError, match="byte limit"):
        canonical_jsonl(records, max_bytes=len(payload) - 1)


def test_marketplace_artifact_store_uploads_and_reads_back_exact_bytes():
    import hashlib
    import io
    from src.marketplace.artifacts import (ArtifactError,
                                           MinioMarketplaceArtifactStore,
                                           canonical_jsonl)

    class FakeResponse(io.BytesIO):
        def release_conn(self):
            pass

    class FakeMinio:
        def __init__(self, corrupt=False):
            self.objects = {}
            self.corrupt = corrupt
            self.removed = []
            self.lifecycle = None

        def bucket_exists(self, bucket):
            return True

        def get_bucket_lifecycle(self, bucket):
            return self.lifecycle

        def set_bucket_lifecycle(self, bucket, config):
            self.lifecycle = config

        def put_object(self, bucket, key, stream, length, content_type):
            self.objects[(bucket, key)] = stream.read(length)

        def get_object(self, bucket, key):
            data = self.objects[(bucket, key)]
            return FakeResponse(data + b"corrupt" if self.corrupt else data)

        def remove_object(self, bucket, key):
            self.removed.append((bucket, key))
            self.objects.pop((bucket, key), None)

    records = [{"url": "https://example.com", "content": "sample"}]
    payload, digest, count = canonical_jsonl(records)
    fake = FakeMinio()
    store = MinioMarketplaceArtifactStore(client=fake, bucket="test-artifacts")

    result = store.put_verified("tenant-secret-id", "job_0123456789abcdef", payload, digest)

    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.size_bytes == len(payload)
    assert result.record_count == count == 1
    assert result.uri.startswith("s3://test-artifacts/marketplace/tenants/")
    assert "tenant-secret-id" not in result.object_key
    assert fake.objects[("test-artifacts", result.object_key)] == payload
    assert fake.lifecycle.rules[0].expiration.days == 30
    assert fake.lifecycle.rules[0].rule_filter.prefix == "marketplace/tenants/"

    corrupt_store = MinioMarketplaceArtifactStore(
        client=FakeMinio(corrupt=True), bucket="test-artifacts")
    with pytest.raises(ArtifactError, match="digest verification"):
        corrupt_store.put_verified("tenant-secret-id", "job_0123456789abcdef",
                                   payload, digest)
    assert corrupt_store.client.removed


def test_worker_persists_marketplace_artifact_only_with_explicit_rights(
        crawl_env, monkeypatch):
    from src.marketplace.artifacts import ArtifactMetadata

    wt, redis = crawl_env
    monkeypatch.setenv("MARKETPLACE_ARTIFACT_STORAGE_ENABLED", "true")
    job_id = "job_artifact"
    redis.hset(f"wcp:v1:job:{job_id}", mapping={"tenant": "tenant-private"})
    records = [{"url": "https://example.com", "content": "crawl data"}]

    class FakeStore:
        called = False

        def put_verified(self, tenant, received_job_id, payload, digest):
            import hashlib
            self.called = True
            assert tenant == "tenant-private"
            assert received_job_id == job_id
            assert hashlib.sha256(payload).hexdigest() == digest
            return ArtifactMetadata(
                uri="s3://bucket/marketplace/tenants/hash/jobs/job_artifact/dataset.jsonl",
                object_key="marketplace/tenants/hash/jobs/job_artifact/dataset.jsonl",
                sha256=digest, size_bytes=len(payload), record_count=1,
                expires_at="2026-11-22T00:00:00+00:00")

    store = FakeStore()
    status = wt._persist_marketplace_artifact(
        redis, job_id, "tenant-private", records, rights_confirmed=True, store=store)

    assert status == "available"
    assert store.called is True
    assert redis.hget(f"wcp:v1:job:{job_id}", "artifact_status") == "available"
    assert redis.hget(f"wcp:v1:job:{job_id}", "artifact_sha256")
    assert redis.hget(f"wcp:v1:job:{job_id}", "artifact_uri").startswith("s3://bucket/")

    foreign_job_id = "job_foreign_artifact"
    redis.hset(f"wcp:v1:job:{foreign_job_id}", mapping={"tenant": "other-tenant"})
    store.called = False
    rejected = wt._persist_marketplace_artifact(
        redis, foreign_job_id, "tenant-private", records,
        rights_confirmed=True, store=store)
    assert rejected == "rejected"
    assert store.called is False


def test_crawl_request_requires_rights_confirmation_for_artifact_persistence():
    from pydantic import ValidationError
    from src.api.schemas import CrawlJobCreate

    with pytest.raises(ValidationError, match="marketplace_data_rights_confirmed"):
        CrawlJobCreate(url="https://example.com", persist_marketplace_artifact=True)

    request = CrawlJobCreate(
        url="https://example.com", persist_marketplace_artifact=True,
        marketplace_data_rights_confirmed=True)
    assert request.persist_marketplace_artifact is True


def test_run_crawl_never_persists_full_artifact_without_explicit_opt_in(
        crawl_env, monkeypatch):
    wt, redis = crawl_env
    monkeypatch.setenv("MARKETPLACE_ARTIFACT_STORAGE_ENABLED", "true")
    monkeypatch.setattr(wt, "_store_item", lambda item: item)
    persisted = []
    monkeypatch.setattr(
        wt, "_persist_marketplace_artifact",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        wt, "_update_job",
        lambda client, job_id, fields: client.hset(f"wcp:v1:job:{job_id}", mapping={
            key: str(value) for key, value in fields.items()
        }),
    )
    job_id = "job_artifact_optout"
    redis.hset(f"wcp:v1:job:{job_id}", mapping={"tenant": "tenant-private",
                                                "state": "queued"})

    result = wt.run_crawl(
        {"url": "https://example.com", "max_pages": 1,
         "tenant": "tenant-private", "job_id": job_id, "obey_robots": False},
        redis_client=redis, fetcher=lambda _url: ("<html><body>Content</body></html>", 200))

    assert result["state"] == "completed"
    assert persisted == []
    assert redis.hget(f"wcp:v1:job:{job_id}", "artifact_status") == "not_requested"
