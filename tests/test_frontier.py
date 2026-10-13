"""URL Frontier unit tests using fakeredis (Lua-enabled)."""

import time

import fakeredis
import pytest

import redis as real_redis


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    server = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(real_redis, "from_url", lambda url, **kw: server)
    yield server


def make_frontier(delay=1.0):
    from src.frontier.url_frontier import URLFrontier

    return URLFrontier("redis://fake/0", politeness_delay=delay)


def test_add_and_dedupe():
    f = make_frontier()
    assert f.add("https://Example.com/page/#frag") is True
    assert f.add("https://example.com/page/") is False   # normalized dup
    assert f.stats()["pending"] == 1


def test_rejects_non_http():
    f = make_frontier()
    assert f.add("javascript:void(0)") is False
    assert f.add("mailto:a@b.com") is False


def test_politeness_blocks_immediate_refetch():
    f = make_frontier(delay=60)
    f.add("https://a.com/x")
    assert len(f.pop(batch=5)) == 1
    assert f.pop(batch=5) == []            # domain locked for 60s


def test_ack_nack_flow():
    f = make_frontier(delay=0)
    f.add("https://a.com/x")
    popped = f.pop()
    assert f.stats()["in_flight"] == 1
    f.nack(popped[0].url)
    assert f.stats()["pending"] == 1 and f.stats()["in_flight"] == 0
    f.pop()
    f.ack("https://a.com/x")
    assert f.stats()["in_flight"] == 0


def test_stale_recovery():
    f = make_frontier(delay=0)
    f.add("https://c.com/z")
    got = f.pop()
    f.r.zadd(f._k("processing"), {got[0].url: time.time() - 1})
    assert f.recover_stale() == 1
    assert f.stats()["pending"] == 1


def test_normalize_static():
    from src.frontier.url_frontier import URLFrontier

    assert URLFrontier.normalize("HTTP://Ex.COM/p/?q=1#f") == "http://ex.com/p?q=1"
