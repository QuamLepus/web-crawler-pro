# Observability and worker tracing

The API and Celery workers use OpenTelemetry's W3C Trace Context format to
continue a trace across the broker boundary. The API's current span is injected
into Celery message headers before publish; each worker task creates a consumer
span as a child of that context. Task retries appear as separate task attempts
under the same trace when Celery propagates the headers.

## Enable OTLP export

The default `requirements.txt` installs the API and SDK tracing interfaces but
keeps the OTLP/gRPC exporter optional. Install it when using an OTLP collector:

```bash
pip install -r requirements.txt -r requirements-otel.txt
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_SERVICE_NAME=webcrawlerpro-api
export OTEL_WORKER_SERVICE_NAME=webcrawlerpro-worker
```

The API defaults its service name to `webcrawlerpro-api`; workers default to
`webcrawlerpro-worker`. Configure the API with `OTEL_SERVICE_NAME` and override
the worker service name separately with `OTEL_WORKER_SERVICE_NAME`.
Without an endpoint, spans are created and dropped quietly. If an endpoint is
configured but the OTLP exporter is missing or fails to initialize, the app
logs a warning and drops spans rather than printing them to stdout. This avoids
leaking span data into unstructured logs.

Celery instrumentation is registered when `src.celery_app` loads. The worker
provider is initialized lazily inside each prefork child (or on its first task
for non-prefork pools), avoiding initialization of exporter threads before a
fork. Worker shutdown attempts a bounded flush.

Spans include task name, task ID, routing key, state, and exception type. They
deliberately omit task arguments, crawl URLs, tenant IDs, result data, and
exception messages. The integration propagates trace context only—not baggage—
across the broker. Existing API request spans and Prometheus metrics remain
available independently of OTLP export.

## Validation and limitations

`tests/test_celery_tracing.py` checks W3C context injection/extraction and task
span lifecycle without contacting a broker or collector. The broader worker
instrumentation is best validated in a staging deployment with the same Celery
pool and OTLP collector configuration used in production. This is not a
replacement for end-to-end trace sampling, collector availability, or exporter
backpressure testing.
