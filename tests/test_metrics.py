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
