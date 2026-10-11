"""OpenTelemetry tracing using the GenAI semantic conventions.

One trace per question:

    invoke_agent dpa-ask                    (root: question, status, cost, prompt version)
    ├── chat <deployment>                   (one per model call: tokens, latency, finish reason)
    ├── execute_tool top_price_changes      (args, ok/error, row count)
    ├── chat <deployment>
    └── guardrail number_grounding          (numbers checked, ungrounded)

Exporters (any combination):
  - Azure Monitor / Application Insights when APPLICATIONINSIGHTS_CONNECTION_STRING is set.
    Spans land in the `dependencies` / `requests` tables; ops/kql/ has the queries.
  - A JSONL file (local runs and eval runs), so traces are inspectable without Azure.
  - In-memory, for tests.

Question and answer text are NOT recorded unless DPA_TRACE_CONTENT=true, because
traces are often retained and shared more widely than the app's own data.
"""
from __future__ import annotations

import json
import os
from typing import Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

SERVICE_NAME = "dpa-ask-agent"
_provider: TracerProvider | None = None


def capture_content() -> bool:
    return os.environ.get("DPA_TRACE_CONTENT", "").lower() in ("1", "true", "yes")


class JsonlSpanExporter(SpanExporter):
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with open(self.path, "a") as f:
            for s in spans:
                f.write(json.dumps(span_to_dict(s), default=str) + "\n")
        return SpanExportResult.SUCCESS


def span_to_dict(s: ReadableSpan) -> dict:
    return {
        "trace_id": format(s.context.trace_id, "032x"),
        "span_id": format(s.context.span_id, "016x"),
        "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
        "name": s.name,
        "start_ns": s.start_time,
        "duration_ms": round((s.end_time - s.start_time) / 1e6, 2) if s.end_time else None,
        "status": s.status.status_code.name,
        "attributes": dict(s.attributes or {}),
    }


def setup_tracing(jsonl_path: str | None = None, extra_exporter: SpanExporter | None = None,
                  service_version: str = "0.2.0") -> TracerProvider:
    """Configure the global tracer provider once per process."""
    global _provider
    if _provider is not None:
        return _provider
    provider = TracerProvider(resource=Resource.create({
        "service.name": SERVICE_NAME, "service.version": service_version}))
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

        provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=conn)))
    if jsonl_path:
        provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(jsonl_path)))
    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("dpa.agent")


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()
