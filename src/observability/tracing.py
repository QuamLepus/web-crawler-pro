"""Shared, optional OpenTelemetry tracer-provider configuration."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("wcp.observability.tracing")


class _SilentExporter:
    """Discard spans when no collector is configured, without logging payloads."""

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def configure_tracer_provider(service_name: str) -> bool:
    """Install one global provider, leaving externally configured providers intact."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
        from opentelemetry.trace import ProxyTracerProvider

        if not isinstance(trace.get_tracer_provider(), ProxyTracerProvider):
            return True

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        processor = None
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            except Exception as exc:  # noqa: BLE001 - exporter integration is optional
                logger.warning(
                    "OTLP exporter unavailable (%s); worker/API spans will be dropped",
                    exc,
                )
        if processor is None:
            processor = SimpleSpanProcessor(_SilentExporter())

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
            active_span_processor=processor,
        )
        trace.set_tracer_provider(provider)
        return True
    except Exception as exc:  # noqa: BLE001 - tracing must not block service startup
        logger.warning("OpenTelemetry unavailable (%s); using no-op tracing", exc)
        try:
            from opentelemetry import trace

            trace.set_tracer_provider(trace.NoOpTracerProvider())
        except Exception as fallback_exc:  # noqa: BLE001
            logger.debug("could not install no-op tracer provider: %s", fallback_exc)
        return False
