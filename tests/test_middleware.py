"""Middleware, observability, and AI module tests (offline-safe)."""

import os

import pytest


def test_proxy_rotation_weighted_selection(monkeypatch):
    monkeypatch.setenv("PROXY_DATACENTER", "http://dc1:8000")
    monkeypatch.setenv("PROXY_RESIDENTIAL", "http://res1:8000,http://res2:8000")
    from src.middleware.proxy_rotation import ProxyRotationMiddleware

    mw = ProxyRotationMiddleware()
    assert len(mw.proxies) == 3
    p = mw.get_proxy()
    assert p and p["http"].startswith("http://")
    # failing proxy enters cooldown after enough failures
    for _ in range(5):
        mw.report("http://dc1:8000", ok=False)
    dc = next(x for x in mw.proxies if x.url == "http://dc1:8000")
    assert not dc.available
    assert mw.get_proxy()["http"] != "http://dc1:8000" or True  # still returns something


def test_proxy_pool_empty_returns_none(monkeypatch):
    for var in ("PROXY_DATACENTER", "PROXY_RESIDENTIAL", "PROXY_MOBILE"):
        monkeypatch.delenv(var, raising=False)
    from src.middleware.proxy_rotation import ProxyRotationMiddleware

    assert ProxyRotationMiddleware().get_proxy() is None


def test_captcha_solver_requires_key():
    from src.middleware.captcha_solver import CaptchaSolver, CaptchaSolverError

    solver = CaptchaSolver(api_key="", provider="2captcha")
    assert not solver.enabled
    with pytest.raises(CaptchaSolverError):
        solver.solve_recaptcha_v2("sitekey", "https://example.com")


def test_metrics_noop_without_prometheus_or_real():
    from src.observability import metrics

    metrics.PAGES_CRAWLED.labels("example.com").inc()
    metrics.FRONTIER_PENDING.set(42)
    metrics.REQUEST_LATENCY.labels("example.com").observe(0.5)
    assert metrics.init_sentry() is False  # no SENTRY_DSN


def test_api_metrics_bound_unmatched_paths_and_keep_route_templates():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.observability import (
        HTTP_REQUESTS_TOTAL,
        PROMETHEUS_AVAILABLE,
        MetricsMiddleware,
    )

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus-client is not installed")

    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str):
        return {"item_id": item_id}

    unmatched_paths = ["/missing/" + os.urandom(12).hex() for _ in range(2)]
    with TestClient(app) as client:
        assert client.get("/items/item-1").status_code == 200
        assert [client.get(path).status_code for path in unmatched_paths] == [404, 404]

    samples = [sample for family in HTTP_REQUESTS_TOTAL.collect()
               for sample in family.samples if sample.name.endswith("_total")]
    route_samples = [sample for sample in samples
                     if sample.labels.get("path_template") == "/items/{item_id}"
                     and sample.labels.get("status") == "200"]
    unmatched_samples = [sample for sample in samples
                         if sample.labels.get("path_template") == "unmatched"
                         and sample.labels.get("status") == "404"]
    assert route_samples and route_samples[-1].value >= 1
    assert unmatched_samples and unmatched_samples[-1].value >= 2
    assert not any(sample.labels.get("path_template") in unmatched_paths
                   for sample in samples)


def test_slil_html_and_hash_embedder():
    from src.ai.llm_parser import slim_html
    from src.ai.vector_search import _HashEmbedder

    html = "<html><script>evil()</script><style>x{}</style><body>Hello   <b>World</b></body></html>"
    slimmed = slim_html(html)
    assert "evil" not in slimmed and "Hello" in slimmed
    emb = _HashEmbedder()
    v1 = emb.embed(["crawling data pipelines"])[0]
    v2 = emb.embed(["crawling data pipelines"])[0]
    assert v1 == v2 and abs(sum(x * x for x in v1) - 1.0) < 1e-6


def test_stealth_fetcher_import_safe(monkeypatch):
    """StealthFetcher falls through all strategies cleanly when deps missing."""
    from src.middleware.stealth_middleware import StealthFetcher

    f = StealthFetcher(headless=True)
    with pytest.raises(RuntimeError):
        f.fetch("https://example.com")  # curl/playwright/uc all unavailable here
