"""Metrics: instrument recording, cardinality refusal, export fail-soft, health.

All in-memory. No network, no real Collector. Two seams are used:
  * `metric_reader=InMemoryMetricReader()` — record and read points back, no export.
  * `exporter=<fake MetricExporter>` — drive the PeriodicExportingMetricReader so
    the export-health path (success/failure) is exercised without a socket.
"""
import pytest
from opentelemetry.sdk.metrics.export import (InMemoryMetricReader,
                                              MetricExporter, MetricExportResult)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import obskit
from obskit import GenAIMetric, LabMetric, MetricLabel, ObservationType, metrics, tracing


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _points(reader, name):
    """All histogram data points recorded under metric `name`."""
    data = reader.get_metrics_data()
    out = []
    for rm in getattr(data, "resource_metrics", []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def _names(reader):
    data = reader.get_metrics_data()
    return {m.name for rm in getattr(data, "resource_metrics", [])
            for sm in rm.scope_metrics for m in sm.metrics}


class _FakeExporter(MetricExporter):
    """A MetricExporter whose result is caller-controlled — no I/O."""

    def __init__(self):
        super().__init__()
        self.result = MetricExportResult.SUCCESS
        self.raises = False
        self.calls = 0

    def export(self, metrics_data, timeout_millis=10_000, **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.result

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kw):
        return None


@pytest.fixture()
def mem():
    """Tracing on (in-memory spans) + metrics on (in-memory reader)."""
    tracing.reset_for_tests()
    metrics.reset_for_tests()
    obskit.init(enabled_flag=True, service_name="obskit-tests",
                service_namespace="lab", deployment_environment="test",
                endpoint="", span_processor=SimpleSpanProcessor(InMemorySpanExporter()))
    reader = InMemoryMetricReader()
    res = obskit.setup_metrics(metric_reader=reader)
    assert res["status"] == "enabled", res
    yield reader
    metrics.reset_for_tests()
    tracing.reset_for_tests()


# ---------------------------------------------------------------------------
# each instrument records
# ---------------------------------------------------------------------------

def test_request_duration_records(mem):
    tracing.bind_request(user_id="u1", session_id="s1")
    root = tracing.start_root_span("chat", attributes={obskit.App.ROUTE: "code"})
    tracing.end_root_span(root, output="hi")
    pts = _points(mem, LabMetric.REQUEST_DURATION)
    assert len(pts) == 1
    attrs = dict(pts[0].attributes)
    assert attrs[MetricLabel.OUTCOME] == "ok"
    assert attrs[MetricLabel.ROUTE] == "code"
    assert pts[0].count == 1


def test_request_outcome_error(mem):
    tracing.bind_request()
    root = tracing.start_root_span("chat")
    tracing.end_root_span(root, error="ValueError: nope")
    pts = _points(mem, LabMetric.REQUEST_DURATION)
    assert dict(pts[0].attributes)[MetricLabel.OUTCOME] == "error"


def test_request_outcome_degraded(mem):
    tracing.bind_request()
    root = tracing.start_root_span("chat")
    root.set_degraded("reranker down", reason="rerank_failover")
    tracing.end_root_span(root, output="ok-ish")
    pts = _points(mem, LabMetric.REQUEST_DURATION)
    assert dict(pts[0].attributes)[MetricLabel.OUTCOME] == "degraded"


def test_model_call_duration_and_tokens_and_ttft(mem):
    tracing.bind_request()
    with tracing.generation("ollama.chat", model="llama3", provider="ollama") as sp:
        sp.set_completion_start()
        sp.set_usage(120, 45)

    assert GenAIMetric.OPERATION_DURATION in _names(mem)

    op = _points(mem, GenAIMetric.OPERATION_DURATION)[0]
    assert dict(op.attributes)[MetricLabel.MODEL] == "llama3"
    assert dict(op.attributes)[MetricLabel.OPERATION] == "chat"
    assert op.count == 1

    tok = _points(mem, GenAIMetric.TOKEN_USAGE)
    by_type = {dict(p.attributes)[MetricLabel.TOKEN_TYPE]: p.sum for p in tok}
    assert by_type["input"] == 120
    assert by_type["output"] == 45

    assert len(_points(mem, GenAIMetric.TIME_TO_FIRST_CHUNK)) == 1


def test_retrieval_duration_records(mem):
    tracing.bind_request()
    with tracing.span("memory.retrieve", observation_type=ObservationType.RETRIEVER):
        pass
    assert len(_points(mem, LabMetric.RETRIEVAL_DURATION)) == 1


# ---------------------------------------------------------------------------
# explicit histogram buckets are applied — NOT the SDK millisecond defaults
# ---------------------------------------------------------------------------

# The OTel SDK default explicit-bucket boundaries (millisecond-scaled). Recording
# seconds against these collapses every real latency into the first one or two
# buckets. This is exactly what obskit must NOT be on. Captured from the SDK
# (opentelemetry-sdk 1.44.0) so the test states the thing it is defending against.
_SDK_DEFAULT_BUCKETS = [0.0, 5.0, 10.0, 25.0, 50.0, 75.0, 100.0, 250.0, 500.0,
                        750.0, 1000.0, 2500.0, 5000.0, 7500.0, 10000.0]


def test_histograms_use_explicit_buckets_not_sdk_defaults(mem):
    """Every histogram must carry its own explicit boundaries. Fails loudly if
    someone drops explicit_bucket_boundaries_advisory and silently reverts to the
    SDK's millisecond-scaled defaults (the bug this release fixes)."""
    # drive one of every instrument through the real span lifecycle
    tracing.bind_request()
    root = tracing.start_root_span("chat", attributes={obskit.App.ROUTE: "code"})
    with tracing.generation("ollama.chat", model="llama3", provider="ollama") as sp:
        sp.set_completion_start()          # -> time_to_first_chunk
        sp.set_usage(120, 45)              # -> token.usage (input + output)
    with tracing.span("memory.retrieve", observation_type=ObservationType.RETRIEVER):
        pass                               # -> retrieval.duration
    tracing.end_root_span(root, output="hi")  # -> request.duration

    expected = {
        LabMetric.REQUEST_DURATION: metrics.REQUEST_DURATION_BUCKETS,
        GenAIMetric.OPERATION_DURATION: metrics.OPERATION_DURATION_BUCKETS,
        GenAIMetric.TOKEN_USAGE: metrics.TOKEN_USAGE_BUCKETS,
        GenAIMetric.TIME_TO_FIRST_CHUNK: metrics.TTFT_BUCKETS,
        LabMetric.RETRIEVAL_DURATION: metrics.RETRIEVAL_DURATION_BUCKETS,
    }
    for name, want in expected.items():
        pts = _points(mem, name)
        assert pts, f"{name} recorded no data point"
        got = [float(b) for b in pts[0].explicit_bounds]
        assert got == [float(b) for b in want], f"{name}: bounds {got} != {want}"
        assert got != _SDK_DEFAULT_BUCKETS, f"{name} is on the SDK default buckets"


def test_bucket_boundaries_are_sane():
    """Each bucket list is strictly increasing, positive, and omits the useless
    (-inf, 0] bucket the SDK default carries. Pure check — no provider needed."""
    for name, b in {
        "request": metrics.REQUEST_DURATION_BUCKETS,
        "operation": metrics.OPERATION_DURATION_BUCKETS,
        "ttft": metrics.TTFT_BUCKETS,
        "retrieval": metrics.RETRIEVAL_DURATION_BUCKETS,
        "tokens": metrics.TOKEN_USAGE_BUCKETS,
    }.items():
        assert b == sorted(b), f"{name} not sorted"
        assert len(b) == len(set(b)), f"{name} has duplicates"
        assert b[0] > 0, f"{name} includes a non-positive boundary"


# ---------------------------------------------------------------------------
# high-cardinality labels are refused / dropped (in code, not docs)
# ---------------------------------------------------------------------------

def test_labels_drops_non_allowlisted_keys():
    metrics.reset_for_tests()
    out = metrics._labels(**{"lab.request.id": "r1", "user.id": "u1",
                             "session.id": "s1", "trace_id": "t1",
                             "lab.retrieval.query": "who am i",
                             MetricLabel.ROUTE: "code"})
    assert out == {MetricLabel.ROUTE: "code"}


def test_value_cardinality_cap_folds_overflow():
    metrics.reset_for_tests()
    seen = {metrics._bounded(MetricLabel.MODEL, f"m{i}") for i in range(metrics._VALUE_CAP)}
    assert "__other__" not in seen
    # the (cap+1)-th distinct value folds
    assert metrics._bounded(MetricLabel.MODEL, "one-too-many") == "__other__"
    # a value already seen still returns itself
    assert metrics._bounded(MetricLabel.MODEL, "m0") == "m0"


def test_recorded_metric_carries_no_identifier_labels(mem):
    tracing.bind_request(request_id="rid-123", user_id="uid-9", session_id="sid-7")
    with tracing.generation("ollama.chat", model="llama3", provider="ollama") as sp:
        sp.set_usage(10, 2)
    op = _points(mem, GenAIMetric.OPERATION_DURATION)[0]
    keys = set(dict(op.attributes).keys())
    forbidden = {"lab.request.id", "request_id", "user.id", "user_id",
                 "session.id", "session_id", "gen_ai.conversation.id"}
    assert keys.isdisjoint(forbidden)
    assert keys <= metrics._ALLOWED_LABELS


def test_tenant_label_off_by_default(mem):
    tracing.bind_request(tenant_id="acme")
    with tracing.generation("ollama.chat", model="llama3") as sp:
        sp.set_usage(1, 1)
    op = _points(mem, GenAIMetric.OPERATION_DURATION)[0]
    assert MetricLabel.TENANT not in dict(op.attributes)


def test_tenant_label_opt_in_allowlist_folds_unknown():
    tracing.reset_for_tests()
    metrics.reset_for_tests()
    obskit.init(enabled_flag=True, service_name="t", service_namespace="lab",
                deployment_environment="test", endpoint="",
                span_processor=SimpleSpanProcessor(InMemorySpanExporter()))
    reader = InMemoryMetricReader()
    obskit.setup_metrics(metric_reader=reader, tenant_label=True,
                         tenant_allowlist=["acme"])
    tracing.bind_request(tenant_id="acme")
    with tracing.generation("ollama.chat", model="m") as sp:
        sp.set_usage(1, 1)
    tracing.bind_request(tenant_id="somebody-else")
    with tracing.generation("ollama.chat", model="m") as sp:
        sp.set_usage(1, 1)
    tenants = {dict(p.attributes).get(MetricLabel.TENANT)
               for p in _points(reader, GenAIMetric.OPERATION_DURATION)}
    assert tenants == {"acme", "other"}
    metrics.reset_for_tests()
    tracing.reset_for_tests()


# ---------------------------------------------------------------------------
# explicit-and-optional: init() alone must not build a meter provider
# ---------------------------------------------------------------------------

def test_init_alone_does_not_enable_metrics():
    tracing.reset_for_tests()
    metrics.reset_for_tests()
    obskit.init(enabled_flag=True, service_name="t", service_namespace="lab",
                deployment_environment="test", endpoint="",
                span_processor=SimpleSpanProcessor(InMemorySpanExporter()))
    assert obskit.metrics.enabled() is False
    assert metrics._meter is None
    # and a generation span records nothing / raises nothing
    tracing.bind_request()
    with tracing.generation("ollama.chat", model="m") as sp:
        sp.set_usage(1, 1)
    assert obskit.metrics.status()["initialized"] is False
    tracing.reset_for_tests()


# ---------------------------------------------------------------------------
# export failure is fail-soft, and health reflects success then failure
# ---------------------------------------------------------------------------

def test_export_failure_does_not_raise_and_health_tracks():
    tracing.reset_for_tests()
    metrics.reset_for_tests()
    obskit.init(enabled_flag=True, service_name="t", service_namespace="lab",
                deployment_environment="test", endpoint="",
                span_processor=SimpleSpanProcessor(InMemorySpanExporter()))
    fake = _FakeExporter()
    res = obskit.setup_metrics(exporter=fake, export_interval_millis=3_600_000)
    assert res["status"] == "enabled"

    # record something, then a successful flush
    tracing.bind_request()
    root = tracing.start_root_span("chat")
    tracing.end_root_span(root, output="x")
    assert metrics.flush() is True

    s = obskit.metrics.status()
    assert s["exports_succeeded"] >= 1
    assert s["consecutive_failures"] == 0
    assert s["last_success"] is not None
    assert s["health_source"] == "wrapped-exporter"

    # now make the exporter raise; flush must not propagate, health must update
    fake.raises = True
    root = tracing.start_root_span("chat")
    tracing.end_root_span(root, output="y")
    metrics.flush()  # must not raise
    s2 = obskit.metrics.status()
    assert s2["exports_failed"] >= 1
    assert s2["consecutive_failures"] >= 1
    assert s2["last_failure"] is not None
    assert s2["last_error"]

    metrics.reset_for_tests()
    tracing.reset_for_tests()


def test_setup_metrics_disabled_flag():
    tracing.reset_for_tests()
    metrics.reset_for_tests()
    res = obskit.setup_metrics(enabled_flag=False)
    assert res["status"] == "disabled"
    assert obskit.metrics.enabled() is False
    metrics.reset_for_tests()


def test_derive_metrics_endpoint():
    assert metrics._derive_metrics_endpoint(
        "http://127.0.0.1:4318/v1/traces") == "http://127.0.0.1:4318/v1/metrics"
    assert metrics._derive_metrics_endpoint(
        "http://c:4318/v1/metrics") == "http://c:4318/v1/metrics"
    assert metrics._derive_metrics_endpoint(
        "http://c:4318") == "http://c:4318/v1/metrics"
    assert metrics._derive_metrics_endpoint(None) is None


# ---------------------------------------------------------------------------
# record_metric=False — one real model call, one record, whatever the layering
# ---------------------------------------------------------------------------

def test_suppressed_generation_records_no_metric_but_still_spans(mem):
    """The wrapper layer must vanish from the histogram and stay in the trace."""
    tracing.bind_request()
    with tracing.generation("ollama.chat_stream", model="m", provider="ollama") as sp:
        sp.set_usage(10, 5)
    with tracing.generation("chat", model="m", provider="ollama",
                            record_metric=False) as sp:
        sp.set_usage(10, 5)

    pts = _points(mem, GenAIMetric.OPERATION_DURATION)
    assert sum(p.count for p in pts) == 1, "the wrapper must not be counted again"
    # tokens too: a suppressed span may not re-report the usage its child reported
    tok = sum(p.count for p in _points(mem, GenAIMetric.TOKEN_USAGE))
    assert tok == 2, tok  # one input + one output point, from the counted span only


def test_suppressed_span_is_still_a_full_generation_in_the_trace(mem):
    """Suppression is a METRICS decision. The span keeps its type and attributes,
    or a fix for double counting would quietly cost the trace its generation."""
    exporter = InMemorySpanExporter()
    tracing.reset_for_tests()
    metrics.reset_for_tests()
    obskit.init(enabled_flag=True, service_name="t", service_namespace="lab",
                deployment_environment="test", endpoint="",
                span_processor=SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    obskit.setup_metrics(metric_reader=reader)
    tracing.bind_request()
    with tracing.generation("chat", model="m", provider="ollama",
                            record_metric=False) as sp:
        sp.set_output("hello")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["langfuse.observation.type"] == ObservationType.GENERATION
    assert attrs["gen_ai.request.model"] == "m"
    assert not _points(reader, GenAIMetric.OPERATION_DURATION)
    metrics.reset_for_tests()
    tracing.reset_for_tests()


def test_suppressed_embedding_records_no_metric(mem):
    tracing.bind_request()
    with tracing.embedding("embeddings.embed", model="bge"):
        with tracing.embedding("ollama.embed", model="bge", record_metric=False):
            pass
    pts = _points(mem, GenAIMetric.OPERATION_DURATION)
    assert sum(p.count for p in pts) == 1


def test_record_metric_defaults_to_true(mem):
    """Nothing changes for an application that never passes the flag."""
    tracing.bind_request()
    with tracing.generation("ollama.chat", model="m"):
        pass
    with tracing.embedding("ollama.embed", model="bge"):
        pass
    assert sum(p.count for p in _points(mem, GenAIMetric.OPERATION_DURATION)) == 2


def test_operation_label_separates_background_from_the_users_turn(mem):
    """The point of the custom GenAIOperation values: a background call must be
    filterable out of the series the user's turn is measured in."""
    tracing.bind_request()
    with tracing.generation("ollama.chat_stream", model="m",
                            operation=obskit.GenAIOperation.CHAT):
        pass
    with tracing.generation("ollama.chat", model="m",
                            operation=obskit.GenAIOperation.EXTRACT_PROFILE):
        pass
    with tracing.generation("ollama.chat", model="m",
                            operation=obskit.GenAIOperation.GENERATE_TITLE):
        pass
    by_op = {}
    for p in _points(mem, GenAIMetric.OPERATION_DURATION):
        by_op[dict(p.attributes)[MetricLabel.OPERATION]] = p.count
    assert by_op == {"chat": 1, "extract_profile": 1, "generate_title": 1}, by_op
