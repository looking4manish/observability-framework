"""OpenTelemetry tracing, exported to Langfuse over OTLP/HTTP.

Part of `obskit` (distribution: observability-framework). Builds a real span tree:
one request-scoped root span per unit of work, with every model call, retrieval,
rerank, tool invocation and agent step hanging off it as a child.

Design constraints, in priority order:

1. **Nothing here may ever reach the request path.** Every public function
   swallows its own exceptions and returns a no-op handle. If the collector is
   down, the SDK is missing, or an attribute fails to serialize, chat still
   works. This is the same contract the previous implementation had, kept.

2. **Parenting must not depend on ambient OTel context.** Most of the
   interesting work happens inside async generators (the SSE stream, the agent
   loop, refine's three passes). A contextvar set inside an async generator is
   visible to the caller's context and can be restored across a `yield`, so
   relying on `start_as_current_span` alone would silently produce orphans.
   Instead every span resolves its parent explicitly: an explicit `parent=`
   handle wins, else the innermost live span from `_parent_span_var`, else the
   request root from `_root_span_var`, else ambient. Deep callsites
   (ollama_client, embeddings, reranker) need no arguments threaded to them.

3. **Identifiers live in contextvars, not in signatures.** `bind_request()`
   stamps request/user/session ids for the whole task; any callsite can read
   them. FastAPI runs the endpoint and its StreamingResponse generator in the
   same task, so the binding survives the streaming boundary.

Export is a `BatchSpanProcessor` over OTLP/HTTP to Langfuse's ingestion
endpoint (`/api/public/otel/v1/traces`, HTTP Basic with the project keys), so
span emission is queued in-memory and flushed by a background thread — the
request path never blocks on the collector. `shutdown()` drains that queue on
service stop; without it a restart drops whatever was still queued.

Enablement is resolved once at startup by `init()`, so flipping it requires a
process restart — the OTel tracer provider is process-global.

`init()` takes its configuration as explicit arguments. It reads no settings
object, no config module and no environment variable of its own, so the package
has no opinion about how the host application stores configuration. Service
identity (service.name, service.namespace, deployment.environment) is REQUIRED:
a missing value raises at startup rather than silently exporting spans as
`unknown_service`.

Every attribute value passes through `redaction.scrub()` inside
`_Span.set_attribute`, so an application cannot leak a credential by forgetting
to redact at a callsite.
"""
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from .redaction import scrub
from .semconv import (App, GenAI, Langfuse, ObservationLevel, ObservationType,
                      SEMCONV_VERSION)

# ---------------------------------------------------------------------------
# module state (all writes happen once, from init())
# ---------------------------------------------------------------------------

_provider = None
_tracer = None
_enabled = False
_init_error: str | None = None
_endpoint: str | None = None   # resolved OTLP URL, surfaced by status()
_service_name: str | None = None
_service_namespace: str | None = None
_service_version: str | None = None
_environment: str | None = None
_tracer_name: str | None = None

# ---------------------------------------------------------------------------
# request-scoped identity + span parenting
# ---------------------------------------------------------------------------

_request_id_var: ContextVar[str | None] = ContextVar("obskit_request_id", default=None)
_user_id_var: ContextVar[str | None] = ContextVar("obskit_user_id", default=None)
_session_id_var: ContextVar[str | None] = ContextVar("obskit_session_id", default=None)
# Multi-tenancy rides the SAME mechanism as the other request identifiers: one
# contextvar, set once by bind_request(), read by _start() when it stamps each
# span. Nothing about span parenting changes — parenting is _root_span_var and
# _parent_span_var, and those are untouched. See the note in bind_request().
_tenant_id_var: ContextVar[str | None] = ContextVar("obskit_tenant_id", default=None)
_root_span_var: ContextVar[object | None] = ContextVar("obskit_root_span", default=None)
_parent_span_var: ContextVar[object | None] = ContextVar("obskit_parent_span", default=None)
# Every set_degraded() in this request, in order. Read by end_root_span (so the ROOT
# is marked, not just the leaf that failed) and by callers that want to stamp an
# intermediate span. Injection 2 showed the leaf-only marking was not enough: a
# trace read as clean unless you expanded to the one child that had failed.
_degraded_var: ContextVar[list | None] = ContextVar("obskit_degraded", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request(*, request_id: str | None = None, user_id: str | None = None,
                 session_id: str | None = None, tenant_id: str | None = None) -> str:
    """Stamp this task with request/user/session/tenant ids. Returns the request id.

    Safe to call when tracing is off — the ids are useful for logging too, and
    binding them costs a contextvar set.

    On tenant_id and the contextvar mechanism: this extends the existing identity
    contextvars, NOT the parenting ones. Parenting is `_parent_span_var` and
    `_root_span_var`, resolved explicitly by `_parent_context()` (module docstring
    point 2) precisely because a `.set()` inside an async generator is not visible
    to the caller's context. `tenant_id` has no such hazard: it is a plain
    immutable string bound once per request at the outermost frame, exactly like
    request/user/session ids, and read — never written — from deeper frames. So it
    inherits the existing guarantee without adding to the fragile part.
    """
    rid = request_id or new_request_id()
    try:
        _request_id_var.set(rid)
        if user_id is not None:
            _user_id_var.set(user_id)
        if session_id is not None:
            _session_id_var.set(session_id)
        if tenant_id is not None:
            _tenant_id_var.set(tenant_id)
        # A *mutable* list, set once per request and then only appended to. Deliberate:
        # a contextvar `.set()` from inside an async generator is not visible to the
        # caller's context, and most degradation happens inside generators (the SSE
        # stream, the agent loop). Mutating one list avoids that trap entirely — same
        # reason chat.py threads its `outcome` dict instead of a contextvar.
        _degraded_var.set([])
    except Exception:  # noqa: BLE001
        pass
    return rid


def record_degradation(reason: str, message: str = "") -> None:
    """Append a degradation to this request's list. Called by set_degraded()."""
    try:
        lst = _degraded_var.get()
        if lst is not None and len(lst) < 50:  # bound it; a retry storm must not grow forever
            lst.append({"reason": reason, "message": message[:300]})
    except Exception:  # noqa: BLE001
        pass


def degradations() -> list:
    """Every degradation recorded in this request so far (possibly empty)."""
    return list(_degraded_var.get() or [])


def mark_degraded_from_request(handle, *, prefix: str = "degraded") -> bool:
    """Stamp `handle` with this request's accumulated degradations, if any.

    Lets an ancestor span inherit a descendant's degradation without threading a
    return value through every layer between them. Returns True if it marked.
    """
    ds = degradations()
    if not ds or handle is None or handle is NULL_SPAN:
        return False
    reasons = sorted({d["reason"] for d in ds})
    try:
        handle.set_attribute(Langfuse.OBSERVATION_LEVEL, ObservationLevel.WARNING)
        handle.set_attribute(App.DEGRADED, True)
        handle.set_attribute(App.DEGRADED_REASON, ",".join(reasons)[:200])
        handle.set_attribute(App.DEGRADED_COUNT, len(ds))
        handle.set_attribute(
            Langfuse.OBSERVATION_STATUS_MESSAGE,
            f"{prefix}: {len(ds)} degradation(s) — " + "; ".join(
                d["message"] for d in ds)[:1800])
    except Exception:  # noqa: BLE001
        return False
    return True


def set_context_state(handle, state: dict) -> None:
    """Stamp a span with the CONVERSATION's truncation state.

    Why a count and not just a boolean: `lab.hot_messages = 4` was ambiguous —
    identical for a fresh four-message chat and for one whose earlier twelve turns
    had been evicted. A count of compressed messages disambiguates it and a boolean
    is derivable from it (count > 0), so the count is strictly more informative for
    the same one attribute.

    Why `compressed_by_trace` is a stored id and not an OTel span link: links must be
    supplied when a span is *created*, and the compression that shaped this turn's
    context happened in an earlier request whose ids are not in scope at creation
    time. More decisively, Langfuse's OTLP ingestion does not surface span links in
    the UI, so a correct link would be invisible in the one tool this is read in. A
    trace id carried on the conversation document is durable, greppable, and
    pasteable into Langfuse.
    """
    if handle is None or handle is NULL_SPAN or not state:
        return
    try:
        n = int(state.get("compressed_messages") or 0)
        handle.set_attribute(App.CONTEXT_COMPRESSED_MESSAGES, n)
        handle.set_attribute(App.CONTEXT_TRUNCATED, n > 0)
        if state.get("hot_tokens") is not None:
            handle.set_attribute(App.CONTEXT_HOT_TOKENS, int(state["hot_tokens"]))
        if state.get("compressed_by_trace"):
            handle.set_attribute(App.CONTEXT_COMPRESSED_BY_TRACE,
                                 str(state["compressed_by_trace"]))
    except Exception:  # noqa: BLE001
        pass


def request_id() -> str | None:
    return _request_id_var.get()


def user_id() -> str | None:
    return _user_id_var.get()


def session_id() -> str | None:
    return _session_id_var.get()


def tenant_id() -> str | None:
    return _tenant_id_var.get()


def enabled() -> bool:
    return _enabled and _tracer is not None


def init_error() -> str | None:
    return _init_error


def status() -> dict:
    """Whether tracing is live, where it exports to, and which semconv it speaks.

    `init()` returns the same facts, but only to the startup log line — which
    uvicorn's logging config swallows. Without this on /api/status the only way
    to tell a live exporter from a silently-disabled one is to send a turn and
    go look in Langfuse, which is exactly the silent-failure mode the tracing
    work exists to remove.
    """
    return {
        "initialized": _tracer is not None,
        "enabled": enabled(),
        "endpoint": _endpoint,
        "service": _service_name,
        "service_namespace": _service_namespace,
        "service_version": _service_version,
        "environment": _environment,
        "semconv_version": SEMCONV_VERSION,
        "error": _init_error,
    }


# ---------------------------------------------------------------------------
# startup / shutdown
# ---------------------------------------------------------------------------

class ServiceIdentityError(ValueError):
    """Raised when init() is called without complete service identity.

    Deliberately loud. A missing service.name does not break tracing in an
    obvious way — OTel silently falls back to `unknown_service`, every
    application on the collector merges into one indistinguishable stream, and
    nobody notices until they try to filter by service months later. Failing at
    startup is the cheaper failure.
    """


def _require(name: str, value) -> str:
    if value is None or not str(value).strip():
        raise ServiceIdentityError(
            f"obskit.tracing.init(): {name} is required and must be a non-empty "
            f"string. Service identity is not defaulted — see ServiceIdentityError."
        )
    return str(value).strip()


def init(*, enabled_flag: bool,
         service_name: str,
         service_namespace: str,
         deployment_environment: str,
         endpoint: str,
         headers: dict | None = None,
         service_version: str = "dev",
         tracer_name: str | None = None,
         resource_attributes: dict | None = None,
         max_export_batch_size: int = 64,
         schedule_delay_millis: int = 1000,
         span_processor=None) -> dict:
    """Build the tracer provider and OTLP exporter. Idempotent for the happy path.

    Configuration is passed in, not read: no settings object, no env var, no
    config module. `endpoint` is the full OTLP traces URL and `headers` carries
    whatever auth the collector wants (for Langfuse: HTTP Basic of
    public_key:secret_key). `helpers.langfuse_endpoint()` builds both.

    Raises ServiceIdentityError if service_name, service_namespace or
    deployment_environment is missing or blank — that is the one failure this
    function does not swallow, and it happens before any provider is built.

    `span_processor` is for tests: pass a SimpleSpanProcessor over an in-memory
    exporter and no network is touched.
    """
    global _provider, _tracer, _enabled, _init_error, _endpoint
    global _service_name, _service_namespace, _environment, _tracer_name
    global _service_version

    # Validated BEFORE the enabled_flag short-circuit, on purpose: a misconfigured
    # service identity is a deployment bug, and it must surface whether or not
    # tracing happens to be switched on in this environment.
    svc = _require("service_name", service_name)
    ns = _require("service_namespace", service_namespace)
    env = _require("deployment_environment", deployment_environment)

    if _tracer is not None:
        return {"status": "already-initialized", "enabled": _enabled}

    _service_name, _service_namespace, _environment = svc, ns, env
    _service_version = service_version
    _tracer_name = tracer_name or svc

    if not enabled_flag:
        _enabled = False
        return {"status": "disabled", "reason": "enabled_flag is false"}

    if span_processor is None and not endpoint:
        _enabled = False
        _init_error = "no OTLP endpoint configured"
        return {"status": "disabled", "reason": _init_error}

    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _endpoint = endpoint or None
        attrs = {
            "service.name": svc,
            "service.namespace": ns,
            "service.version": service_version,
            "deployment.environment.name": env,
            # Kept alongside the 1.37.0 name because collectors and dashboards in
            # the wild still filter on the older key.
            "deployment.environment": env,
            **(resource_attributes or {}),
        }
        provider = TracerProvider(resource=Resource.create(attrs))

        if span_processor is not None:
            provider.add_span_processor(span_processor)
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(
                endpoint=endpoint, headers=dict(headers or {}), timeout=10)
            # Small batch + short delay: low-volume workloads would rather see a
            # trace land promptly than batch efficiently.
            provider.add_span_processor(BatchSpanProcessor(
                exporter,
                max_export_batch_size=max_export_batch_size,
                schedule_delay_millis=schedule_delay_millis))

        otel_trace.set_tracer_provider(provider)
        _provider = provider
        _tracer = provider.get_tracer(_tracer_name)
        _enabled = True
        _init_error = None
        return {"status": "enabled", "endpoint": _endpoint, "service": svc,
                "namespace": ns, "environment": env}
    except Exception as e:  # noqa: BLE001
        _provider = None
        _tracer = None
        _enabled = False
        _init_error = f"{type(e).__name__}: {e}"
        return {"status": "error", "reason": _init_error}


def reset_for_tests() -> None:
    """Drop module state so a test can init() again. Not for production use."""
    global _provider, _tracer, _enabled, _init_error, _endpoint
    global _service_name, _service_namespace, _environment, _tracer_name
    global _service_version
    _provider = _tracer = None
    _enabled = False
    _init_error = _endpoint = None
    _service_name = _service_namespace = _environment = _tracer_name = None
    _service_version = None
    for v in (_request_id_var, _user_id_var, _session_id_var, _tenant_id_var,
              _root_span_var, _parent_span_var, _degraded_var):
        try:
            v.set(None)
        except Exception:  # noqa: BLE001
            pass


def shutdown(timeout_millis: int = 8000) -> None:
    """Flush queued spans on service stop, and the meter provider if metrics are
    on. Never raises."""
    global _enabled
    try:
        if _provider is not None:
            _provider.force_flush(timeout_millis)
            _provider.shutdown()
    except Exception:  # noqa: BLE001
        pass
    # If metrics were turned on (setup_metrics), flush and stop the meter
    # provider on the same process-stop path — so the app has one shutdown()
    # to call, not two. No-op when metrics were never set up.
    try:
        from . import metrics
        metrics.shutdown(timeout_millis)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _enabled = False


def flush(timeout_millis: int = 5000) -> bool:
    """Force-flush the batch queue. Used by tests and the admin surface."""
    try:
        if _provider is None:
            return False
        return bool(_provider.force_flush(timeout_millis))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------

_MAX_ATTR_CHARS = 60_000  # keep a single span well under Langfuse's payload limits

# Prompt-side utilisation at which a generation marks itself degraded. Set high on
# purpose: the window filling is normal, the window being nearly full is the point
# at which eviction starts costing recall, and a lower bar would make WARNING the
# steady state on any long conversation.
_CONTEXT_PRESSURE = 0.90


def _serialize(value) -> str | None:
    """JSON-encode a value for a Langfuse input/output attribute.

    Scrubs before encoding, so a credential nested inside a dict or list is
    redacted rather than merely redacted-looking after it becomes one JSON blob.
    """
    if value is None:
        return None
    value = scrub(value)
    try:
        if isinstance(value, str):
            out = value
        else:
            out = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        try:
            out = str(value)
        except Exception:  # noqa: BLE001
            return None
    if len(out) > _MAX_ATTR_CHARS:
        out = out[:_MAX_ATTR_CHARS] + f"...[truncated {len(out) - _MAX_ATTR_CHARS} chars]"
    return out


def _attr_value(value):
    """Coerce to something the OTel attribute types accept."""
    if isinstance(value, (str, bool, int, float)):
        return value
    return _serialize(value)


def redact_messages(msgs: list[dict]) -> list[dict]:
    """Strip base64 image payloads from traced input — keep the shape, drop the
    megabytes so traces stay light and readable."""
    out = []
    for m in msgs or []:
        content = m.get("content", "") or ""
        if m.get("images"):
            content = (content + " [image]").strip()
        entry = {"role": m.get("role", ""), "content": content}
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_name"):
            entry["tool_name"] = m["tool_name"]
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# span handles
# ---------------------------------------------------------------------------

class _NullSpan:
    """Returned whenever tracing is off or anything failed. Absorbs everything."""

    trace_id = None
    span_id = None
    is_recording = False

    def set(self, *_a, **_k):
        return self

    set_attribute = set_attributes = set

    def set_input(self, _value):
        return self

    def set_output(self, _value):
        return self

    def set_model(self, _model, parameters=None):
        return self

    def set_usage(self, _input_tokens=None, _output_tokens=None, **_k):
        return self

    def set_metadata(self, _mapping=None, **_k):
        return self

    def set_completion_start(self):
        return self

    def set_error(self, _exc, message=None):
        return self

    def set_degraded(self, _message, **_k):
        return self

    def end(self, _end_time=None):
        return self


class _Span:
    """Thin wrapper over an OTel span that speaks Langfuse's attribute dialect."""

    def __init__(self, otel_span):
        self._span = otel_span
        self._ended = False
        # Remembered from set_model() so set_usage() can compute context-window
        # utilisation without any callsite having to pass num_ctx twice. This is
        # why utilisation lands on every generation span in the app — chat, stream,
        # title, summarize, contextualize, refine passes, agent steps — for free.
        self._num_ctx: int | None = None
        # --- metric capture (read by obskit.metrics at span close) -----------
        # These mirror, in plain fields, the few bounded facts a metric needs, so
        # the metrics layer never has to parse span attributes back out. They are
        # populated by the same set_* calls the application already makes; nothing
        # new is threaded through any callsite. `_metric_t0` is a monotonic clock
        # read (immune to wall-clock steps) used purely for duration.
        self._metric_t0: float = time.monotonic()
        self._metric_obs_type: str | None = None
        self._metric_route: str | None = None
        self._metric_model: str | None = None
        self._metric_operation: str | None = None
        self._metric_provider: str | None = None
        self._metric_input_tokens = None
        self._metric_output_tokens = None
        self._metric_ttft: float | None = None
        self._metric_errored: bool = False
        self._metric_degraded: bool = False

    # -- identity ---------------------------------------------------------
    @property
    def trace_id(self) -> str | None:
        try:
            return format(self._span.get_span_context().trace_id, "032x")
        except Exception:  # noqa: BLE001
            return None

    @property
    def span_id(self) -> str | None:
        try:
            return format(self._span.get_span_context().span_id, "016x")
        except Exception:  # noqa: BLE001
            return None

    @property
    def is_recording(self) -> bool:
        try:
            return bool(self._span.is_recording())
        except Exception:  # noqa: BLE001
            return False

    # -- attributes -------------------------------------------------------
    def set_attribute(self, key: str, value):
        """Set one attribute, redacted.

        This is the choke point: every attribute on every span in the package
        goes through here, including the ones set by set_input/set_output/
        set_metadata/set_usage. Redaction lives here rather than at the callsites
        so an application cannot bypass it by forgetting.
        """
        try:
            if value is not None:
                self._span.set_attribute(key, scrub(_attr_value(value)))
                # Snapshot the handful of BOUNDED attributes a metric labels by,
                # into plain fields. Only these keys, never request/user/session
                # ids — so the metrics layer physically cannot read a
                # high-cardinality value off a span even if it tried.
                if key == App.ROUTE:
                    self._metric_route = value
                elif key == GenAI.REQUEST_MODEL:
                    self._metric_model = value
                elif key == GenAI.OPERATION_NAME:
                    self._metric_operation = value
                elif key == GenAI.PROVIDER_NAME:
                    self._metric_provider = value
        except Exception:  # noqa: BLE001
            pass
        return self

    def set_attributes(self, mapping: dict | None):
        for k, v in (mapping or {}).items():
            self.set_attribute(k, v)
        return self

    def set_input(self, value):
        return self.set_attribute(Langfuse.OBSERVATION_INPUT, _serialize(value))

    def set_output(self, value):
        return self.set_attribute(Langfuse.OBSERVATION_OUTPUT, _serialize(value))

    def set_model(self, model: str | None, parameters: dict | None = None):
        if model:
            self.set_attribute(Langfuse.OBSERVATION_MODEL, model)
            self.set_attribute(GenAI.REQUEST_MODEL, model)
        if parameters:
            self.set_attribute(Langfuse.OBSERVATION_MODEL_PARAMETERS, _serialize(parameters))
            # Lift the few parameters semconv has real names for.
            if parameters.get("temperature") is not None:
                self.set_attribute(GenAI.REQUEST_TEMPERATURE, parameters["temperature"])
            if parameters.get("top_p") is not None:
                self.set_attribute(GenAI.REQUEST_TOP_P, parameters["top_p"])
            if parameters.get("num_predict") is not None:
                self.set_attribute(GenAI.REQUEST_MAX_TOKENS, parameters["num_predict"])
            nc = parameters.get("num_ctx")
            if isinstance(nc, int) and nc > 0:
                self._num_ctx = nc
                self.set_attribute(App.CONTEXT_WINDOW, nc)
        return self

    def set_usage(self, input_tokens=None, output_tokens=None, **extra):
        details = {}
        if input_tokens is not None:
            details["input"] = int(input_tokens)
            self.set_attribute(GenAI.USAGE_INPUT_TOKENS, int(input_tokens))
            self._metric_input_tokens = int(input_tokens)
        if output_tokens is not None:
            details["output"] = int(output_tokens)
            self.set_attribute(GenAI.USAGE_OUTPUT_TOKENS, int(output_tokens))
            self._metric_output_tokens = int(output_tokens)
        details.update({k: v for k, v in extra.items() if v is not None})
        if details:
            self.set_attribute(Langfuse.OBSERVATION_USAGE_DETAILS, _serialize(details))
        self._record_utilisation(input_tokens, output_tokens)
        return self

    def _record_utilisation(self, input_tokens, output_tokens) -> None:
        """prompt_tokens / num_ctx, plus (prompt+completion) / num_ctx.

        Injection 3's finding: num_ctx sat in model_parameters and prompt tokens sat
        in usage, and nothing ever divided one by the other — so there was no way to
        chart how close turns were running to the window, or to alert before the hot
        window started costing recall. Total utilisation is recorded too because that
        is what actually overflows: generation shares the window with the prompt.
        """
        if not self._num_ctx or input_tokens is None:
            return
        try:
            pu = int(input_tokens) / self._num_ctx
            self.set_attribute(App.CONTEXT_PROMPT_UTILISATION, round(pu, 4))
            tot = int(input_tokens) + int(output_tokens or 0)
            self.set_attribute(App.CONTEXT_TOTAL_UTILISATION,
                               round(tot / self._num_ctx, 4))
            if pu >= _CONTEXT_PRESSURE:
                self.set_degraded(
                    f"context window {pu:.0%} full on the prompt alone "
                    f"({input_tokens}/{self._num_ctx}) — older turns are being "
                    f"evicted and recall will degrade",
                    reason="context_window_pressure")
        except Exception:  # noqa: BLE001
            pass

    def set_metadata(self, mapping: dict | None = None, **kw):
        merged = {**(mapping or {}), **kw}
        for k, v in merged.items():
            if v is None:
                continue
            # Langfuse flattens metadata as `<prefix>.<key>`; matching that keeps
            # the keys individually filterable in the UI instead of one JSON blob.
            self.set_attribute(f"{Langfuse.OBSERVATION_METADATA}.{k}", v)
        return self

    def set_completion_start(self):
        """Mark time-to-first-token on a generation.

        Besides the Langfuse timestamp attribute, capture TTFT as an elapsed
        duration (first-token time minus span start) for the metrics layer — the
        attribute alone is a wall-clock instant, which a histogram cannot use.
        """
        if self._metric_ttft is None:
            try:
                self._metric_ttft = max(0.0, time.monotonic() - self._metric_t0)
            except Exception:  # noqa: BLE001
                pass
        return self.set_attribute(
            Langfuse.OBSERVATION_COMPLETION_START_TIME,
            _serialize(datetime.now(timezone.utc).isoformat()),
        )

    def set_error(self, exc, message: str | None = None):
        try:
            from opentelemetry.trace import Status, StatusCode

            text = message or (f"{type(exc).__name__}: {exc}" if exc else "error")
            self._metric_errored = True
            self._span.set_status(Status(StatusCode.ERROR, text))
            self.set_attribute(Langfuse.OBSERVATION_LEVEL, ObservationLevel.ERROR)
            self.set_attribute(Langfuse.OBSERVATION_STATUS_MESSAGE, text[:2000])
            if isinstance(exc, BaseException):
                self._span.record_exception(exc)
        except Exception:  # noqa: BLE001
            pass
        return self

    def set_degraded(self, message: str, *, reason: str | None = None):
        """Mark a span as *soft-degraded*: it succeeded, but on a worse path.

        The gap this closes: a dead dependency behind a graceful fallback used to
        produce a green, DEFAULT-level, statusMessage-None span. Anything alerting
        on span level or status saw nothing, so the only tell was a custom
        attribute you had to already know the name of.

        Deliberately NOT set_error(): the turn did not fail, so the OTel span
        status stays UNSET and this never shows up as an error rate. What it does
        set is the Langfuse level (so the trace renders yellow and level-based
        rules fire) and `lab.degraded`, one boolean that is the same across
        every degradation path in the app — so a single alert covers all of them
        without enumerating per-subsystem attribute names.
        """
        try:
            self._metric_degraded = True
            self.set_attribute(Langfuse.OBSERVATION_LEVEL, ObservationLevel.WARNING)
            self.set_attribute(Langfuse.OBSERVATION_STATUS_MESSAGE, str(message)[:2000])
            self.set_attribute(App.DEGRADED, True)
            if reason:
                self.set_attribute(App.DEGRADED_REASON, str(reason)[:200])
            record_degradation(reason or "degraded", str(message))
        except Exception:  # noqa: BLE001
            pass
        return self

    def end(self, end_time=None):
        if self._ended:
            return self
        self._ended = True
        try:
            self._span.end(end_time=end_time)
        except Exception:  # noqa: BLE001
            pass
        return self


NULL_SPAN = _NullSpan()


# ---------------------------------------------------------------------------
# span creation
# ---------------------------------------------------------------------------

def _parent_context(parent):
    """Resolve the parent context for a new span. See module docstring point 2."""
    try:
        from opentelemetry import trace as otel_trace

        candidate = parent
        if candidate is None:
            candidate = _parent_span_var.get()
        if candidate is None:
            candidate = _root_span_var.get()
        if candidate is None:
            return None  # fall back to ambient OTel context
        raw = getattr(candidate, "_span", candidate)
        return otel_trace.set_span_in_context(raw)
    except Exception:  # noqa: BLE001
        return None


def _start(name: str, *, observation_type: str, parent=None, start_time=None,
           input=None, metadata=None, model=None, model_parameters=None,
           attributes: dict | None = None):
    """Create a span. Returns NULL_SPAN if tracing is off or creation failed."""
    if not enabled():
        return NULL_SPAN
    try:
        raw = _tracer.start_span(
            name, context=_parent_context(parent), start_time=start_time
        )
        h = _Span(raw)
        h._metric_obs_type = observation_type
        h.set_attribute(Langfuse.OBSERVATION_TYPE, observation_type)
        rid = _request_id_var.get()
        if rid:
            h.set_attribute(App.REQUEST_ID, rid)
        tid = _tenant_id_var.get()
        if tid:
            h.set_attribute(App.TENANT_ID, tid)
        sid = _session_id_var.get()
        if sid:
            h.set_attribute(GenAI.CONVERSATION_ID, sid)
        if input is not None:
            h.set_input(input)
        if model:
            h.set_model(model, model_parameters)
        if metadata:
            h.set_metadata(metadata)
        if attributes:
            h.set_attributes(attributes)
        return h
    except Exception:  # noqa: BLE001
        return NULL_SPAN


@contextmanager
def _managed(handle):
    """Make `handle` the parent for everything opened inside the block, and
    close it on the way out (recording an exception if one escaped)."""
    token = None
    try:
        if handle is not NULL_SPAN:
            token = _parent_span_var.set(handle)
    except Exception:  # noqa: BLE001
        token = None
    try:
        yield handle
    except Exception as e:  # noqa: BLE001
        handle.set_error(e)
        raise
    finally:
        handle.end()
        # Emit the matching metric (model-call / retrieval), if metrics are on.
        # Imported lazily to keep tracing importable without the metrics module's
        # SDK imports, and to sidestep the tracing<->metrics import cycle. No-op
        # unless the application called setup_metrics().
        try:
            from . import metrics
            metrics.on_span_end(handle)
        except Exception:  # noqa: BLE001
            pass
        if token is not None:
            try:
                _parent_span_var.reset(token)
            except Exception:  # noqa: BLE001
                pass


@contextmanager
def span(name: str, *, observation_type: str = ObservationType.SPAN, parent=None,
         input=None, metadata=None, attributes: dict | None = None, start_time=None):
    """Generic child span. Usable around sync or async code:

        with tracing.span("retrieval", observation_type=ObservationType.RETRIEVER) as sp:
            docs = await memory.retrieve(...)
            sp.set_output(len(docs))
    """
    with _managed(_start(name, observation_type=observation_type, parent=parent,
                         start_time=start_time, input=input, metadata=metadata,
                         attributes=attributes)) as h:
        yield h


@contextmanager
def generation(name: str, *, model: str | None = None, parent=None, input=None,
               model_parameters: dict | None = None, metadata: dict | None = None,
               operation: str = "chat", provider: str | None = None,
               attributes: dict | None = None, start_time=None):
    """A model call. Renders as a Langfuse generation with model + token usage.

    `provider` has no default: this package does not know which vendor an
    application talks to. When omitted, gen_ai.provider.name / gen_ai.system are
    simply not set rather than being set to something untrue.
    """
    attrs = {GenAI.OPERATION_NAME: operation, **(attributes or {})}
    if provider:
        attrs.setdefault(GenAI.PROVIDER_NAME, provider)
        attrs.setdefault(GenAI.SYSTEM, provider)
    with _managed(_start(name, observation_type=ObservationType.GENERATION, parent=parent,
                         start_time=start_time, input=input, metadata=metadata,
                         model=model, model_parameters=model_parameters,
                         attributes=attrs)) as h:
        yield h


@contextmanager
def embedding(name: str, *, model: str | None = None, parent=None, input=None,
              metadata: dict | None = None, attributes: dict | None = None,
              provider: str | None = None):
    """An embedding call — its own Langfuse observation type."""
    attrs = {GenAI.OPERATION_NAME: "embeddings", **(attributes or {})}
    if provider:
        attrs.setdefault(GenAI.PROVIDER_NAME, provider)
    with _managed(_start(name, observation_type=ObservationType.EMBEDDING, parent=parent,
                         input=input, metadata=metadata, model=model,
                         attributes=attrs)) as h:
        yield h


@contextmanager
def tool_span(name: str, *, parent=None, input=None, metadata: dict | None = None,
              attributes: dict | None = None):
    """A tool invocation (built-in or MCP)."""
    attrs = {GenAI.OPERATION_NAME: "execute_tool", GenAI.TOOL_NAME: name,
             **(attributes or {})}
    with _managed(_start(name, observation_type=ObservationType.TOOL, parent=parent,
                         input=input, metadata=metadata, attributes=attrs)) as h:
        yield h


def start_root_span(name: str, *, input=None, metadata: dict | None = None,
                    tags: list[str] | None = None, attributes: dict | None = None):
    """Open the request-scoped root span and make it this task's default parent.

    Not a context manager: the chat root has to outlive the endpoint function
    and be closed from inside the streaming generator. Call `end_root_span()`
    (ideally in a `finally`) to close it.

    Trace-level attributes (name, user, session) are set here because Langfuse
    reads them off the root span when assembling the trace.
    """
    if not enabled():
        return NULL_SPAN
    h = _start(name, observation_type=ObservationType.SPAN, input=input,
               metadata=metadata, attributes=attributes)
    if h is NULL_SPAN:
        return h
    try:
        h.set_attribute(Langfuse.TRACE_NAME, name)
        uid, sid, rid = _user_id_var.get(), _session_id_var.get(), _request_id_var.get()
        if uid:
            h.set_attribute(Langfuse.TRACE_USER_ID, uid)
        if sid:
            h.set_attribute(Langfuse.TRACE_SESSION_ID, sid)
        if rid:
            h.set_attribute(App.REQUEST_ID, rid)
        if input is not None:
            h.set_attribute(Langfuse.TRACE_INPUT, _serialize(input))
        if tags:
            h.set_attribute(Langfuse.TRACE_TAGS, _serialize(tags))
        if _environment:
            h.set_attribute(Langfuse.ENVIRONMENT, _environment)
        tid = _tenant_id_var.get()
        if tid:
            h.set_attribute(App.TENANT_ID, tid)
        _root_span_var.set(h)
        _parent_span_var.set(h)
    except Exception:  # noqa: BLE001
        pass
    return h


def adopt_root(handle) -> None:
    """Re-assert `handle` as this context's root/parent span.

    Insurance for the streaming boundary: FastAPI runs the endpoint and the
    StreamingResponse generator in the same task today, so the contextvars set
    by `start_root_span()` carry over — but that is an implementation detail of
    the ASGI stack (a `BaseHTTPMiddleware` anywhere in the chain would run the
    generator in a fresh task and silently orphan every child span). Calling
    this at the top of the generator makes the parenting explicit either way.
    """
    if handle is None or handle is NULL_SPAN:
        return
    try:
        _root_span_var.set(handle)
        _parent_span_var.set(handle)
    except Exception:  # noqa: BLE001
        pass


def end_root_span(handle=None, *, output=None, error: str | None = None) -> None:
    """Close the root span, stamping the trace-level output. Never raises."""
    h = handle if handle is not None else _root_span_var.get()
    if h is None or h is NULL_SPAN:
        return
    try:
        if output is not None:
            h.set_output(output)
            h.set_attribute(Langfuse.TRACE_OUTPUT, _serialize(output))
        if error:
            h.set_error(None, message=error)
        else:
            # Lift any descendant degradation onto the ROOT, so the trace shows as
            # WARNING in a trace list without expanding anything. An error takes
            # precedence — a failed turn is not merely degraded.
            mark_degraded_from_request(h, prefix="turn degraded")
    except Exception:  # noqa: BLE001
        pass
    finally:
        h.end()
        # Request-level metric. Outcome is decided HERE (not read off the span):
        # an explicit error wins; else any degradation recorded during this
        # request makes it "degraded"; else "ok". Degradation is read from the
        # request's accumulated list rather than a span flag because it is lifted
        # onto the root via set_attribute, not set_degraded. Metrics off -> no-op.
        try:
            if error:
                outcome = "error"
            elif degradations():
                outcome = "degraded"
            else:
                outcome = "ok"
            from . import metrics
            metrics.on_request_end(h, outcome=outcome)
        except Exception:  # noqa: BLE001
            pass
        try:
            _root_span_var.set(None)
            _parent_span_var.set(None)
        except Exception:  # noqa: BLE001
            pass


def _current_resource():
    """The SDK Resource init() built, so metrics can reuse the exact same one
    (identical service.* and service.instance.id) and thus correlate with traces.
    Returns None if tracing was never initialized."""
    try:
        return _provider.resource if _provider is not None else None
    except Exception:  # noqa: BLE001
        return None


def service_identity() -> dict:
    """Whatever init() was given, for anything that needs to stamp it (e.g. logs)."""
    return {"service_name": _service_name, "service_namespace": _service_namespace,
            "service_version": _service_version, "deployment_environment": _environment}


def current_span_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) of the INNERMOST live span, else the request root.

    Innermost first on purpose: a log line emitted inside `with span("rerank")`
    should carry the rerank span's id, not the turn root's, so a reader lands on
    the exact span. Both spans share a trace id either way. Formats are the OTel
    canonical hex — 32 chars for trace, 16 for span — which is what the trace
    backend displays, so a value copied out of a log line pastes straight in.
    """
    h = _parent_span_var.get() or _root_span_var.get()
    if h is None or h is NULL_SPAN:
        return None, None
    return getattr(h, "trace_id", None), getattr(h, "span_id", None)


def current_trace_id() -> str | None:
    """Langfuse trace id for the active request, for logs/response headers."""
    h = _root_span_var.get()
    return getattr(h, "trace_id", None) if h is not None else None


# ---------------------------------------------------------------------------
# back-compat
# ---------------------------------------------------------------------------

def record_generation(*, name: str, model: str, input, output: str,
                      usage_details: dict | None = None, metadata: dict | None = None,
                      user_id: str | None = None, session_id: str | None = None,
                      provider: str | None = None) -> None:
    """Legacy one-shot generation, now emitted as a span under the current root.

    Kept so any caller outside the chat path keeps working; the chat path builds
    a proper tree instead.
    """
    if not enabled():
        return
    try:
        h = _start(name, observation_type=ObservationType.GENERATION, input=input,
                   model=model, metadata=metadata,
                   attributes={GenAI.OPERATION_NAME: "chat",
                               **({GenAI.PROVIDER_NAME: provider} if provider else {})})
        if h is NULL_SPAN:
            return
        h.set_output(output)
        if usage_details:
            h.set_usage(usage_details.get("input"), usage_details.get("output"))
        if user_id:
            h.set_attribute(Langfuse.TRACE_USER_ID, user_id)
        if session_id:
            h.set_attribute(Langfuse.TRACE_SESSION_ID, session_id)
        h.end()
    except Exception:  # noqa: BLE001
        pass
