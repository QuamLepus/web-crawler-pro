import time

import fakeredis

from src.intelligence.watch import DUE, create_watch
from workers import crawl_tasks


def test_watch_tick_dispatches_due_watches_once_and_throttles_same_host(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(crawl_tasks, "_job_redis", lambda: redis)
    dispatched = []
    monkeypatch.setattr(crawl_tasks.watch_crawl, "delay",
                        lambda tenant, watch_id: dispatched.append((tenant, watch_id)))
    first = create_watch(redis, tenant="tenant", watch_id="watch_aaaaaaaaaaaaaaaaaaaa",
                         url="https://example.org/a", interval_minutes=30)
    second = create_watch(redis, tenant="tenant", watch_id="watch_bbbbbbbbbbbbbbbbbbbb",
                          url="https://example.org/b", interval_minutes=30)
    redis.zadd(DUE, {first["watch_id"]: time.time() - 1,
                     second["watch_id"]: time.time() - 1})
    assert crawl_tasks.watch_tick() == 1
    assert len(dispatched) == 1
    assert redis.zcard(DUE) == 2  # next interval + same-origin politeness delay
    scores = redis.zrange(DUE, 0, -1, withscores=True)
    assert all(score > time.time() for _, score in scores)


def test_crawl_worker_persists_watch_snapshot_and_emits_change(monkeypatch):
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    watch = create_watch(redis, tenant="tenant", watch_id="watch_cccccccccccccccccccc",
                         url="https://example.org/pricing", interval_minutes=60)
    monkeypatch.setattr(crawl_tasks, "_robots_allows", lambda _url: True)
    monkeypatch.setattr(crawl_tasks, "_store_item", lambda item: item)
    events = []
    monkeypatch.setattr(crawl_tasks, "_emit", lambda tenant, event, data, **kw:
                        events.append((tenant, event, data)))
    page = ["<title>Phone</title><p>Price ৳100</p>", "<title>Phone</title><p>Price ৳90</p>"]
    fetch = lambda _url: (page.pop(0), 200)
    payload = {"url": watch["url"], "max_pages": 1, "obey_robots": True,
               "tenant": "tenant", "watch_id": watch["watch_id"]}
    crawl_tasks.run_crawl(payload, redis_client=redis, fetcher=fetch)
    crawl_tasks.run_crawl(payload, redis_client=redis, fetcher=fetch)
    change_events = [e for e in events if e[1] == "web.changed"]
    assert len(change_events) == 1
    assert change_events[0][2]["price_mentions_before"] == ["৳100"]
    assert change_events[0][2]["price_mentions_after"] == ["৳90"]
