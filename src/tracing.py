"""OpenTelemetry instrumentation, exported to Langfuse Cloud.

WHY OTEL RATHER THAN THE `langfuse` SDK DIRECTLY
---------------------------------------------------
Langfuse Cloud accepts standard OTLP (OpenTelemetry Protocol) traces on its
own ingestion endpoint -- no proprietary SDK required. Using plain
OpenTelemetry means this instrumentation isn't locked to one observability
vendor: pointing it at a different OTLP-compatible backend later is a config
change (the endpoint and auth header), not a rewrite of every span in the
pipeline. That is the actual argument for "use OpenTelemetry," not just
"it's the standard name" -- the standard buys portability.

AUTH
-----
Langfuse's OTel endpoint authenticates with HTTP Basic Auth: the project's
public key as the username, secret key as the password, base64-encoded into
the Authorization header. Both keys come from a Langfuse Cloud project's
settings page -- see src/config.py's langfuse_public_key/langfuse_secret_key.

WHAT GETS TRACED
------------------
One span per pipeline stage, nested to mirror the actual call structure --
not a single flat span per request. The full chain, per stage:

    router -> intent_extraction -> compile (with a nested security_injection
    child span when an identity is present) -> guard -> execute
    -> cache_check / cache_write (Layer 4)
    -> retrieval -> synthesis (Layer 5, stacking path)

ONE HONEST GAP, DOCUMENTED RATHER THAN HIDDEN: the plan names
"bigquery_dry_run" as its own span, nested inside the guard stage. That dry
run happens inside src/cost_guard.py::check_cost(), called from inside
src/graph.py's _make_guard() closure -- both files this project's ground
rules forbid touching or wrapping internally (guardrails.py and cost_guard.py
are the untouched safety-check path; see graph_semantic.py's own docstring on
reusing "the same function object", not a copy). Splitting that inner call
into its own child span would require instrumenting inside a closure this
codebase deliberately never edits. The honest choice: one span named "guard"
covers guardrail checking AND the cost dry-run together, and this paragraph
says so rather than claiming finer granularity than the constraint allows.

FAILS OPEN, NOT CLOSED
------------------------
A tracing backend being unreachable, misconfigured, or simply not yet set up
must never break a real request -- observability is not allowed to be a new
single point of failure for a system whose whole thesis is about bounding
failure. init_tracing() with no keys configured is a documented no-op: spans
are created against a real OTel tracer either way (so instrumented code never
branches on "is tracing on"), but with no keys they simply have nowhere to
be exported and are dropped locally at negligible cost.
"""

from __future__ import annotations

import base64
import logging
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)

_SERVICE_NAME = "semantic-execution-gateway"
_initialized = False


def init_tracing(settings: Any | None = None) -> None:
    """Configure the global OTel TracerProvider. Idempotent -- safe to call
    more than once (e.g. once per test, once at app startup) without
    duplicating exporters.

    No-ops with a logged message (not a raised exception) when Langfuse
    keys aren't configured -- see module docstring, "FAILS OPEN, NOT CLOSED".
    """
    global _initialized
    if _initialized:
        return

    if settings is None:
        from src.config import get_settings

        settings = get_settings()

    provider = TracerProvider(
        resource=Resource.create({"service.name": _SERVICE_NAME})
    )

    public_key = getattr(settings, "langfuse_public_key", "") or ""
    secret_key = getattr(settings, "langfuse_secret_key", "") or ""
    host = getattr(settings, "langfuse_host", "") or "https://cloud.langfuse.com"

    if public_key and secret_key:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{host.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info("tracing: exporting spans to Langfuse at %s", host)
    else:
        log.info(
            "tracing: no Langfuse keys configured -- spans are created "
            "but not exported anywhere. Set LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY to enable."
        )

    trace.set_tracer_provider(provider)
    _initialized = True


def get_tracer():
    """The tracer every pipeline module imports and calls .start_as_current_
    span on. Always returns a real tracer -- init_tracing() need not have
    been called first; OTel's default no-op provider makes spans that are
    simply never exported, so instrumented code never has to check whether
    tracing is "on"."""
    return trace.get_tracer(_SERVICE_NAME)


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Convenience wrapper: start a span named `name`, set `attributes` on
    it, and record any exception raised inside the block onto the span
    before letting it propagate -- so a failed pipeline stage is visible in
    the trace, not just in application logs.

    Usage:
        with traced_span("compile", measure=intent.measure):
            out = compile_intent(intent, model, settings)
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 - re-raised immediately after recording
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
