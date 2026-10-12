"""Prometheus metrics + Sentry error tracking.

`metrics.py` exposes a module-level REGISTRY with named counters/gauges/histograms
used across crawlers and workers. `start_metrics_server()` serves /metrics
for Prometheus scraping; `init_sentry()` wires error tracking from env.
"""

from __future__ import annotations

import os

# ---- optional integrations, safe no-op fallbacks -------------------------- #
try:
    from prometheus_client import (
        CollectorRegistry, Counter, Gauge, Histogram, start_http_server,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PROMETHEUS_AVAILABLE = False

    class _NoOpMetric:
        def __init__(self, *a, **k): pass
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): pass
        def dec(self, *a, **k): pass
        def set(self, *a, **k): pass
        def observe(self, *a, **k): pass

    Counter = Gauge = Histogram = _NoOpMetric

    def start_http_server(port): pass


def _m(metric_cls, *args, **kwargs):
    """Construct metric only when prometheus is installed."""
    if not PROMETHEUS_AVAILABLE:
        return metric_cls()  # _NoOpMetric accepts nothing meaningful
    return metric_cls(*args, **kwargs)


# ---- core metrics ---------------------------------------------------------- #
PAGES_CRAWLED = _m(
    Counter, "crawler_pages_crawled_total", "Total pages fetched", ["domain"])
REQUEST_ERRORS = _m(
    Counter, "crawler_request_errors_total", "Failed requests", ["domain", "error_type"])
ITEMS_STORED = _m(
    Counter, "crawler_items_stored_total", "Items persisted", ["backend"])
DUPLICATES_SKIPPED = _m(
    Counter, "crawler_duplicates_skipped_total", "Near-duplicates dropped")
FRONTIER_PENDING = _m(
    Gauge, "crawler_frontier_pending", "URLs waiting in the frontier")
FRONTIER_IN_FLIGHT = _m(
    Gauge, "crawler_frontier_in_flight", "URLs currently being processed")
REQUEST_LATENCY = _m(
    Histogram, "crawler_request_duration_seconds", "HTTP request latency", ["domain"])
TASK_QUEUE_DEPTH = _m(
    Gauge, "crawler_task_queue_depth", "Celery queue length", ["queue"])


def start_metrics_server(port: int | None = None) -> None:
    port = port or int(os.getenv("METRICS_PORT", "8000"))
    start_http_server(port)


def init_sentry() -> bool:
    """Initialize Sentry SDK from SENTRY_DSN. Returns True if enabled."""
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("DEPLOY_ENV", "development"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            integrations=[CeleryIntegration()],
        )
        return True
    except ImportError:
        return False


def update_frontier_gauges(frontier) -> None:
    """Snapshot frontier stats into Prometheus gauges."""
    stats = frontier.stats()
    FRONTIER_PENDING.set(stats.get("pending", 0))
    FRONTIER_IN_FLIGHT.set(stats.get("in_flight", 0))
