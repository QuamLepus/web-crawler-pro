"""Observability wiring for the public API: OpenTelemetry tracing, Prometheus
metrics exposition, request/latency instrumentation and Sentry error capture.

Design notes
------------
* **OpenTelemetry** is fully optional: if ``opentelemetry`` packages are not
  installed the app uses a no-op tracer shim. Without an OTLP endpoint it uses
  a silent exporter; missing exporter integrations never print spans to stdout.
* **Prometheus**: we prefer the first-party
  ``prometheus-fastapi-instrumentator`` when installed; otherwise we ship a
  small built-in middleware using ``prometheus_client`` directly (always
  available because :mod:`src.observability.metrics` already depends on it).
* **Sentry** init lives in :mod:`src.observability.metrics.init_sentry` and is
  reused here — DSN-gated, no-op without ``SENTRY_DSN``.

All endpoints are mounted under ``/metrics``, ``/healthz``, ``/readyz`` so
Kubernetes and Prometheus ServiceMonitors can scrape them without auth.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger("wcp.api.observability")

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "webcrawlerpro-api")

# --------------------------------------------------------------------------- #
# OpenTelemetry
# --------------------------------------------------------------------------- #
_OTEL_AVAILABLE = False


def setup_tracing(app: FastAPI) -> bool:
    """Configure API tracing and instrument FastAPI when OTel is available.

    Returns ``True`` when an SDK provider is active, ``False`` when tracing
    degrades gracefully to the API's no-op tracer shim.
    """
    global _OTEL_AVAILABLE
    try:
        from src.observability.tracing import configure_tracer_provider

        _OTEL_AVAILABLE = configure_tracer_provider(SERVICE_NAME)
        if not _OTEL_AVAILABLE:
            return False

        # Auto-instrument FastAPI if the integration package is present.
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(
                app, excluded_urls="healthz,readyz,metrics"
            )
        except Exception:  # noqa: BLE001 - optional instrumentation must not block API startup
            logger.debug("FastAPI OTel instrumentation not available")
        logger.info("OpenTelemetry tracing enabled (service=%s)", SERVICE_NAME)
        return True
    except Exception as exc:  # noqa: BLE001 - optional tracing must not block API startup
        logger.warning("OpenTelemetry unavailable (%s); using no-op tracing", exc)
        _OTEL_AVAILABLE = False
        return False


def get_tracer(name: str = "wcp.api"):
    """Return an OTel tracer or a shim with the same surface."""
    if _OTEL_AVAILABLE:
        from opentelemetry import trace

        return trace.get_tracer(name)
    return _NoOpTracer()


class _NoOpSpan:
    def set_attribute(self, *a, **k):
        return self

    def set_status(self, *a, **k):
        return self

    def record_exception(self, *a, **k):
        return self

    def end(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _NoOpTracer:
    def start_as_current_span(self, *a, **k):
        return _NoOpSpan()

    def start_span(self, *a, **k):
        return _NoOpSpan()


# --------------------------------------------------------------------------- #
# Prometheus metrics
# --------------------------------------------------------------------------- #
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except Exception:  # noqa: BLE001 - metrics are optional
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None

HTTP_REQUESTS_TOTAL = None
HTTP_REQUEST_LATENCY = None
if PROMETHEUS_AVAILABLE:
    HTTP_REQUESTS_TOTAL = Counter(
        "wcp_api_http_requests_total",
        "Total HTTP requests served by the public API",
        ["method", "path_template", "status"],
        registry=REGISTRY,
    )
    HTTP_REQUEST_LATENCY = Histogram(
        "wcp_api_http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["method", "path_template"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        registry=REGISTRY,
    )


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request count + latency histograms with low-cardinality labels.

    ``path_template`` uses the matched route (e.g. ``/api/v1/jobs/{job_id}``)
    so job IDs / tenant names never explode label cardinality.
    """

    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            dur = time.perf_counter() - t0
            route = request.scope.get("route")
            # Never use the concrete URL as a label: unmatched requests may
            # contain random identifiers and create unbounded series.
            template = getattr(route, "path", None) or "unmatched"
            if HTTP_REQUESTS_TOTAL is not None:
                HTTP_REQUESTS_TOTAL.labels(
                    method=request.method, path_template=template,
                    status=str(status_code),
                ).inc()
                HTTP_REQUEST_LATENCY.labels(
                    method=request.method, path_template=template
                ).observe(dur)


def add_metrics_endpoint(app: FastAPI, path: str = "/metrics") -> None:
    """Expose ``/metrics`` in Prometheus text format."""

    @app.get(path, include_in_schema=False)
    async def prometheus_metrics() -> Response:
        payload = generate_latest(REGISTRY) if PROMETHEUS_AVAILABLE else b""
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST
                        if PROMETHEUS_AVAILABLE else "text/plain")


def setup_observability(app: FastAPI, *,
                        exclude_paths: Iterable[str] = (
                            "/healthz", "/readyz", "/livez", "/metrics")) -> None:
    """One-call wiring: OTel tracing, metrics middleware + endpoint, Sentry."""
    setup_tracing(app)
    if PROMETHEUS_AVAILABLE:
        app.add_middleware(MetricsMiddleware)
    add_metrics_endpoint(app)

    # Sentry error reporting (no-op unless SENTRY_DSN is configured).
    try:
        from src.observability.metrics import init_sentry

        init_sentry()
    except Exception as exc:  # noqa: BLE001 - Sentry must not block API startup
        logger.debug("sentry init skipped: %s", exc)

    @app.middleware("http")
    async def otel_request_span(request: Request, call_next):
        """Lightweight span per request even without FastAPIInstrumentor."""
        if request.url.path in tuple(exclude_paths):
            return await call_next(request)
        tracer = get_tracer()
        with tracer.start_as_current_span("http.request") as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.target", request.url.path)
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            return response
