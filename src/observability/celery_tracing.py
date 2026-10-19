"""Propagate OpenTelemetry trace context across Celery task boundaries.

Only task names, IDs, queue names, states, and exception types are recorded.
Task arguments, URLs, tenant IDs, and exception messages are deliberately not
attached to spans.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("wcp.observability.celery")
_ACTIVE_SPANS: dict[str, tuple[object, object, object]] = {}
_ACTIVE_SPANS_LOCK = threading.Lock()
_SETUP_ATTEMPTED = False
_SETUP_RESULT = False


def _ensure_worker_tracing() -> bool:
    """Configure a worker provider lazily, after a prefork child has started."""
    global _SETUP_ATTEMPTED, _SETUP_RESULT
    if _SETUP_ATTEMPTED:
        return _SETUP_RESULT
    _SETUP_ATTEMPTED = True
    try:
        import os

        from src.observability.tracing import configure_tracer_provider

        service_name = os.getenv("OTEL_WORKER_SERVICE_NAME", "webcrawlerpro-worker")
        _SETUP_RESULT = configure_tracer_provider(service_name)
    except Exception as exc:  # noqa: BLE001 - optional tracing must not block workers
        logger.warning("worker tracing unavailable (%s)", exc)
        _SETUP_RESULT = False
    return _SETUP_RESULT


def _before_task_publish(sender=None, headers=None, routing_key=None, **_kwargs):
    """Inject W3C trace context into Celery headers before broker publish."""
    if not isinstance(headers, dict):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        task_name = str(sender or headers.get("task") or "unknown")
        task_id = headers.get("id")
        with trace.get_tracer("wcp.celery.producer").start_as_current_span(
            "celery.publish", kind=SpanKind.PRODUCER
        ) as span:
            span.set_attribute("messaging.system", "celery")
            span.set_attribute("messaging.operation", "publish")
            span.set_attribute("celery.task.name", task_name)
            if task_id:
                span.set_attribute("celery.task.id", str(task_id))
            if routing_key:
                span.set_attribute("messaging.destination.name", str(routing_key))
            TraceContextTextMapPropagator().inject(headers)
    except Exception as exc:  # noqa: BLE001 - tracing must never block tasks
        logger.debug("could not inject Celery trace context: %s", exc)


def _task_prerun(sender=None, task_id=None, task=None, **_kwargs):
    """Start a consumer span parented to the producer's W3C context."""
    if not _ensure_worker_tracing():
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        request = getattr(task, "request", None)
        headers = getattr(request, "headers", None) or {}
        parent = TraceContextTextMapPropagator().extract(headers)
        task_name = str(getattr(task, "name", None) or sender or "unknown")
        tracer = trace.get_tracer("wcp.celery.worker")
        span = tracer.start_span(
            f"celery.task {task_name}", context=parent, kind=SpanKind.CONSUMER
        )
        span.set_attribute("messaging.system", "celery")
        span.set_attribute("messaging.operation", "process")
        span.set_attribute("celery.task.name", task_name)
        if task_id:
            span.set_attribute("celery.task.id", str(task_id))
        token = otel_context.attach(trace.set_span_in_context(span))
        with _ACTIVE_SPANS_LOCK:
            _ACTIVE_SPANS[str(task_id)] = (span, token, otel_context)
    except Exception as exc:  # noqa: BLE001 - tracing must never block tasks
        logger.debug("could not start Celery task span: %s", exc)


def _task_failure(sender=None, task_id=None, exception=None, **_kwargs):
    """Annotate failed attempts without exporting exception messages/payloads."""
    with _ACTIVE_SPANS_LOCK:
        active = _ACTIVE_SPANS.get(str(task_id))
    if active is None:
        return
    span, _token, _otel_context = active
    try:
        from opentelemetry.trace import Status, StatusCode

        if exception is not None:
            span.set_attribute("exception.type", type(exception).__name__)
        span.set_status(Status(StatusCode.ERROR, "Celery task failed"))
    except Exception as exc:  # noqa: BLE001 - observability is best effort
        logger.debug("could not annotate failed Celery task span: %s", exc)


def _task_postrun(sender=None, task_id=None, state=None, **_kwargs):
    """Close the current task span and record its terminal Celery state."""
    with _ACTIVE_SPANS_LOCK:
        active = _ACTIVE_SPANS.pop(str(task_id), None)
    if active is None:
        return
    span, token, otel_context = active
    try:
        from opentelemetry.trace import Status, StatusCode

        state_text = str(state or "unknown").upper()
        span.set_attribute("celery.task.state", state_text)
        if state_text == "SUCCESS":
            span.set_status(Status(StatusCode.OK))
        elif state_text == "FAILURE":
            span.set_status(Status(StatusCode.ERROR, "Celery task failed"))
        elif state_text == "RETRY":
            span.set_attribute("celery.task.retry", True)
    except Exception as exc:  # noqa: BLE001 - tracing cleanup must not affect tasks
        logger.debug("could not annotate Celery task result: %s", exc)
    finally:
        try:
            otel_context.detach(token)
        except Exception as exc:  # noqa: BLE001 - context cleanup is best effort
            logger.debug("could not detach Celery task span context: %s", exc)
        try:
            span.end()
        except Exception as exc:  # noqa: BLE001 - tracing must not block task completion
            logger.debug("could not end Celery task span: %s", exc)



def _worker_process_init(**_kwargs):
    """Initialize the exporter in each forked worker, not the pre-fork parent."""
    _ensure_worker_tracing()


def _worker_process_shutdown(**_kwargs):
    """Flush buffered spans when a prefork worker child exits."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=1000)
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown tracing is best effort
        logger.debug("could not flush worker spans: %s", exc)


def register_celery_tracing() -> None:
    """Register idempotent Celery signal hooks for producer and worker spans."""
    from celery.signals import (
        before_task_publish,
        task_failure,
        task_postrun,
        task_prerun,
        worker_process_init,
        worker_process_shutdown,
    )

    before_task_publish.connect(
        _before_task_publish, weak=False, dispatch_uid="wcp.otel.before_task_publish"
    )
    task_prerun.connect(_task_prerun, weak=False, dispatch_uid="wcp.otel.task_prerun")
    task_failure.connect(_task_failure, weak=False, dispatch_uid="wcp.otel.task_failure")
    task_postrun.connect(_task_postrun, weak=False, dispatch_uid="wcp.otel.task_postrun")
    worker_process_init.connect(
        _worker_process_init, weak=False, dispatch_uid="wcp.otel.worker_process_init"
    )
    worker_process_shutdown.connect(
        _worker_process_shutdown,
        weak=False,
        dispatch_uid="wcp.otel.worker_process_shutdown",
    )
