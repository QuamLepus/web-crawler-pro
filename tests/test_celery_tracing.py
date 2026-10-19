"""Trace context propagation and lifecycle for Celery tasks."""
from types import SimpleNamespace

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from src.observability import celery_tracing

TRACE_ID = 0x123456789ABCDEF0123456789ABCDEF0
SPAN_ID = 0x123456789ABCDEF0


def test_api_scheduler_uses_the_configured_celery_app():
    import fakeredis
    from fastapi.testclient import TestClient

    from src.api.main import create_api_app
    from src.celery_app import celery_app

    app = create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                         enable_graphql=False, enable_celery=True)
    client = TestClient(app)
    test_app = create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                              enable_graphql=False)

    assert client.app.state.services.scheduler.app is celery_app
    assert test_app.state.services.scheduler.app is None


def test_scheduler_priority_queues_match_declared_celery_queues():
    from src.celery_app import app
    from src.compute.scheduler import JobPriority

    configured = {queue.name for queue in app.conf.task_queues}
    priorities = {
        JobPriority.CRITICAL: "crawl.high",
        JobPriority.HIGH: "crawl.high",
        JobPriority.NORMAL: "crawl.default",
        JobPriority.LOW: "crawl.bulk",
        JobPriority.BULK: "crawl.bulk",
    }
    assert {priority.queue for priority in JobPriority} == set(priorities.values())
    assert set(priorities.values()) <= configured

    import fakeredis

    from src.compute.scheduler import JobSpec, SchedulerService

    published = []
    spy = SimpleNamespace(send_task=lambda name, **options: published.append(
        (name, options["queue"])))
    scheduler = SchedulerService(fakeredis.FakeStrictRedis(decode_responses=True),
                                 celery_app=spy)
    for priority in JobPriority:
        scheduler.submit(JobSpec(
            kind="workflow", tenant="trace-test", priority=priority,
            payload={"estimated_pages": 1}))
    assert [queue for _name, queue in published] == [
        "crawl.high", "crawl.high", "crawl.default", "crawl.bulk", "crawl.bulk"]


def _parent_context():
    span_context = SpanContext(
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


def test_before_publish_injects_w3c_context_without_payload_fields():
    from celery.signals import before_task_publish

    import src.celery_app  # noqa: F401 - imports the app and registers signal hooks

    token = otel_context.attach(_parent_context())
    headers = {"task": "workers.example", "id": "task-id"}
    try:
        before_task_publish.send(
            sender="workers.example", headers=headers, routing_key="crawl.default")
    finally:
        otel_context.detach(token)

    extracted = TraceContextTextMapPropagator().extract(headers)
    parent = trace.get_current_span(extracted).get_span_context()
    assert parent.trace_id == TRACE_ID
    assert headers["traceparent"].startswith("00-")
    assert not {"args", "kwargs", "tenant", "url"}.intersection(headers)


def test_worker_signals_extract_parent_and_close_task_context(monkeypatch):
    from celery.signals import task_failure, task_postrun, task_prerun

    import src.celery_app  # noqa: F401 - imports the app and registers signal hooks

    monkeypatch.setattr(celery_tracing, "_ensure_worker_tracing", lambda: True)
    headers = {}
    TraceContextTextMapPropagator().inject(headers, context=_parent_context())
    task = type("FakeTask", (), {})()
    task.name = "workers.example"
    task.request = SimpleNamespace(headers=headers)
    task_id = "trace-test-task"

    task_prerun.send(sender=task, task_id=task_id, task=task)
    active = trace.get_current_span().get_span_context()
    assert active.is_valid
    assert active.trace_id == TRACE_ID
    assert task_id in celery_tracing._ACTIVE_SPANS

    task_failure.send(
        sender=task, task_id=task_id, exception=RuntimeError("sensitive detail"))
    task_postrun.send(sender=task, task_id=task_id, state="FAILURE")

    assert task_id not in celery_tracing._ACTIVE_SPANS
    assert not trace.get_current_span().get_span_context().is_valid
