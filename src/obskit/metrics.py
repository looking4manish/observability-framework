"""OpenTelemetry metrics, exported to the OTLP Collector over HTTP.

Part of `obskit` (distribution: observability-framework). Metrics are the third
signal, alongside the span tree (`tracing`) and structured logs (`logs`). A
trace tells you what one request did; a metric tells you what a thousand requests
did in aggregate — the rate, the latency distribution, the error and degradation
fractions — without keeping a row per request.

Design, in the same priority order the rest of the package holds:

1. **Nothing here may ever reach the request path.** Every public function
   swallows its own exceptions and returns None (or a status dict). If the
   Collector is down, the SDK is missing, or a label fails to build, recording a
   metric is a no-op and the application keeps serving. Same contract as tracing.

2. **Explicit and optional, exactly like `setup_logging`.** `tracing.init()`
   does NOT build a meter provider and never will. An application that wants only
   traces gets no MeterProvider. Metrics are turned on by a separate,
   deliberately-named call — `setup_metrics()` — which returns a status dict
   rather than raising. Until it is called, the recording hooks below are inert.

3. **Cardinality is enforced in code, not documentation.** Every metric label
   passes through `_labels()`, which (a) keeps only keys on a fixed ALLOW-list,
   (b) drops any key on the DENY-list even if it somehow reached here, and
   (c) caps the number of distinct VALUES per label, folding the overflow to
   `__other__` so no label can silently explode the time-series count. The
   public recording surface never accepts a free-form attribute dict, so a
   request id / trace id / user id has no parameter to arrive through in the
   first place. See `_labels` and the module-level ALLOW/DENY sets.

4. **Redaction is the existing one.** String label values pass through
   `redaction.scrub_text` — the same function tracing and logging use. There is
   no second redaction implementation.

Wiring: the recording happens inside `tracing`'s own span lifecycle
(`_managed`/`end_root_span`), so an application that already emits spans gets the
matching metrics for free once it calls `setup_metrics()` — no new callsites.
The hooks (`on_span_end`, `on_request_end`) are no-ops until then.

Export health: the OTLP metric exporter does not expose whether its last export
succeeded. To report that honestly rather than inventing it, `setup_metrics`
wraps the real exporter in `_HealthTrackingExporter`, which records the result of
each `export()` call. When metrics run against an in-memory reader (tests) there
is no exporter and no push, so the health fields are None — stated, not faked.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .redaction import scrub_text
from .semconv import GenAIMetric, LabMetric, MetricLabel, TokenType

# ---------------------------------------------------------------------------
# module state (all writes happen from setup_metrics(), read by the hooks)
# ---------------------------------------------------------------------------

_provider = None
_meter = None
_enabled = False
_init_error: str | None = None
_endpoint: str | None = None
_interval_millis: int | None = None

# Instruments. All five are histograms — see README/report: every requested
# measurement is either a duration or a token count, and a histogram already
# carries a monotonic `count` (so request/model-call count need no separate
# counter) and a `sum` (so total tokens need no separate counter). A separate
# counter would double the series for information the histogram already exposes.
_h_request = None      # lab.request.duration       — request latency + count + outcome
_h_operation = None    # gen_ai.client.operation.duration — model-call latency + count
_h_tokens = None       # gen_ai.client.token.usage  — input/output tokens
_h_ttft = None         # gen_ai.client.operation.time_to_first_chunk — TTFT
_h_retrieval = None    # lab.retrieval.duration     — retrieval latency + count

_health = None         # _HealthTrackingExporter, when a real exporter is used

# ---------------------------------------------------------------------------
# cardinality control
# ---------------------------------------------------------------------------

# Only these keys may ever become a label. Anything else is dropped by _labels()
# before it reaches the SDK. This is the primary structural defence: the public
# recording path has no free-form attribute parameter, and even the internal
# builder refuses unknown keys.
_ALLOWED_LABELS = frozenset({
    MetricLabel.ROUTE,       # closed set: the app's route classifier output
    MetricLabel.OUTCOME,     # ok | error | degraded | aborted
    MetricLabel.MODEL,       # bounded by installed models (capped, see _bounded)
    MetricLabel.OPERATION,   # chat | embeddings | execute_tool | invoke_agent
    MetricLabel.PROVIDER,    # ollama | ... (capped)
    MetricLabel.TOKEN_TYPE,  # input | output
    MetricLabel.TENANT,      # opt-in only; capped + allow-listed (see setup)
})

# Belt-and-suspenders. These are the high-cardinality identifiers the brief names
# explicitly: even if a future refactor routed one of them into _labels(), it is
# dropped here. None of them is on the ALLOW-list, so this is redundant by
# design — redundancy is the point.
_DENY_LABELS = frozenset({
    "lab.request.id", "request_id", "request.id",
    "trace_id", "trace.id", "span_id", "span.id",
    "user.id", "user_id",
    "session.id", "session_id",
    "gen_ai.conversation.id", "conversation.id", "conversation_id",
    "lab.retrieval.query", "query", "message", "text", "prompt", "input", "output",
})

# Per-label distinct-value ceiling. The (label, value) pairs seen so far are
# remembered; once a label has this many distinct values, every further NEW value
# folds to "__other__" so the series count for that label stops growing. Applies
# to the labels whose value set is bounded-in-practice-but-not-in-theory (model,
# route, provider, tenant). Values already seen keep their own series.
_VALUE_CAP = 50
_OVERFLOW = "__other__"
_seen_values: dict[str, set] = {}

# tenant labelling: off unless the application opts in (see setup_metrics).
_tenant_label_enabled = False
_tenant_allowlist: frozenset = frozenset()

_MAX_LABEL_CHARS = 64


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(
            timespec="milliseconds")
    except Exception:  # noqa: BLE001
        return None


def _bounded(key: str, value) -> str | None:
    """Coerce, scrub, length-cap and cardinality-cap one label value.

    Returns the value to use, "__other__" if this label is already at its
    distinct-value ceiling, or None to drop it. Never raises.
    """
    if value is None:
        return None
    try:
        v = scrub_text(str(value))[:_MAX_LABEL_CHARS]
    except Exception:  # noqa: BLE001
        return None
    seen = _seen_values.setdefault(key, set())
    if v in seen:
        return v
    if len(seen) >= _VALUE_CAP:
        return _OVERFLOW
    seen.add(v)
    return v


def _labels(**kw) -> dict:
    """Build the attribute dict for an instrument, enforcing the label policy.

    (1) keep only ALLOW-listed keys, (2) drop DENY-listed keys, (3) bound each
    value's length and per-label cardinality. The one place any label is built.
    """
    out: dict = {}
    for key, value in kw.items():
        if key not in _ALLOWED_LABELS or key in _DENY_LABELS:
            continue
        bv = _bounded(key, value)
        if bv is not None:
            out[key] = bv
    return out


def _with_tenant(labels: dict) -> dict:
    """Add the tenant label IF opted in and the value is permitted. Never raises.

    Tenant is bounded today but the brief warns it may not stay bounded, so it is
    off by default. When enabled: a value outside the configured allow-list folds
    to "other" (not the raw id), and the distinct-value cap still applies on top.
    """
    if not _tenant_label_enabled:
        return labels
    try:
        from . import tracing
        t = tracing.tenant_id()
        if not t:
            return labels
        t = str(t)
        if _tenant_allowlist and t not in _tenant_allowlist:
            t = "other"
        bv = _bounded(MetricLabel.TENANT, t)
        if bv is not None:
            labels[MetricLabel.TENANT] = bv
    except Exception:  # noqa: BLE001
        pass
    return labels


# ---------------------------------------------------------------------------
# export health
# ---------------------------------------------------------------------------

def _make_health_exporter(inner):
    """Wrap a MetricExporter so each export()'s result is recorded. Lazy so the
    SDK import stays inside setup_metrics()'s try (fail-soft on a missing SDK)."""
    from opentelemetry.sdk.metrics.export import (MetricExporter,
                                                  MetricExportResult)

    class _HealthTrackingExporter(MetricExporter):
        """Delegates to a real exporter, remembering the last export outcome.

        The OTLP SDK exposes no success/failure signal of its own; this observes
        the value the reader already acts on, so the numbers in status() are real
        rather than guessed.
        """

        def __init__(self, delegate):
            super().__init__(
                preferred_temporality=getattr(delegate, "_preferred_temporality", None),
                preferred_aggregation=getattr(delegate, "_preferred_aggregation", None),
            )
            self._delegate = delegate
            self.exports_succeeded = 0
            self.exports_failed = 0
            self.consecutive_failures = 0
            self.last_success: float | None = None
            self.last_failure: float | None = None
            self.last_error: str | None = None

        def export(self, metrics_data, timeout_millis: float = 10_000, **kw):
            try:
                res = self._delegate.export(metrics_data,
                                            timeout_millis=timeout_millis, **kw)
            except Exception as e:  # noqa: BLE001
                self.exports_failed += 1
                self.consecutive_failures += 1
                self.last_failure = time.time()
                self.last_error = f"{type(e).__name__}: {e}"
                return MetricExportResult.FAILURE
            if res == MetricExportResult.SUCCESS:
                self.exports_succeeded += 1
                self.consecutive_failures = 0
                self.last_success = time.time()
            else:
                self.exports_failed += 1
                self.consecutive_failures += 1
                self.last_failure = time.time()
                self.last_error = "exporter returned FAILURE"
            return res

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            try:
                return bool(self._delegate.force_flush(timeout_millis))
            except Exception:  # noqa: BLE001
                return False

        def shutdown(self, timeout_millis: float = 30_000, **kw) -> None:
            try:
                self._delegate.shutdown(timeout_millis, **kw)
            except Exception:  # noqa: BLE001
                pass

    return _HealthTrackingExporter(inner)


# ---------------------------------------------------------------------------
# setup / teardown
# ---------------------------------------------------------------------------

def _derive_metrics_endpoint(traces_endpoint: str | None) -> str | None:
    """Turn an OTLP *traces* URL into the *metrics* URL on the same Collector.

    The metrics OTLP path is `/v1/metrics` (the SDK's own default). We derive it
    from the traces endpoint rather than asking the caller for a second URL,
    because they point at the same Collector; the only difference is the signal
    path.
    """
    if not traces_endpoint:
        return None
    ep = traces_endpoint
    if ep.endswith("/v1/traces"):
        return ep[: -len("/v1/traces")] + "/v1/metrics"
    if ep.endswith("/v1/metrics"):
        return ep
    return ep.rstrip("/") + "/v1/metrics"


def setup_metrics(*, enabled_flag: bool = True,
                  endpoint: str | None = None,
                  headers: dict | None = None,
                  export_interval_millis: int = 10_000,
                  export_timeout_millis: int = 10_000,
                  tenant_label: bool = False,
                  tenant_allowlist=None,
                  value_cardinality_cap: int = 50,
                  resource=None,
                  metric_reader=None,
                  exporter=None) -> dict:
    """Build the meter provider and OTLP metric exporter. Explicit, optional, fail-soft.

    Mirrors `logs.setup_logging`: `tracing.init()` never calls this, it returns a
    status dict instead of raising, and calling it is the only way metrics turn
    on. Idempotent-ish: a second call while already enabled is a no-op.

    endpoint         full OTLP *metrics* URL. If omitted, it is derived from the
                     traces endpoint `tracing.init()` was given (same Collector,
                     `/v1/metrics` path). headers carry whatever auth the
                     Collector wants (for the local Collector: none).
    export_interval_millis  how often the PeriodicExportingMetricReader flushes.
    tenant_label     include lab.tenant.id as a label. OFF by default — tenant is
                     bounded today but may not stay so (see report).
    tenant_allowlist iterable of tenant ids permitted as label values when
                     tenant_label is on; anything else folds to "other".
    value_cardinality_cap  per-label distinct-value ceiling before folding to
                     "__other__".
    resource         an SDK Resource to attach. If omitted, the one tracing.init()
                     built is reused (so metrics and traces share service.* and
                     service.instance.id and correlate); failing that, one is
                     built from tracing.service_identity().
    metric_reader    for tests: an in-memory reader. No exporter, no network, no
                     health tracking (stated in status()).
    exporter         for tests: a raw MetricExporter to wrap with health tracking
                     and drive with a PeriodicExportingMetricReader.
    """
    global _provider, _meter, _enabled, _init_error, _endpoint, _interval_millis
    global _health, _tenant_label_enabled, _tenant_allowlist, _VALUE_CAP

    if _meter is not None:
        return {"status": "already-initialized", "enabled": _enabled}

    _tenant_label_enabled = bool(tenant_label)
    _tenant_allowlist = frozenset(str(t) for t in (tenant_allowlist or []))
    if isinstance(value_cardinality_cap, int) and value_cardinality_cap > 0:
        _VALUE_CAP = value_cardinality_cap

    if not enabled_flag:
        _enabled = False
        return {"status": "disabled", "reason": "enabled_flag is false"}

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        # --- resolve the resource, reusing tracing's so signals correlate ----
        res = resource
        if res is None:
            try:
                from . import tracing
                res = tracing._current_resource()
            except Exception:  # noqa: BLE001
                res = None
        if res is None:
            from opentelemetry.sdk.resources import Resource
            from . import tracing
            ident = tracing.service_identity()
            attrs = {k: v for k, v in {
                "service.name": ident.get("service_name"),
                "service.namespace": ident.get("service_namespace"),
                "service.version": ident.get("service_version"),
                "deployment.environment.name": ident.get("deployment_environment"),
                "deployment.environment": ident.get("deployment_environment"),
            }.items() if v}
            res = Resource.create(attrs)

        # --- build the reader ------------------------------------------------
        _health = None
        if metric_reader is not None:
            reader = metric_reader
            _endpoint = None
        else:
            if exporter is not None:
                inner = exporter
                _endpoint = endpoint
            else:
                metrics_endpoint = endpoint
                if not metrics_endpoint:
                    from . import tracing
                    metrics_endpoint = _derive_metrics_endpoint(tracing._endpoint)
                if not metrics_endpoint:
                    _enabled = False
                    _init_error = "no OTLP metrics endpoint configured"
                    return {"status": "disabled", "reason": _init_error}
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
                inner = OTLPMetricExporter(
                    endpoint=metrics_endpoint, headers=dict(headers or {}),
                    timeout=max(1, export_timeout_millis // 1000))
                _endpoint = metrics_endpoint
            _health = _make_health_exporter(inner)
            reader = PeriodicExportingMetricReader(
                _health, export_interval_millis=export_interval_millis,
                export_timeout_millis=export_timeout_millis)

        provider = MeterProvider(resource=res, metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)
        _provider = provider
        _meter = provider.get_meter("obskit.metrics")
        _interval_millis = export_interval_millis

        _build_instruments(_meter)

        _enabled = True
        _init_error = None
        return {"status": "enabled", "endpoint": _endpoint,
                "interval_millis": _interval_millis,
                "tenant_label": _tenant_label_enabled}
    except Exception as e:  # noqa: BLE001
        _provider = _meter = None
        _enabled = False
        _init_error = f"{type(e).__name__}: {e}"
        return {"status": "error", "reason": _init_error}


# ---------------------------------------------------------------------------
# histogram bucket boundaries
# ---------------------------------------------------------------------------
#
# Every histogram below ships EXPLICIT bucket boundaries, sized to what this
# system actually does. Without them the SDK falls back to its default boundaries
# — [0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000, 7500, 10000] —
# which are calibrated for MILLISECONDS. Every duration here is recorded in
# SECONDS (unit="s"; see _elapsed), so real latencies collapse into the first one
# or two of those buckets and p50/p95/p99 become indistinguishable. Ranges below
# are measured on the Legion Chat workload (Prometheus + trace artifacts), not
# guessed: retrieval ~15 ms, time-to-first-token sub-second to a few seconds,
# model calls ~0.05–25 s, whole chat turns ~2–50 s (a compression-heavy turn was
# observed at 48 s).
#
# These are passed as `explicit_bucket_boundaries_advisory`, i.e. as a property of
# the INSTRUMENT rather than as a MeterProvider View. That is the library-correct
# seam: obskit *advises* the scale it knows about, and an adopting application can
# still override any of these with its own View without editing this package. A
# View shipped from here would instead silently win over the app's own config.
#
# Series cost: each histogram produces (finite buckets + 1 for +Inf + _sum +
# _count) time series PER label-value combination. Every count below is LOWER than
# the 18/combo the 15-boundary default produced — resolution went up while the
# series count went down. See the README "Histogram buckets" section.
#
# CHANGING THESE IS A BREAKING CHANGE: re-bucketing makes histograms recorded
# before and after incomparable (old and new le-series do not line up), so it
# rides a version bump exactly like an attribute rename (working rule 2).

# lab.request.duration — a whole request / chat turn. Observed ~2–50 s, tail to a
# ~48 s compression turn. 10 finite buckets -> 13 series/combo.
REQUEST_DURATION_BUCKETS = [0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120]

# gen_ai.client.operation.duration — one model call, fast embeddings through long
# generations/summarisation. Observed mean ~2.9 s, tail into 10–25 s. 11 finite
# buckets -> 14 series/combo.
OPERATION_DURATION_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64]

# gen_ai.client.operation.time_to_first_chunk — the latency a user actually feels.
# Its useful range is far shorter than a whole turn, so it gets five sub-second
# buckets and a shorter top (30 s, for cold model loads). Observed mean ~0.9 s.
# 10 finite buckets -> 13 series/combo.
TTFT_BUCKETS = [0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 6, 12, 30]

# lab.retrieval.duration — vector search + rerank. Milliseconds to a few seconds;
# observed mean ~15 ms. Fine millisecond resolution where the mass is. 10 finite
# buckets -> 13 series/combo.
RETRIEVAL_DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5]

# gen_ai.client.token.usage — a COUNT, not a duration ({token}); bucketed here too
# so no instrument is left on accidental defaults and the no-defaults test can
# cover all five. Sized for token counts: small outputs through large input
# contexts (observed 1–5.3k tokens; headroom to 100k). 12 finite -> 15 series/combo.
TOKEN_USAGE_BUCKETS = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]


def _build_instruments(meter) -> None:
    global _h_request, _h_operation, _h_tokens, _h_ttft, _h_retrieval
    _h_request = meter.create_histogram(
        LabMetric.REQUEST_DURATION, unit="s",
        description="Duration of an application request (root span), by route and outcome.",
        explicit_bucket_boundaries_advisory=REQUEST_DURATION_BUCKETS)
    _h_operation = meter.create_histogram(
        GenAIMetric.OPERATION_DURATION, unit="s",
        description="GenAI operation (model call) duration.",
        explicit_bucket_boundaries_advisory=OPERATION_DURATION_BUCKETS)
    _h_tokens = meter.create_histogram(
        GenAIMetric.TOKEN_USAGE, unit="{token}",
        description="Number of input and output tokens used, by token type.",
        explicit_bucket_boundaries_advisory=TOKEN_USAGE_BUCKETS)
    _h_ttft = meter.create_histogram(
        GenAIMetric.TIME_TO_FIRST_CHUNK, unit="s",
        description="Time from issuing a model call to its first streamed chunk.",
        explicit_bucket_boundaries_advisory=TTFT_BUCKETS)
    _h_retrieval = meter.create_histogram(
        LabMetric.RETRIEVAL_DURATION, unit="s",
        description="Duration of a retrieval (vector search + rerank), by outcome.",
        explicit_bucket_boundaries_advisory=RETRIEVAL_DURATION_BUCKETS)


def reset_for_tests() -> None:
    """Drop metrics module state so a test can setup_metrics() again."""
    global _provider, _meter, _enabled, _init_error, _endpoint, _interval_millis
    global _health, _tenant_label_enabled, _tenant_allowlist, _VALUE_CAP
    global _h_request, _h_operation, _h_tokens, _h_ttft, _h_retrieval
    try:
        if _provider is not None:
            _provider.shutdown()
    except Exception:  # noqa: BLE001
        pass
    _provider = _meter = None
    _enabled = False
    _init_error = _endpoint = _interval_millis = None
    _health = None
    _h_request = _h_operation = _h_tokens = _h_ttft = _h_retrieval = None
    _tenant_label_enabled = False
    _tenant_allowlist = frozenset()
    _VALUE_CAP = 50
    _seen_values.clear()


def flush(timeout_millis: int = 10_000) -> bool:
    """Force the reader to collect and export now. Used by tests and admin."""
    try:
        if _provider is None:
            return False
        return bool(_provider.force_flush(timeout_millis))
    except Exception:  # noqa: BLE001
        return False


def shutdown(timeout_millis: int = 10_000) -> None:
    """Flush and stop the meter provider. Never raises."""
    global _enabled
    try:
        if _provider is not None:
            _provider.force_flush(timeout_millis)
            _provider.shutdown()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _enabled = False


def enabled() -> bool:
    return _enabled and _meter is not None


def status() -> dict:
    """Whether metrics are live, where they export, and whether export succeeds now.

    Extends the startup-decision reporting `tracing.status()` does for traces with
    LIVE export health, observed by wrapping the exporter (see module docstring).
    Fields that the SDK genuinely does not expose are reported as None with a note
    rather than invented.
    """
    h = _health
    return {
        "initialized": _meter is not None,
        "enabled": enabled(),
        "endpoint": _endpoint,
        "export_interval_millis": _interval_millis,
        "tenant_label_enabled": _tenant_label_enabled,
        "value_cardinality_cap": _VALUE_CAP,
        "instruments": ([LabMetric.REQUEST_DURATION, GenAIMetric.OPERATION_DURATION,
                         GenAIMetric.TOKEN_USAGE, GenAIMetric.TIME_TO_FIRST_CHUNK,
                         LabMetric.RETRIEVAL_DURATION] if enabled() else []),
        # live export health — real numbers from the wrapped exporter, or None
        # (with a reason) when there is no push exporter to observe.
        "exports_succeeded": getattr(h, "exports_succeeded", None),
        "exports_failed": getattr(h, "exports_failed", None),
        "consecutive_failures": getattr(h, "consecutive_failures", None),
        "last_success": _iso(getattr(h, "last_success", None)),
        "last_failure": _iso(getattr(h, "last_failure", None)),
        "last_error": getattr(h, "last_error", None),
        "health_source": ("wrapped-exporter" if h is not None
                          else "unavailable (in-memory reader or metrics off)"),
        "error": _init_error,
    }


# ---------------------------------------------------------------------------
# recording hooks — called by tracing's span lifecycle, no-op until setup
# ---------------------------------------------------------------------------

def _elapsed(handle) -> float | None:
    try:
        t0 = getattr(handle, "_metric_t0", None)
        if t0 is None:
            return None
        return max(0.0, time.monotonic() - t0)
    except Exception:  # noqa: BLE001
        return None


def _span_outcome(handle) -> str:
    if getattr(handle, "_metric_errored", False):
        return "error"
    if getattr(handle, "_metric_degraded", False):
        return "degraded"
    return "ok"


def on_span_end(handle) -> None:
    """Record the metric a just-closed span implies, if any. Never raises.

    generation/embedding -> gen_ai.client.operation.duration (+ tokens, TTFT for
    generations); retriever -> lab.retrieval.duration. Everything else records
    nothing. Inert unless setup_metrics() has run.
    """
    if not _enabled or handle is None:
        return
    try:
        ot = getattr(handle, "_metric_obs_type", None)
        if ot in ("generation", "embedding"):
            _record_operation(handle)
        elif ot == "retriever":
            _record_retrieval(handle)
    except Exception:  # noqa: BLE001
        pass


def _record_operation(handle) -> None:
    dur = _elapsed(handle)
    labels = _with_tenant(_labels(**{
        MetricLabel.OPERATION: getattr(handle, "_metric_operation", None),
        MetricLabel.MODEL: getattr(handle, "_metric_model", None),
        MetricLabel.PROVIDER: getattr(handle, "_metric_provider", None),
        MetricLabel.OUTCOME: _span_outcome(handle),
    }))
    if dur is not None and _h_operation is not None:
        _h_operation.record(dur, labels)

    if getattr(handle, "_metric_obs_type", None) != "generation":
        return
    tok_base = _labels(**{
        MetricLabel.MODEL: getattr(handle, "_metric_model", None),
        MetricLabel.OPERATION: getattr(handle, "_metric_operation", None),
    })
    it = getattr(handle, "_metric_input_tokens", None)
    ot = getattr(handle, "_metric_output_tokens", None)
    if it is not None and _h_tokens is not None:
        _h_tokens.record(int(it), {**tok_base, MetricLabel.TOKEN_TYPE: TokenType.INPUT})
    if ot is not None and _h_tokens is not None:
        _h_tokens.record(int(ot), {**tok_base, MetricLabel.TOKEN_TYPE: TokenType.OUTPUT})
    ttft = getattr(handle, "_metric_ttft", None)
    if ttft is not None and _h_ttft is not None:
        _h_ttft.record(float(ttft), tok_base)


def _record_retrieval(handle) -> None:
    dur = _elapsed(handle)
    if dur is None or _h_retrieval is None:
        return
    labels = _with_tenant(_labels(**{MetricLabel.OUTCOME: _span_outcome(handle)}))
    _h_retrieval.record(dur, labels)


def on_request_end(handle, *, outcome: str) -> None:
    """Record lab.request.duration for a closing root span. Never raises.

    outcome is decided by the caller (tracing.end_root_span): error | degraded |
    ok. The histogram's count IS the request count, and its outcome slices give
    the error and degraded-turn rates — so no separate counters are emitted.
    Inert unless setup_metrics() has run.
    """
    if not _enabled or handle is None or _h_request is None:
        return
    try:
        dur = _elapsed(handle)
        if dur is None:
            return
        labels = _with_tenant(_labels(**{
            MetricLabel.ROUTE: getattr(handle, "_metric_route", None),
            MetricLabel.OUTCOME: outcome,
        }))
        _h_request.record(dur, labels)
    except Exception:  # noqa: BLE001
        pass
