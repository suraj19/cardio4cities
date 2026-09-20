"""
OpenTelemetry instrumentation.

Runs the FULL pipeline in MOCK mode with an in-memory span exporter attached
and asserts on the trace it produces. Offline like the rest of the suite: no
collector, no keys, no internet.

These are contract tests for the trace, not for OpenTelemetry. What they pin
is the part that silently rots — a node renamed, a span that stops nesting
under the run, an exception that is swallowed before it reaches the span.
Telemetry that is wrong is worse than telemetry that is absent, because it is
believed.
"""
import asyncio
import os

import pytest

# Assigned rather than setdefault, for the reason in tests/test_jobs.py.
os.environ["RUN_MODE"] = "MOCK"
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_cardio4cities.db")

from app import telemetry  # noqa: E402
from app.jobs import JobRegistry  # noqa: E402


@pytest.fixture
def spans():
    """Capture spans in memory for one test, then put telemetry back.

    The tracer is set directly rather than through `telemetry.setup()`
    because setup is deliberately once-per-process — a second TracerProvider
    would silently drop the first one's spans — and a fixture that could not
    run twice would be a fixture that worked in isolation and failed in a
    suite.
    """
    from opentelemetry.instrumentation.threading import ThreadingInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Same instrumentation the app installs, and load-bearing for these
    # tests: four stages fan out across thread pools, and trace context is a
    # contextvar that threads do not inherit.
    ThreadingInstrumentor().instrument()

    previous = telemetry._tracer
    telemetry._tracer = provider.get_tracer("test")
    try:
        yield exporter
    finally:
        telemetry._tracer = previous
        ThreadingInstrumentor().uninstrument()


def _run_a_city(city="Springfield"):
    registry = JobRegistry()

    async def go():
        job, _ = await registry.submit(city, "Testland")
        for _ in range(600):
            if job.state in ("succeeded", "failed", "cancelled"):
                return job
            await asyncio.sleep(0.05)
        raise AssertionError(f"job never finished; stuck at {job.state}")

    return asyncio.run(go())


def test_a_run_produces_one_trace_with_every_node_under_it(spans):
    """The whole point of the root span. A run is a background job that
    outlives the request that started it, so without an explicit root the
    nodes would arrive as ten unrelated traces and 'show me that run' would
    not be a question anyone could ask."""
    job = _run_a_city()
    assert job.state == "succeeded", job.error

    finished = spans.get_finished_spans()
    by_name = {s.name: s for s in finished}

    root = next(s for s in finished if s.parent is None)
    assert root.name == "research Springfield"
    assert root.attributes["cardio4cities.city"] == "Springfield"

    from app.graph.workflow import NODES

    for node in NODES:
        assert f"node.{node}" in by_name, f"no span for node {node}"

    # One trace, not ten. This is the assertion that catches a node span
    # created outside the run's context.
    trace_ids = {s.context.trace_id for s in finished}
    assert len(trace_ids) == 1, f"run fragmented into {len(trace_ids)} traces"


def test_node_spans_carry_what_the_log_line_carries(spans):
    """A trace and a log of the same run must not be able to disagree, so the
    span records the same summary string the log prints."""
    _run_a_city("Shelbyville")

    planner = next(
        s for s in spans.get_finished_spans() if s.name == "node.planner"
    )
    assert planner.attributes["cardio4cities.node"] == "planner"
    assert planner.attributes["cardio4cities.city"] == "Shelbyville"
    assert "dimensions" in planner.attributes["cardio4cities.node.summary"]


def test_llm_spans_use_the_genai_conventions_and_never_carry_prompts(spans):
    """Named attributes so a backend recognises these as model calls, and
    token counts so cost is readable off the trace.

    The second half matters more: prompts here contain scraped page content,
    and a trace backend is not a place to put that. This asserts it stays
    out."""
    _run_a_city("Ogdenville")

    chats = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    assert chats, "no LLM spans were produced"

    for chat in chats:
        assert chat.attributes["gen_ai.operation.name"] == "chat"
        assert chat.attributes["gen_ai.request.model"]
        assert chat.attributes["cardio4cities.llm.profile"] in ("bulk", "judgement")
        for key, value in chat.attributes.items():
            assert "prompt" not in key or key.endswith("_chars"), (
                f"{key} looks like it could carry prompt text"
            )
            if isinstance(value, str):
                assert len(value) < 200, f"{key} is long enough to be content"

    # Both profiles are exercised by a full run, and the split is the whole
    # cost argument — if everything lands on one profile it is misconfigured.
    profiles = {c.attributes["cardio4cities.llm.profile"] for c in chats}
    assert profiles == {"bulk", "judgement"}, f"only saw {profiles}"


def test_content_capture_is_off_unless_asked_for(monkeypatch):
    """The careful default, asserted rather than assumed.

    Prompts here contain scraped third-party page content, so the cost of
    this flag defaulting the wrong way is a copy of that content in a trace
    backend nobody audits."""
    from app.config import settings

    assert settings.OTEL_CAPTURE_CONTENT is False
    assert telemetry.content_attributes(system="s", user="u") == {}


def test_content_capture_populates_the_panels_an_llm_dashboard_reads(
    spans, monkeypatch
):
    """With the flag on, prompts and completions land under the keys Opik,
    Langfuse and LangSmith read for their input/output panels. Without it
    those tools draw a correct trace with every detail panel empty, which
    reads as a broken integration rather than a deliberate one."""
    monkeypatch.setattr(telemetry.settings, "OTEL_CAPTURE_CONTENT", True)

    _run_a_city("Capital City")

    chats = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    assert chats
    for chat in chats:
        assert chat.attributes["gen_ai.prompt.system"]
        assert chat.attributes["gen_ai.prompt.user"]
        assert "gen_ai.completion.completion" in chat.attributes


def test_captured_content_is_truncated_and_says_so(monkeypatch):
    """Oversized attributes get dropped or silently cut by backends, and a
    prompt reasoned about from an unmarked fragment is worse than one that is
    obviously incomplete."""
    monkeypatch.setattr(telemetry.settings, "OTEL_CAPTURE_CONTENT", True)
    monkeypatch.setattr(telemetry.settings, "OTEL_CAPTURE_CONTENT_CHARS", 50)

    attributes = telemetry.content_attributes(user="x" * 500)
    value = attributes["gen_ai.prompt.user"]

    assert "truncated" in value
    assert "500 chars total" in value
    assert len(value) < 200


def test_a_traces_only_endpoint_does_not_get_a_metrics_exporter(monkeypatch):
    """Opik ingests traces and not metrics, so deriving the metrics endpoint
    from the shared one the way the OTel convention says to would POST every
    60 seconds to a URL that can never accept it — a permanent background
    error that reads as the whole integration being broken while traces are
    in fact arriving."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )

    monkeypatch.setattr(telemetry.settings, "OTEL_METRICS_ENABLED", True)
    monkeypatch.setattr(telemetry.settings, "OTEL_METRICS_ENDPOINT", "")

    for traces_only in (
        # Opik: cloud, and self-hosted on 5173.
        "https://www.comet.com/opik/api/v1/private/otel",
        "http://localhost:5173/api/v1/private/otel",
        # Langfuse: EU, US, and self-hosted on 3000.
        "https://cloud.langfuse.com/api/public/otel",
        "https://us.cloud.langfuse.com/api/public/otel",
        "http://localhost:3000/api/public/otel",
    ):
        monkeypatch.setattr(
            telemetry.settings, "OTEL_EXPORTER_ENDPOINT", traces_only
        )
        assert (
            telemetry._build_metric_exporter(OTLPMetricExporter) is None
        ), traces_only

    # A normal collector still gets metrics, at the conventional path.
    monkeypatch.setattr(
        telemetry.settings, "OTEL_EXPORTER_ENDPOINT", "http://localhost:4318"
    )
    exporter = telemetry._build_metric_exporter(OTLPMetricExporter)
    assert exporter is not None
    assert exporter._endpoint == "http://localhost:4318/v1/metrics"

    # And an explicit metrics endpoint overrides the traces-only rule, so
    # metrics to Prometheus while traces go to Opik stays possible.
    monkeypatch.setattr(
        telemetry.settings,
        "OTEL_EXPORTER_ENDPOINT",
        "https://www.comet.com/opik/api/v1/private/otel",
    )
    monkeypatch.setattr(
        telemetry.settings, "OTEL_METRICS_ENDPOINT", "http://collector:4318/v1/metrics"
    )
    exporter = telemetry._build_metric_exporter(OTLPMetricExporter)
    assert exporter is not None
    assert exporter._endpoint == "http://collector:4318/v1/metrics"


def test_the_opik_trace_endpoint_is_built_exactly_as_opik_documents_it(monkeypatch):
    """Pins the one string that makes the Opik integration a config change
    rather than a code change. Opik documents
    `<base>/api/v1/private/otel` as OTEL_EXPORTER_OTLP_ENDPOINT and expects
    the SDK to append the signal path."""
    monkeypatch.setattr(telemetry.settings, "OTEL_CONSOLE_EXPORT", False)
    monkeypatch.setattr(telemetry.settings, "OTEL_METRICS_ENABLED", False)
    monkeypatch.setattr(
        telemetry.settings,
        "OTEL_EXPORTER_ENDPOINT",
        "https://www.comet.com/opik/api/v1/private/otel",
    )

    span_exporter, metric_exporter = telemetry._build_exporters()

    assert span_exporter._endpoint == (
        "https://www.comet.com/opik/api/v1/private/otel/v1/traces"
    )
    assert metric_exporter is None


def test_a_failing_node_records_the_exception_on_its_span(spans, monkeypatch):
    """Almost every node catches its own failures and degrades rather than
    propagating, so the exceptions that DO escape are the ones nobody sees.
    The span is where they have to land."""
    from opentelemetry.trace import StatusCode

    import app.graph.workflow as workflow_module

    def boom(state):
        raise RuntimeError("planner exploded")

    monkeypatch.setitem(workflow_module.NODES, "planner", boom)
    # Rebuilt so the patched node is the one wrapped and wired.
    monkeypatch.setattr(
        workflow_module, "workflow", workflow_module.build_workflow()
    )
    monkeypatch.setattr("app.jobs.workflow", workflow_module.workflow)

    job = _run_a_city("Brockway")
    assert job.state == "failed"

    planner = next(
        s for s in spans.get_finished_spans() if s.name == "node.planner"
    )
    assert planner.status.status_code is StatusCode.ERROR
    assert any(e.name == "exception" for e in planner.events)

    root = next(s for s in spans.get_finished_spans() if s.parent is None)
    assert any(e.name == "exception" for e in root.events)


def test_telemetry_is_free_when_it_is_off():
    """The disabled path has to cost nothing and import nothing, because this
    also has to fit a 0.5 GB container. `span()` still has to return something
    with the full interface so no call site needs a conditional."""
    previous = telemetry._tracer
    telemetry._tracer = None
    try:
        with telemetry.span("anything", some="attribute") as s:
            assert s is telemetry._NOOP
            # Every method a call site uses, on the no-op.
            s.set_attribute("k", "v")
            s.set_attributes({"k": "v"})
            s.add_event("e", {"k": "v"})
            s.record_exception(ValueError("x"))
            assert s.is_recording() is False
        # Metric helpers are equally inert with no meter configured.
        telemetry.record_llm_usage("m", 1, 2)
        telemetry.record_node_duration("planner", 1.0)
        telemetry.record_fact("VERIFIED")
    finally:
        telemetry._tracer = previous


def test_an_exception_still_propagates_through_a_span():
    """Instrumentation must be transparent. A span that swallowed the
    exception it recorded would turn every failure into a silent wrong
    answer."""
    previous = telemetry._tracer
    telemetry._tracer = None
    try:
        with pytest.raises(ValueError, match="still raised"):
            with telemetry.span("x"):
                raise ValueError("still raised")
    finally:
        telemetry._tracer = previous
