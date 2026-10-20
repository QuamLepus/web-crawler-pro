"""Topic-driven website discovery.

These tests are the regression net for the feature "give the crawler a topic
and a number instead of a list of URLs". They are fully offline: providers are
driven by an injected fake fetcher (or ``WCP_OFFLINE``), Redis is fakeredis,
and crawls get a fake page fetcher — so they prove the *wiring* is real, not
that the public internet is up.
"""

from __future__ import annotations

import json

import fakeredis
import pytest
import redis as real_redis

from src.compute.scheduler import SchedulerService
from src.discovery.planner import plan_queries, template_queries, topic_keywords
from src.discovery.providers import (
    CommonCrawlProvider,
    DuckDuckGoHTMLProvider,
    LLMSuggestionProvider,
    SearchHit,
    SeedProvider,
    build_providers,
    collect_hits,
    offline,
)
from src.discovery.ranker import Candidate, Ranker, domain_of
from src.discovery.service import (
    DiscoveryService,
    DiscoveryState,
    preview_discovery,
    run_discovery,
    run_discovery_and_crawl,
)


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
# planner
# --------------------------------------------------------------------------- #
def test_template_queries_expand_a_topic():
    queries = template_queries("best budget laptops 2026", max_queries=5)
    assert queries[0] == "best budget laptops 2026"
    assert len(queries) == 5
    assert len(set(queries)) == len(queries), "queries must be unique"


def test_template_queries_reject_empty_topic():
    assert template_queries("   ") == []


def test_plan_queries_returns_keywords_without_stopwords():
    plan = plan_queries("the best budget laptops for you")
    assert plan.source == "template"
    assert plan.queries
    assert "the" not in plan.keywords
    assert "laptops" in plan.keywords
    # keywords come from the same scorer the autonomous agent trusts
    assert plan.keywords == topic_keywords("the best budget laptops for you")


def test_plan_queries_respects_max_queries():
    assert len(plan_queries("climate change", max_queries=3).queries) <= 3


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
DDG_HTML = """
<html><body>
<div class="result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frtings.com%2Flaptop%2Fbest%2Fbudget">
     Best Budget Laptops 2026 - RTINGS.com</a>
  <div class="result__snippet">We bought and tested 40 cheap laptops.</div>
</div>
<div class="result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fspam.example%2Fbuy%2Fnow">
     BUY CHEAP LAPTOPS NOW</a>
  <div class="result__snippet">lorem ipsum dolor sit amet</div>
</div>
</body></html>
"""


def test_duckduckgo_parsing_unwraps_redirects_and_snippets():
    hits = DuckDuckGoHTMLProvider()._parse(DDG_HTML, "budget laptops", 10)
    assert [h.url for h in hits] == [
        "https://rtings.com/laptop/best/budget",
        "https://spam.example/buy/now",
    ]
    assert hits[0].provider == "duckduckgo"
    assert hits[0].rank == 1
    assert "tested" in hits[0].snippet
    assert "RTINGS" in hits[0].title


def test_duckduckgo_falls_back_to_plain_links_when_layout_changes():
    html = ('<html><body><a href="https://a.example/x">A</a>'
            '<a href="https://b.example/y">B</a></body></html>')
    hits = DuckDuckGoHTMLProvider()._parse(html, "q", 10)
    assert {h.url for h in hits} == {"https://a.example/x", "https://b.example/y"}


def test_duckduckgo_returns_nothing_offline():
    assert offline() is True  # conftest sets WCP_OFFLINE
    assert DuckDuckGoHTMLProvider().search("anything") == []


def test_seed_provider_emits_seeds_and_expands_sitemaps(monkeypatch):
    # the sitemap probe is a real fetch; lift the test-wide offline guard
    from src.discovery import providers as providers_mod

    monkeypatch.setattr(providers_mod, "offline", lambda: False)
    sitemap = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://rtings.com/laptop</loc></url>
      <url><loc>https://rtings.com/headphone</loc></url>
    </urlset>"""
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        if url.endswith("sitemap.xml"):
            return sitemap.encode()
        raise AssertionError(f"unexpected fetch: {url}")

    hits = SeedProvider(["https://rtings.com/"], fetcher=fake_fetch).search("q")
    assert any(h.url == "https://rtings.com/" for h in hits)
    assert any(h.url == "https://rtings.com/laptop" for h in hits)
    assert all(h.provider == "seed" for h in hits)
    assert any("sitemap.xml" in c for c in calls)


def test_commoncrawl_provider_builds_a_url_regex():
    assert CommonCrawlProvider._url_regex("best budget laptops").startswith(".*(")
    assert "laptops" in CommonCrawlProvider._url_regex("best budget laptops")
    assert CommonCrawlProvider._url_regex("x y") == ""


def test_llm_suggestion_provider_parses_json_arrays():
    out = LLMSuggestionProvider._parse(
        '[{"url": "https://a.example", "title": "A", "why": "authoritative"}]',
        "topic", 5)
    assert out[0].url == "https://a.example"
    assert out[0].provider == "llm"


def test_llm_suggestion_provider_is_inert_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DISCOVERY_USE_LLM", "")
    assert LLMSuggestionProvider().enabled is False
    assert LLMSuggestionProvider().search("topic") == []


def test_build_providers_always_includes_a_free_backend():
    providers = build_providers()
    assert any(p.name == "duckduckgo" for p in providers)
    # no seed given -> no seed provider
    assert not any(p.name == "seed" for p in providers)

    with_seeds = build_providers(seeds=["https://a.example"])
    assert with_seeds[0].name == "seed", "seeds come first so they rank first"


def test_collect_hits_survives_a_broken_provider():
    class Boom:
        name = "boom"

        def search(self, query, limit=10):
            raise RuntimeError("provider down")

    ok_hits = collect_hits([Boom(), _FakeProvider()], ["q1", "q2"], limit=2)
    assert len(ok_hits) == 2  # 2 queries x 1 hit from the healthy provider


class _FakeProvider:
    name = "fake"

    def search(self, query, limit=10):
        return [SearchHit(url=f"https://{query}.example/page", title=query,
                          snippet="a page about the topic",
                          provider=self.name, rank=1, query=query)]


# --------------------------------------------------------------------------- #
# ranker
# --------------------------------------------------------------------------- #
def _hits():
    return [
        SearchHit(url="https://rtings.com/laptop/reviews/best/budget",
                  title="The Best Budget Laptops 2026",
                  snippet="We tested 40 budget laptops under $500.",
                  provider="duckduckgo", rank=1, query="best budget laptops 2026"),
        SearchHit(url="https://rtings.com/laptop/reviews/best/budget",
                  title="The Best Budget Laptops 2026",
                  snippet="We tested 40 budget laptops under $500.",
                  provider="brave", rank=2, query="budget laptops review"),
        SearchHit(url="https://wirecutter.example/best-budget-laptops",
                  title="The Best Cheap Laptops",
                  snippet="Our picks for budget laptops that are actually good.",
                  provider="duckduckgo", rank=3, query="best budget laptops 2026"),
        SearchHit(url="https://spam.example/buy-cheap-laptops-now",
                  title="lorem ipsum cheap laptops",
                  snippet="lorem ipsum dolor sit amet laptops laptops laptops",
                  provider="duckduckgo", rank=4, query="cheap laptops"),
    ]


def test_build_groups_duplicate_urls_and_records_providers():
    cands = Ranker("budget laptops").build(_hits())
    urls = {c.url for c in cands}
    assert len(cands) == len(urls) == 3
    rtings = next(c for c in cands if "rtings" in c.url)
    assert set(rtings.providers) == {"duckduckgo", "brave"}


def test_ranker_prefers_authoritative_pages_over_spam():
    ranker = Ranker("best budget laptops 2026")
    scored = ranker.score(ranker.build(_hits()))
    top = ranker.select(scored, count=2)
    domains = [c.domain for c in top]
    assert "spam.example" not in domains
    assert domains[0] == "rtings.com", "agreement across 2 providers should win"


def test_ranker_marks_seeds_and_explains_the_score():
    ranker = Ranker("budget laptops")
    cands = ranker.build(_hits(), seed_domains=["https://wirecutter.example"])
    scored = ranker.score(cands)
    seeded = next(c for c in scored if c.domain == "wirecutter.example")
    assert seeded.seeded is True
    assert any("seeded by user" in r for r in seeded.reasons)
    assert any(r.startswith("relevance ") for r in scored[0].reasons)


def test_ranker_select_returns_distinct_websites():
    ranker = Ranker("budget laptops")
    scored = ranker.score(ranker.build(_hits()))
    two = ranker.select(scored, count=2, per_domain=1)
    assert len({c.domain for c in two}) == 2
    # per_domain=2 allows two entry points from the same site
    assert ranker.select(scored, count=5, per_domain=5) != two


def test_ranker_honours_the_blocklist():
    ranker = Ranker("budget laptops", blocklist=["spam.example"])
    scored = ranker.score(ranker.build(_hits()))
    spam = next(c for c in scored if c.domain == "spam.example")
    assert spam.rejected is True
    assert any("blocklisted" in r for r in spam.reasons)
    assert "spam.example" not in [c.domain for c in ranker.select(scored, 5)]


def test_domain_of_normalises_hosts():
    assert domain_of("https://WWW.Example.com:443/a/b") == "example.com"
    assert domain_of("https://user@sub.example.com/x") == "sub.example.com"
    assert domain_of("not a url") == ""


def test_candidate_round_trips_through_dict():
    cand = Candidate(url="https://a.example/x", domain="a.example", score=0.5,
                     providers=["duckduckgo"], reasons=["why"],
                     seeded=True, selected=True)
    back = Candidate.from_dict(cand.to_dict())
    assert back == cand


# --------------------------------------------------------------------------- #
# service / end-to-end
# --------------------------------------------------------------------------- #
def _fake_page_fetcher(pages: dict):
    def fetch(url):
        if url not in pages:
            raise RuntimeError(f"not found: {url}")
        return pages[url], 200

    return fetch


PAGES = {
    "https://rtings.com/laptop/reviews/best/budget": (
        "<html><title>Best Budget Laptops</title><body>"
        "We tested forty budget laptops under five hundred dollars.</body></html>"),
    "https://wirecutter.example/best-budget-laptops": (
        "<html><title>Best Cheap Laptops</title><body>"
        "Our picks for cheap laptops that are actually good.</body></html>"),
}


def _search_provider(name: str = "duckduckgo"):
    class _P:
        def __init__(self) -> None:
            self.name = name

        def search(self, query, limit=10):
            return [
                SearchHit(url=u, title=t, snippet=s, provider=self.name,
                          rank=i, query=query)
                for i, (u, t, s) in enumerate([
                    ("https://rtings.com/laptop/reviews/best/budget",
                     "The Best Budget Laptops 2026",
                     "We tested 40 budget laptops under $500."),
                    ("https://wirecutter.example/best-budget-laptops",
                     "The Best Cheap Laptops",
                     "Our picks for budget laptops that are actually good."),
                    ("https://spam.example/buy-cheap-laptops-now",
                     "lorem ipsum cheap laptops",
                     "lorem ipsum dolor sit amet laptops laptops"),
                ], 1)]

    return _P()


@pytest.fixture(autouse=True)
def no_network_crawl(monkeypatch, tmp_path):
    """`run_crawl` would otherwise hit robots.txt and write into ./output."""
    import workers.crawl_tasks as wt

    monkeypatch.setattr(wt, "_robots_allows",
                        lambda url, cache=None: True)
    monkeypatch.setattr(wt, "CRAWL_OUTPUT_DIR", str(tmp_path / "crawl"))


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_discovery_ranks_and_persists_candidates(redis_client):
    record = run_discovery(
        {"topic": "best budget laptops 2026", "count": 2, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()])
    assert record["state"] == DiscoveryState.awaiting_approval.value
    svc = DiscoveryService(redis_client)
    cands = svc.candidates(record["job_id"])
    selected = [c for c in cands if c["selected"]]
    assert len(selected) == 2
    assert {c["domain"] for c in selected} == {
        "rtings.com", "wirecutter.example"}
    stored = svc.get(record["job_id"])
    assert json.loads(stored["queries"]), "search queries must be recorded"
    assert json.loads(stored["providers"]) == ["duckduckgo"]


def test_discovery_auto_mode_crawls_every_shortlisted_site(redis_client):
    record = run_discovery_and_crawl(
        {"topic": "best budget laptops 2026", "count": 2, "auto": True,
         "max_pages_per_site": 1, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()],
        crawl_fetcher=_fake_page_fetcher(PAGES))
    assert record["state"] == DiscoveryState.succeeded.value
    assert record["sites_total"] == 2
    assert record["sites_succeeded"] == 2
    assert record["pages"] == 2
    assert len(record["crawled_items"]) == 2
    by_domain = {s["domain"]: s for s in record["per_site"]}
    assert set(by_domain) == {"rtings.com", "wirecutter.example"}
    assert by_domain["rtings.com"]["items"] == 1
    assert by_domain["rtings.com"]["data_sha256"]


def test_discovery_is_idempotent_per_job_id(redis_client):
    payload = {"topic": "budget laptops", "count": 2, "job_id": "disc_fixed",
               "tenant": "t1"}
    first = run_discovery(payload, redis_client=redis_client,
                          providers=[_search_provider()])
    second = run_discovery(payload, redis_client=redis_client,
                           providers=[_search_provider()])
    assert first["job_id"] == second["job_id"] == "disc_fixed"
    svc = DiscoveryService(redis_client)
    # still one job, not two
    assert len(svc.candidates("disc_fixed")) == 3


def test_approve_fans_out_one_crawl_job_per_site(redis_client):
    from src.compute.scheduler import JobSpec

    submitted: list[JobSpec] = []

    class _Recorder:
        def submit(self, spec: JobSpec):
            submitted.append(spec)
            return spec.job_id

    record = run_discovery(
        {"topic": "budget laptops", "count": 2, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()])
    svc = DiscoveryService(redis_client, scheduler=_Recorder())
    selected, job_ids = svc.approve(record["job_id"])

    assert len(selected) == 2
    assert len(job_ids) == 2
    assert {s.kind for s in submitted} == {"crawl_job"}
    urls = {s.payload["url"] for s in submitted}
    assert urls == {c["url"] for c in selected}
    assert all(s.payload["discovery_id"] == record["job_id"] for s in submitted)
    assert svc.get(record["job_id"])["state"] == DiscoveryState.crawling.value


def test_drop_removes_a_site_and_promotes_an_alternate(redis_client):
    record = run_discovery(
        {"topic": "budget laptops", "count": 1, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()])
    svc = DiscoveryService(redis_client)
    job_id = record["job_id"]
    before = [c["domain"] for c in svc.candidates(job_id) if c["selected"]]
    assert len(before) == 1

    dropped = before[0]
    svc.drop(job_id, dropped)
    after = [c["domain"] for c in svc.candidates(job_id) if c["selected"]]
    assert dropped not in after, "the dropped site must leave the shortlist"
    assert len(after) == 1 and after[0] != dropped, \
        "the next best site is promoted into the freed slot"


def test_approve_can_be_restricted_to_explicit_domains(redis_client):
    record = run_discovery(
        {"topic": "budget laptops", "count": 2, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()])
    svc = DiscoveryService(redis_client)
    selected, job_ids = svc.approve(record["job_id"],
                                    domains=["wirecutter.example"])
    assert [c["domain"] for c in selected] == ["wirecutter.example"]
    assert len(job_ids) == 1


def test_results_aggregate_the_per_site_crawl_records(redis_client):
    record = run_discovery_and_crawl(
        {"topic": "budget laptops", "count": 2, "auto": True,
         "max_pages_per_site": 1, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()],
        crawl_fetcher=_fake_page_fetcher(PAGES))
    report = DiscoveryService(redis_client).results(record["job_id"])
    assert report["sites_total"] == 2
    assert report["sites_succeeded"] == 2
    assert report["pages"] == 2
    assert report["data_sha256"], "multi-site digest must be produced"


def test_preview_does_not_crawl_or_persist(redis_client):
    out = preview_discovery(topic="budget laptops", count=2,
                            providers=[_search_provider()])
    assert len(out["candidates"]) == 2
    assert out["queries"]
    assert out["alternates"], "rejected alternates are reported too"
    assert redis_client.keys("wcp:v1:discovery:*") == []


def test_cancel_marks_the_job(redis_client):
    record = run_discovery(
        {"topic": "budget laptops", "count": 2, "tenant": "t1"},
        redis_client=redis_client, providers=[_search_provider()])
    svc = DiscoveryService(redis_client)
    assert svc.cancel(record["job_id"])["state"] == DiscoveryState.cancelled.value


def test_list_is_tenant_scoped(redis_client):
    run_discovery({"topic": "a", "count": 1, "tenant": "t1"},
                  redis_client=redis_client, providers=[_search_provider()])
    run_discovery({"topic": "b", "count": 1, "tenant": "t2"},
                  redis_client=redis_client, providers=[_search_provider()])
    svc = DiscoveryService(redis_client)
    mine, total = svc.list("t1")
    assert total == 1
    assert all(r["tenant"] == "t1" for r in mine)


# --------------------------------------------------------------------------- #
# wiring with the rest of the platform
# --------------------------------------------------------------------------- #
def test_scheduler_maps_discovery_to_real_celery_tasks():
    """A stale task name is accepted by the broker and silently dropped."""
    from workers.discovery_tasks import app as celery_app

    celery_app.loader.import_default_modules()
    for kind, name in SchedulerService._TASK_MAP.items():
        assert name in celery_app.tasks, (
            f"kind {kind!r} maps to {name!r}, which is not a registered task")


def test_discovery_jobs_are_charged_against_quota(redis_client):
    from src.compute.scheduler import (
        JobPriority,
        JobSpec,
        QuotaExceeded,
        SchedulerService,
    )

    r = redis_client
    sched = SchedulerService(r)
    sched.set_quota("t1", "requests_month", 5)
    r.set("quota:t1:requests_month:limit", 5)
    with pytest.raises(QuotaExceeded):
        sched.submit(JobSpec(kind="discover", payload={"count": 50},
                             tenant="t1", priority=JobPriority.NORMAL))
    # a small discovery still fits
    r.set("quota:t1:requests_month", 0)
    sched.submit(JobSpec(kind="discover", payload={"count": 3}, tenant="t1"))


def test_cli_discover_preview_runs_offline(monkeypatch, capsys):
    from src.cli import main

    monkeypatch.setattr("src.discovery.service.build_providers",
                        lambda **kw: [_search_provider()])
    rc = main(["preview", "--topic", "best budget laptops 2026", "--count", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rtings.com" in out


def test_cli_discover_auto_writes_a_report(monkeypatch, tmp_path, capsys):
    from src.cli import main

    monkeypatch.setattr("src.discovery.service.build_providers",
                        lambda **kw: [_search_provider()])
    monkeypatch.setenv("WCP_OFFLINE", "1")
    calls = {"n": 0}

    def crawl_fetcher(url):
        calls["n"] += 1
        return PAGES[url], 200

    monkeypatch.setattr("workers.crawl_tasks._plain_fetch", crawl_fetcher)
    monkeypatch.setattr("workers.crawl_tasks._robots_allows",
                        lambda url, cache=None: True)

    rc = main(["discover", "--topic", "best budget laptops 2026",
               "--count", "2", "--auto", "--max-pages", "1",
               "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert calls["n"] == 2, "both shortlisted sites must be crawled"
    reports = list(tmp_path.glob("*/report.json"))
    assert reports, "a report.json must be written"
    payload = json.loads(reports[0].read_text())
    assert payload["sites_total"] == 2
    assert (reports[0].parent / "items.jsonl").exists()


def test_discovery_router_is_mounted():
    from src.api.main import create_api_app

    app = create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                         enable_graphql=False, enable_rate_limit=False)
    paths = app.openapi()["paths"]
    assert "/api/v1/discovery" in paths
    assert "/api/v1/discovery/preview" in paths
    assert "/api/v1/discovery/{job_id}/approve" in paths
    assert "/api/v1/discovery/{job_id}/results" in paths


def test_discovery_endpoints_require_auth():
    from fastapi.testclient import TestClient

    from src.api.main import create_api_app

    app = create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                         enable_graphql=False, enable_rate_limit=False)
    client = TestClient(app)
    resp = client.post("/api/v1/discovery", json={"topic": "x", "count": 1})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# REST surface: create -> review -> approve -> results
# --------------------------------------------------------------------------- #
def _api_client():
    """A TestClient on the real app.

    No explicit redis client: ``create_api_app`` then falls through to
    ``_default_redis()``, which the module-level ``fake_redis`` fixture has
    patched onto a shared fakeredis server — so the app, the scheduler and
    ``run_discovery`` all see the same store.
    """
    from fastapi.testclient import TestClient

    from src.api.main import create_api_app

    app = create_api_app(enable_graphql=False, enable_rate_limit=False)
    return TestClient(app)


def _auth_headers(client, email="disc@example.com", org="Discovery Labs"):
    tok = client.post("/auth/signup", json={
        "org_name": org, "email": email, "password": "s3cret-passw0rd!"}).json()
    assert tok.get("access_token"), tok
    key = client.post("/keys",
                      headers={"Authorization": f"Bearer {tok['access_token']}"},
                      json={"name": "ci", "scopes": ["read", "crawl"]}
                      ).json()["key"]
    return {"X-API-Key": key}


def test_discovery_api_full_journey(monkeypatch, fake_redis):
    """POST -> GET -> candidates -> approve -> results, end to end."""
    monkeypatch.setattr("src.discovery.service.build_providers",
                        lambda **kw: [_search_provider()])

    with _api_client() as client:
        headers = _auth_headers(client)

        created = client.post("/api/v1/discovery", headers=headers,
                              json={"topic": "best budget laptops 2026",
                                    "count": 2})
        assert created.status_code == 202, created.text
        job = created.json()
        job_id = job["job_id"]
        assert job["state"] == "queued"
        assert job["topic"] == "best budget laptops 2026"

        # In production a worker picks the job off the queue; here we drive the
        # same code path inline against the same fakeredis store.
        from src.discovery.service import run_discovery

        shared = fakeredis.FakeStrictRedis(server=fake_redis,
                                           decode_responses=True)
        run_discovery({"topic": "best budget laptops 2026", "count": 2,
                       "job_id": job_id, "tenant": job["tenant"]},
                      redis_client=shared, providers=[_search_provider()])

        got = client.get(f"/api/v1/discovery/{job_id}", headers=headers)
        assert got.status_code == 200
        assert got.json()["state"] == "awaiting_approval"

        cands = client.get(f"/api/v1/discovery/{job_id}/candidates",
                           headers=headers, params={"only_selected": True})
        assert cands.status_code == 200
        selected = cands.json()
        assert len(selected) == 2
        assert all(c["reasons"] for c in selected), "scores must be explained"

        approved = client.post(f"/api/v1/discovery/{job_id}/approve",
                               headers=headers, json=None)
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == "crawling"
        assert approved.json()["sites_total"] == 2

        results = client.get(f"/api/v1/discovery/{job_id}/results",
                             headers=headers)
        assert results.status_code == 200
        assert results.json()["sites_total"] == 2


def test_discovery_api_validates_the_body():
    with _api_client() as client:
        headers = _auth_headers(client)
        assert client.post("/api/v1/discovery", headers=headers,
                           json={"topic": "x"}).status_code == 422
        assert client.post("/api/v1/discovery", headers=headers,
                           json={"topic": "ok", "count": 0}).status_code == 422
        assert client.post("/api/v1/discovery", headers=headers,
                           json={"topic": "ok",
                                 "seeds": ["ftp://nope"]}).status_code == 422


def test_discovery_api_is_tenant_scoped(monkeypatch):
    """One tenant must not be able to read another tenant's discovery job."""
    monkeypatch.setattr("src.discovery.service.build_providers",
                        lambda **kw: [_search_provider()])

    with _api_client() as client:
        mine = _auth_headers(client, email="a@example.com", org="Org A")
        other = _auth_headers(client, email="b@example.com", org="Org B")

        created = client.post("/api/v1/discovery", headers=mine,
                              json={"topic": "budget laptops", "count": 1})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]

        assert client.get(f"/api/v1/discovery/{job_id}",
                          headers=other).status_code == 404
        assert client.get(f"/api/v1/discovery/{job_id}",
                          headers=mine).status_code == 200


def test_discovery_preview_endpoint_answers_offline(monkeypatch):
    monkeypatch.setattr("src.discovery.service.build_providers",
                        lambda **kw: [_search_provider()])

    with _api_client() as client:
        headers = _auth_headers(client)
        resp = client.post("/api/v1/discovery/preview", headers=headers,
                           json={"topic": "budget laptops", "count": 2})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_found"] == 3
        assert len(body["candidates"]) == 2
        assert body["queries"], "the query plan is part of the response"
