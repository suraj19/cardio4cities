"""
OpenTelemetry wiring — traces and metrics for a run you cannot watch.

A city takes minutes to research and makes 150-250 model calls across ten
nodes and three datastores. When it comes back thin, the question is always
"which part gave up", and the honest answer before this module was "read the
warnings and guess". The pipeline already measured itself — per-node timings
in `workflow._timed`, per-model tokens in `llm.usage.ledger` — but those are
only visible after the run, only inside this process, and only if you thought
to look. Tracing turns the same measurements into something you can open.

DESIGN: OFF BY DEFAULT, AND FREE WHEN OFF.

Set OTEL_ENABLED=true to turn it on. When it is off, `span()` yields a shared
no-op singleton: no TracerProvider, no exporter, no background export thread,
and no span objects allocated across the few hundred call sites a city hits.
This module imports nothing from opentelemetry on that path either, though
that part saves less than it sounds — langsmith, which arrives with LangGraph,
has already imported the API package by the time anything here runs. The
saving is the provider and the export pipeline, not the import.

When it is on but the packages are missing, it logs once and carries on
disabled — telemetry is never a reason for a research run to fail.

WHAT IS INSTRUMENTED

  automatic   FastAPI request/response, outbound `requests` calls (page
              fetches, robots.txt, WHO and World Bank), SQLAlchemy queries
  manual      each of the ten graph nodes, every LLM call (GenAI semantic
              conventions, with token counts), every embedding batch, and
              the Milvus and Neo4j round trips

The embedding span earns its place: Milvus and Graphiti both embed through
the same local model, so when it cannot load, `/ask` reports two unrelated
store outages and the shared cause is invisible. As a span it is one failed
child under both parents.

EXPORT

Anything speaking OTLP: Jaeger, Grafana Tempo, Honeycomb, Datadog, an
OpenTelemetry Collector. Configured with the standard environment variables
(OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS) rather than
settings of our own, so anyone who has configured an OTel SDK before already
knows how to point this one. OTEL_CONSOLE_EXPORT=true prints spans to stdout
instead, which needs no collector and is enough to answer "where did the time
go" on a laptop.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from app.config import settings

logger = logging.getLogger(__name__)

# Resolved once by setup(). Left as None when telemetry is off or unavailable,
# which is what every call site branches on.
_tracer = None
_meter = None
_instruments: dict = {}
_configured = False


class _NoopSpan:
    """Stands in for a real span so call sites need no conditionals.

    Every method a call site uses is present and does nothing. This is the
    object returned on the disabled path, allocated once rather than per
    span, because the disabled path runs a few hundred times per city and
    should not produce garbage.
    """

    def set_attribute(self, key, value):
        return None

    def set_attributes(self, attributes):
        return None

    def add_event(self, name, attributes=None):
        return None

    def record_exception(self, exception):
        return None

    def set_status(self, *args, **kwargs):
        return None

    def is_recording(self) -> bool:
        return False


_NOOP = _NoopSpan()


def setup(app=None) -> bool:
    """Configure the SDK and instrument the frameworks. Returns whether it ran.

    Idempotent, because the app factory and the test suite can both reach it,
    and a second TracerProvider would silently drop the first one's spans.
    """
    global _tracer, _meter, _configured

    if _configured:
        return _tracer is not None
    _configured = True

    if not settings.OTEL_ENABLED:
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # Deliberately not fatal. A missing telemetry package is a reason to
        # run without telemetry, not a reason not to research a city.
        logger.warning(
            "OTEL_ENABLED is set but OpenTelemetry is not installed (%s). "
            "Continuing without telemetry. Install the optional block at the "
            "end of requirements.txt to enable it.",
            exc,
        )
        return False

    resource = Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            "service.version": settings.OTEL_SERVICE_VERSION,
            # The run mode is on the resource rather than on each span
            # because it cannot change within a process, and because a MOCK
            # trace next to a LIVE one in the same backend is otherwise very
            # confusing: the shapes are identical and the timings are not.
            "deployment.environment": settings.RUN_MODE.lower(),
        }
    )

    span_exporter, metric_exporter = _build_exporters()
    if span_exporter is None:
        return False

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("cardio4cities")

    if metric_exporter is not None:
        metrics.set_meter_provider(
            MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
            )
        )
        _meter = metrics.get_meter("cardio4cities")
        _build_instruments()

    _instrument_libraries(app)
    logger.info(
        "OpenTelemetry enabled: service=%s export=%s",
        settings.OTEL_SERVICE_NAME,
        "console" if settings.OTEL_CONSOLE_EXPORT else settings.OTEL_EXPORTER_ENDPOINT,
    )
    return True


def _build_exporters():
    """Console or OTLP. Console needs no collector, which is the difference
    between telemetry you can try in one command and telemetry you set up."""
    if settings.OTEL_CONSOLE_EXPORT:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter(), (
            ConsoleMetricExporter() if settings.OTEL_METRICS_ENABLED else None
        )

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as exc:
        logger.warning(
            "OTLP exporter not installed (%s) and OTEL_CONSOLE_EXPORT is off, "
            "so there is nowhere to send spans. Continuing without telemetry.",
            exc,
        )
        return None, None

    # Endpoint left to the SDK's own env vars when ours is blank, so the
    # standard OTEL_EXPORTER_OTLP_ENDPOINT keeps working untouched.
    kwargs = {}
    if settings.OTEL_EXPORTER_ENDPOINT:
        kwargs["endpoint"] = f"{settings.OTEL_EXPORTER_ENDPOINT.rstrip('/')}/v1/metrics"
    span_exporter = OTLPSpanExporter(**kwargs)

    return span_exporter, _build_metric_exporter(OTLPMetricExporter)


def _build_metric_exporter(OTLPMetricExporter):
    """Metrics, but not down a traces-only pipe.

    Some backends ingest traces and nothing else — Opik is the one this
    project documents. Deriving the metrics endpoint from the shared one, as
    the OTel convention says to, then sends a metrics POST every 60 seconds
    to a URL that will never accept it: a permanent background error that
    looks like the whole integration is broken when traces are in fact
    arriving fine.

    So a traces-only endpoint disables metrics and says so, and setting
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT explicitly overrides that — pointing
    metrics at a Prometheus-shaped collector while traces go to Opik is a
    perfectly reasonable thing to want.
    """
    if not settings.OTEL_METRICS_ENABLED:
        return None

    if settings.OTEL_METRICS_ENDPOINT:
        return OTLPMetricExporter(endpoint=settings.OTEL_METRICS_ENDPOINT)

    endpoint = settings.OTEL_EXPORTER_ENDPOINT
    if endpoint and _is_traces_only(endpoint):
        logger.info(
            "Metrics disabled: %s accepts traces only. Traces are unaffected. "
            "Set OTEL_EXPORTER_OTLP_METRICS_ENDPOINT to send metrics elsewhere.",
            endpoint,
        )
        return None

    kwargs = {}
    if endpoint:
        kwargs["endpoint"] = f"{endpoint.rstrip('/')}/v1/metrics"
    return OTLPMetricExporter(**kwargs)


#   Opik      <host>/api/v1/private/otel
#   Langfuse  <host>/api/public/otel
# Matched on path rather than hostname so each rule covers that vendor's
# cloud, every regional variant, and a self-hosted instance on localhost.
_TRACES_ONLY_PATHS = ("/api/v1/private/otel", "/api/public/otel")


def _is_traces_only(endpoint: str) -> bool:
    """Whether an endpoint is known to ingest traces and not metrics.

    Both LLM-native backends this project documents are trace-only. Neither
    rejects a metrics POST in a way you would notice quickly — you get a
    steady drip of export errors in the log while traces arrive perfectly,
    which is a confusing thing to debug and an easy thing to avoid.
    """
    lowered = endpoint.lower()
    return any(path in lowered for path in _TRACES_ONLY_PATHS)


def _build_instruments() -> None:
    """The handful of metrics worth having alongside the traces.

    Deliberately few. Everything here is already derivable from spans; these
    exist because the two questions they answer — "what is this costing" and
    "how long does a city take" — get asked as aggregates over time, which is
    the one thing traces are bad at.
    """
    _instruments["llm_tokens"] = _meter.create_counter(
        "gen_ai.client.token.usage",
        unit="token",
        description="Tokens consumed, split by model and input/output.",
    )
    _instruments["llm_calls"] = _meter.create_counter(
        "gen_ai.client.operation.count",
        unit="1",
        description="Chat completions attempted, including failures.",
    )
    _instruments["node_duration"] = _meter.create_histogram(
        "cardio4cities.node.duration",
        unit="s",
        description="Wall-clock time per pipeline node.",
    )
    _instruments["facts"] = _meter.create_counter(
        "cardio4cities.facts.adjudicated",
        unit="1",
        description="Fact-checked claims, split by confidence tier.",
    )


def _instrument_libraries(app) -> None:
    """Auto-instrumentation, each guarded separately.

    Separately on purpose: these are independent optional packages, and one
    of them being absent is not a reason to lose the other two.
    """
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(
                app,
                # The healthcheck runs every 30 seconds forever and tells you
                # nothing. Left in, it is the overwhelming majority of spans.
                excluded_urls="health",
            )
        except Exception as exc:
            logger.warning("FastAPI instrumentation unavailable: %s", exc)

    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        # Covers page fetches, robots.txt, and the WHO and World Bank APIs —
        # every outbound call the pipeline makes that is not the LLM.
        RequestsInstrumentor().instrument()
    except Exception as exc:
        logger.warning("requests instrumentation unavailable: %s", exc)

    try:
        from opentelemetry.instrumentation.threading import ThreadingInstrumentor

        # NOT optional here, despite looking like a nicety. Trace context
        # lives in a contextvar, which asyncio tasks inherit and threads do
        # not — and extraction, fact-checking, crawlability and search all
        # fan out across ThreadPoolExecutors. Without this, every LLM call
        # made inside a pool starts its own root trace: ~150 orphan traces
        # per city, none of them attached to the run that caused them. The
        # test suite asserts a run is exactly one trace, which is how this
        # was found rather than shipped.
        ThreadingInstrumentor().instrument()
    except Exception as exc:
        logger.warning(
            "threading instrumentation unavailable (%s) — spans created inside "
            "worker threads will not be linked to their run.", exc
        )

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
    except Exception as exc:
        logger.warning("SQLAlchemy instrumentation unavailable: %s", exc)


@contextmanager
def span(name: str, **attributes):
    """A span, or a no-op when telemetry is off.

    Call sites read the same either way:

        with span("extraction.fetch", url=url) as s:
            ...
            s.set_attribute("http.status_code", response.status_code)

    Exceptions are recorded and re-raised. Recording matters more than it
    looks in this codebase: almost every node catches its own failures and
    degrades rather than propagating, so an exception that never reaches a
    handler is exactly the kind that vanishes from the logs.
    """
    if _tracer is None:
        yield _NOOP
        return

    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            from opentelemetry.trace import Status, StatusCode

            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def content_attributes(**fields) -> dict:
    """Prompt/completion attributes, or nothing at all.

    Returns an empty dict unless OTEL_CAPTURE_CONTENT is on, so a call site
    can merge it unconditionally and the decision lives in one place rather
    than at every span that touches text.

    Keys are prefixed `gen_ai.prompt.` / `gen_ai.completion.`, which is what
    LLM-native backends look for when populating their input/output panels —
    Opik, Langfuse and LangSmith all read these.
    """
    if not settings.OTEL_CAPTURE_CONTENT:
        return {}

    limit = settings.OTEL_CAPTURE_CONTENT_CHARS
    attributes = {}
    for name, text in fields.items():
        if not text:
            continue
        prefix = "gen_ai.completion" if name == "completion" else "gen_ai.prompt"
        value = str(text)
        if len(value) > limit:
            # Marked rather than silently cut, so nobody reasons about a
            # prompt from a fragment without knowing it is one.
            value = f"{value[:limit]}… [truncated, {len(value)} chars total]"
        attributes[f"{prefix}.{name}"] = value
    return attributes


def record_llm_usage(
    model: str, prompt_tokens: int, completion_tokens: int, failed: bool = False
) -> None:
    """Mirror a ledger entry into metrics. No-op when metrics are off.

    Called from the same place as `ledger.record` rather than replacing it:
    the ledger is what a job's `llm_usage` field reports back to the caller
    and is needed whether or not anyone is collecting telemetry.
    """
    counter = _instruments.get("llm_tokens")
    if counter is None:
        return
    _instruments["llm_calls"].add(1, {"gen_ai.request.model": model, "error": failed})
    if prompt_tokens:
        counter.add(prompt_tokens, {"gen_ai.request.model": model, "gen_ai.token.type": "input"})
    if completion_tokens:
        counter.add(
            completion_tokens,
            {"gen_ai.request.model": model, "gen_ai.token.type": "output"},
        )


def record_node_duration(node: str, seconds: float) -> None:
    histogram = _instruments.get("node_duration")
    if histogram is not None:
        histogram.record(seconds, {"cardio4cities.node": node})


def record_fact(tier: str) -> None:
    counter = _instruments.get("facts")
    if counter is not None:
        counter.add(1, {"cardio4cities.tier": tier})
