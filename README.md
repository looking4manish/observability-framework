# observability-framework (`obskit`)

OpenTelemetry span-tree tracing, structured logging and metrics for LLM
applications, exported over OTLP/HTTP (to a Collector, or straight to Langfuse
for traces).

Extracted from the Legion Chat application so other applications can share the
same vocabulary and the same tracing behaviour. Distribution name is
`observability-framework`; the import name is `obskit`.

```python
import obskit

obskit.init(
    enabled_flag=True,
    service_name="my-api",
    service_namespace="lab",
    deployment_environment="prod",
    **obskit.langfuse_endpoint(host, public_key, secret_key),
)

obskit.bind_request(user_id=uid, session_id=conv_id, tenant_id="acme")
root = obskit.start_root_span("chat")
try:
    with obskit.generation("llm.chat", model="qwen3:32b", provider="ollama") as sp:
        sp.set_usage(prompt_tokens, completion_tokens)
finally:
    obskit.end_root_span(root, output=answer)
```

## What it gives you over raw OTel

- A **Langfuse-shaped span tree** — `generation`, `embedding`, `tool`,
  `retriever`, `agent`, `chain` observation types, with model parameters and
  token usage in the shape Langfuse ingests.
- **Explicit parenting that survives async generators.** Most interesting work
  in a streaming LLM app happens inside `async def ... yield`. A contextvar set
  inside an async generator is not reliably visible to the caller, so relying on
  `start_as_current_span` alone silently produces orphan spans. Every span here
  resolves its parent explicitly: an explicit `parent=` wins, else the innermost
  live span, else the request root, else ambient context.
- **Soft-degradation marking.** A dependency that failed over gracefully still
  produces a WARNING span with `lab.degraded=true`, and it propagates to the
  root so a degraded request is visible in a trace list without expanding
  anything.
- **Metrics that refuse to explode.** The same span lifecycle that builds the
  trace also records request/model-call/retrieval histograms — no new callsites.
  Every label is filtered against a fixed allow-list and a per-label
  cardinality cap *in code*, so a request id or user id has no way to become a
  time series. Opt-in, exactly like the logging. See [Metrics](#metrics).
- **Fail-soft everywhere.** Every public function swallows its own exceptions and
  returns a no-op handle. A dead collector never reaches the request path.

## Service identity is required

`init()` raises `ServiceIdentityError` if `service_name`, `service_namespace` or
`deployment_environment` is missing or blank. There are no defaults.

This is deliberate. A missing `service.name` does not fail visibly — OTel falls
back to `unknown_service`, every application on the collector merges into one
indistinguishable stream, and nobody notices until they try to filter by service
months later. Failing at startup is the cheaper failure.

Identity is validated **before** the `enabled_flag` check, so a misconfigured
deployment surfaces even in an environment where tracing is switched off.

These land as OTel resource attributes: `service.name`, `service.namespace`,
`service.version`, `deployment.environment.name` (and `deployment.environment`
alongside it, because collectors and dashboards in the wild still filter on the
older key).

## Configuration

`init()` reads nothing. No settings object, no config module, no environment
variable of its own — all configuration is passed as explicit arguments, so the
package has no opinion about how the host application stores config.
`langfuse_endpoint(host, public_key, secret_key)` builds the `endpoint` and
`headers` kwargs for the Langfuse OTLP ingestion path.

## Redaction

Credential scrubbing runs inside `_Span.set_attribute` — the single choke point
every attribute passes through, including those set by `set_input`,
`set_output`, `set_metadata` and `set_usage`. It is **not** a helper you call at
the callsite, because an application would eventually forget to.

**Strategy: redact the secret, keep the shape.** A trace is a debugging tool, and
replacing a whole value with `[REDACTED]` destroys the reason you were looking at
it. Each pattern keeps the parts that identify *what* the value was and drops
only the part that authenticates:

| Input | Output | Kept, and why |
|---|---|---|
| `mongodb://app:s3cret@db1:27017/x` | `mongodb://app:[REDACTED]@db1:27017/x` | scheme, username, host, port, database — "which database, which credential" are normal debugging questions; the username is not the secret |
| `Authorization: Bearer eyJhbGci...` | `Authorization: Bearer [REDACTED]` | the auth scheme, so you can still tell bearer from basic |
| `sk-lf-abcdef0123456789` | `sk-lf-[REDACTED]` | the vendor prefix, so you can still tell which key type leaked |
| `-----BEGIN RSA PRIVATE KEY-----...` | `[REDACTED PRIVATE KEY BLOCK]` | nothing — nothing inside a key block is worth keeping |
| `"password": "hunter2"` | `"password": "[REDACTED]"` | the key name, so the shape of the payload survives |

Covered: `mongodb://`, `mongodb+srv://`, `postgres://`, `postgresql://`,
`mysql://`, `redis://`, `rediss://`, `amqp://`, `amqps://`, plus a generic
`scheme://user:pass@host` backstop; `Bearer`/`Basic`/`Token` headers; vendor API
keys (`sk-`, `pk-`, `sk-lf-`, `ghp_`, `xoxb-`, `AKIA`, `glpat-`, `hf_`, `AIza`,
…); `password=` / `secret=` / `api_key=` style key-value pairs; and PEM private
key blocks including truncated ones.

Containers are walked, so a credential nested inside a dict or list is redacted
before the structure is serialized into one JSON attribute.

**Stated limits.** This is best-effort regex matching, not a proof. A credential
in a shape not listed above passes through. It reduces blast radius; it is not a
guarantee and it is not a substitute for keeping secrets out of traces. It is
also on the hot path, so a cheap substring pre-filter short-circuits the common
case (a model name, a number, a label) before any regex runs. And it never
raises — a redaction bug must not take down the request path.

## Structured logging

A span tells you THAT something failed; the error text lives in a log line. With
no shared key there is no way to get from one to the other. `setup_logging()`
emits one JSON object per line to stdout, and every record automatically carries
the same ids the spans carry.

```python
obskit.setup_logging(level="INFO")           # after init(), before serving
logging.getLogger("myapp").info("started")   # callsites do not change
```

```json
{"timestamp":"2026-07-30T13:19:12.459+00:00","level":"INFO","logger":"myapp",
 "message":"started","request_id":"3f31...","trace_id":"18686637887a03fe0322b15a9ac22006",
 "span_id":"94d72f178a82c2c3","tenant_id":"acme","user_id":"u1","session_id":"s1",
 "service":{"name":"my-api","namespace":"lab","version":"1.4.0","environment":"prod"}}
```

**Automatic means automatic.** `request_id`, `trace_id`, `span_id`, `tenant_id`,
`user_id` and `session_id` are read from the same contextvars `bind_request()`
and the span helpers already populate. Callsites keep writing plain
`log.info(...)`. Service identity comes from what you gave `init()`.

**The ids are the join.** `trace_id` is 32 lowercase hex, `span_id` is 16 — the
OTel canonical form, which is exactly what Langfuse displays. Copy either out of
a log line, paste it into the trace UI, land on the right span. `span_id` is the
**innermost** live span, not the request root, so a line logged inside
`with span("rerank")` points at the rerank span.

**Fields are omitted, not null, when unbound.** A line from startup or a
background task simply has no `request_id`. A null would imply one was expected.

**Redaction is the same one.** Messages, `extra=` fields, exception text and
stack traces all go through `redaction.scrub` — the function that scrubs span
attributes. There is no second implementation to drift.

**Setup is explicit and optional.** `init()` never configures logging. An
application that wants only traces has its logging left exactly as it found it;
reconfiguring a host application's root logger as a side effect of asking for
traces would be a hostile default.

**It attaches to the root logger**, so third-party library records become JSON
too. That is usually what you want and occasionally surprising — OpenTelemetry's
own warnings will appear in your log stream.

**uvicorn's access logs.** uvicorn configures only `uvicorn`, `uvicorn.error` and
`uvicorn.access`, and never touches the root logger — which is why a plain
`log.info()` in a uvicorn-hosted app goes nowhere by default until you call
`setup_logging()`. Those access lines **can** be brought into the same format:
pass `capture_uvicorn=True` and their handlers are removed and they propagate to
the JSON handler instead. It is off by default because it mutates loggers this
package does not own.

**Access lines DO carry the correlation set (correction, 0.3.0).** An earlier
version of this note claimed the opposite — that access lines "carry no
`request_id` or `trace_id`: uvicorn emits them after the response, outside the
request context." That is wrong, and production logs disprove it. uvicorn emits
the access record from *within the same ASGI task* that served the request, so
the request-scoped contextvars `bind_request()` set are still in scope when the
JSON formatter reads them. "After the response" is not "outside the context": the
task's contextvars are not torn down when the response is sent. A verified line
for a streaming `POST /api/chat`:

```json
{"logger":"uvicorn.access","message":"127.0.0.1:36386 - \"POST /api/chat HTTP/1.1\" 200",
 "request_id":"227b7c789a51485c91326c394ada6602",
 "trace_id":"f6f14e647f9ded296baf331a9ae1309c","span_id":"2257143ec595250d",
 "user_id":"6a3f...","session_id":"6a6b...","service":{"name":"legion-chat-backend",...}}
```

This holds for **both** streaming and non-streaming responses — the streaming
case is the one the old note specifically got wrong, because a streamed body
finishes long after the endpoint returns yet still runs in the same task. The
one access line that carries *none* of the correlation fields is a request that
never bound identity — a health check, an unauthenticated probe. That is correct:
there was no request identity to carry, so only `service` appears (verified: the
`GET /api/health` access line has no `request_id`/`trace_id`).

Options: `level`, `stream` (defaults to stdout), `capture_uvicorn`,
`static_fields`, `include_source`, `replace_existing`. Returns a status dict and
never raises — a logging misconfiguration must not stop a service starting.

Logs go to **stdout only**. Shipping them over OTLP to a collector is
deliberately out of scope.

## Metrics

A trace tells you what one request did; a metric tells you what a thousand
requests did in aggregate — the rate, the latency distribution, the error and
degradation fractions — without keeping a row per request. Metrics export over
OTLP to the **same Collector** as traces (the `/v1/metrics` path), which
re-exposes them for Prometheus to scrape.

```python
obskit.setup_metrics()                 # after init(); reuses its endpoint + Resource
```

That is the whole adoption cost. `setup_metrics()` derives the metrics endpoint
from the traces endpoint `init()` was given, reuses the exact `Resource` (so
metrics and traces share `service.instance.id` and correlate), and then the span
helpers you already call record the matching metric on close. **No new
callsites**: a `generation()` records the model-call metrics, a `RETRIEVER` span
records retrieval duration, and `end_root_span()` records the request metric.

### The metric set

Five instruments cover the ten measurements in the brief. **All five are
histograms**, and that is a deliberate choice, not laziness:

| Measurement(s) | Instrument name | Type | Unit | Standard? |
|---|---|---|---|---|
| request count · duration · error count · degraded-turn count | `lab.request.duration` | Histogram | `s` | `lab.` (no standard) |
| model-call count · duration | `gen_ai.client.operation.duration` | Histogram | `s` | **GenAI 1.37.0** |
| input tokens · output tokens | `gen_ai.client.token.usage` | Histogram | `{token}` | **GenAI 1.37.0** |
| time to first token | `gen_ai.client.operation.time_to_first_chunk` | Histogram | `s` | **GenAI 1.37.0** |
| retrieval duration | `lab.retrieval.duration` | Histogram | `s` | `lab.` (no standard) |

**Why histograms and not counters.** A histogram already carries a monotonic
`count` and a `sum`. So request count *is* `lab_request_duration_count`, model-call
count *is* the operation-duration count, and total tokens *is* the token-usage
`sum`. Emitting separate counters would double the series for information the
histogram already exposes. **Error count and degraded-turn count** are likewise
not separate instruments — they are the request histogram's count sliced by the
`lab.outcome` label (`ok` / `error` / `degraded`). This follows the OTel HTTP
semantic convention, where request rate and error rate both derive from
`http.server.request.duration` rather than from dedicated counters. No
UpDownCounter is used: none of the ten measurements is a gauge-like quantity that
rises and falls (there is no "in-flight requests" in the requested set).

**Standard names.** Where the OTel GenAI semantic conventions (pinned at
`SEMCONV_VERSION = 1.37.0`) already define a metric, that exact name is used
rather than a `lab.` invention — `gen_ai.client.operation.duration`,
`gen_ai.client.token.usage`, `gen_ai.client.operation.time_to_first_chunk` (the
*client* time-to-first-chunk, because the application is the client of the model
server). Only the two measurements with no standard — a generic application
request and a retrieval — get the `lab.` prefix. Naming follows the OTel metric
convention: dotted, lowercase, no unit in the name (the unit rides on the
instrument), matching the shape of stable `http.server.request.duration`. These
metric-name literals are pinned and re-declared exactly like the `gen_ai.*`
attribute strings, and `verify_against_upstream()` now checks them too.

### Labels: bounded only, enforced in code

**Every distinct label-value combination is a separate time series.** So the
label policy is the most important part of this design, and it is enforced by
`metrics._labels()` — not by documentation you have to remember.

**Allowed** (all bounded / closed sets): service identity (`service.*`,
`deployment.*`, from the Resource), `lab.route`, `lab.outcome`,
`gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.operation.name`,
`gen_ai.token.type`, and — opt-in only — `lab.tenant.id`.

**Refused** (high-cardinality identifiers): request id, trace id, span id, user
id, session id, conversation id, raw query / message text. These can *never*
become labels. How that is guaranteed, in layers:

1. **No free-form attribute parameter exists.** The recording hooks take no
   `**attributes` dict, so there is no parameter through which a request id could
   arrive. The bounded facts a metric labels by are snapshotted onto the span
   from the same `set_*` calls the app already makes — and only a fixed handful
   of keys (route, model, operation, provider) are snapshotted; the identity
   contextvars are never read into the metric path.
2. **Allow-list.** `_labels()` keeps only keys in a fixed `frozenset`. Anything
   else is dropped before it reaches the SDK.
3. **Deny-list.** The known-dangerous keys are *also* explicitly dropped, so even
   a future refactor that routed one in would fail closed. (Redundant with the
   allow-list by design — redundancy is the point.)
4. **Per-label cardinality cap.** Even an allowed label whose value set is
   bounded-in-practice-but-not-in-theory (model, route, provider, tenant) is
   capped: once a label has `value_cardinality_cap` distinct values (default 50),
   every further *new* value folds to `__other__`, so no label can silently grow
   the series count without bound.
5. **Redaction.** String label values pass through the same `redaction.scrub_text`
   traces and logs use — one implementation, no drift.

**Tenant is opt-in and capped.** `lab.tenant.id` is bounded today but may not stay
so, so it is **off by default**. Turn it on with
`setup_metrics(tenant_label=True, tenant_allowlist=[...])`: a tenant outside the
allow-list folds to `"other"` (never the raw id), and the cardinality cap applies
on top. Traces still carry per-request tenant detail regardless.

**Worst-case series count.** With caps saturated (route 50 · model 50 · provider
50 · 4 outcomes · 2 token types) and the OTel default ~16 histogram buckets plus
`_sum`/`_count`, the five instruments come to **≈ 58,000 series per process**.
The dominant term is the model-call duration histogram (`operation × model ×
provider × outcome × buckets`). Realistically — ~6 routes, ~10 models, one
provider, one process — it is **≈ 5,500 series**. Estimated as the product of
label-value counts per instrument, times per-histogram bucket series, summed over
the five instruments. One multiplier is outside this package: the Collector's
Prometheus exporter has `resource_to_telemetry_conversion` on, which promotes
every Resource attribute (including the per-process `service.instance.id`, a fresh
UUID each restart) to a label — so each restart starts a new series family that
expires after the Collector's `metric_expiration` (5m). Bounded, but real.

### Export health in `status()`

`obskit.metrics_status()` (alias for `obskit.metrics.status()`) reports not just
the startup decision but whether export is **succeeding now**: `exports_succeeded`,
`exports_failed`, `consecutive_failures`, `last_success`, `last_failure`,
`last_error`. The OTLP metric exporter does not expose any of this itself — so
rather than invent numbers, `setup_metrics` wraps the real exporter and records
the result of each `export()` call. When metrics run against an in-memory reader
(tests) there is no push exporter to observe, and the health fields are `None`
with `health_source: "unavailable ..."` — stated, not faked.

### Fail-soft and optional, like everything else

`init()` does **not** build a meter provider and never will — an application that
wants only traces gets none (verified by a test). Every recording path swallows
its own exceptions, so a label that fails to build or a dead Collector costs you
the metric, never the request. `setup_metrics()` returns a status dict rather than
raising.

## Attribute namespaces

| Prefix | Owner | Renameable |
|---|---|---|
| `lab.*` | this package / the application | yes — the only one |
| `gen_ai.*` | OTel GenAI semantic conventions, pinned to `SEMCONV_VERSION = 1.37.0` | no |
| `langfuse.*` | Langfuse OTLP ingestion contract (mirrored from langfuse 3.15.0) | no |
| `user.id`, `session.id`, `service.*`, `deployment.*` | standard OTel | no |

The `gen_ai.*` strings are re-declared as literals rather than imported, because
upstream keeps them under `opentelemetry.semconv._incubating.attributes` — a
private path that is free to move or vanish in a patch release, which would
otherwise break tracing at import time. `verify_against_upstream()` re-checks the
pinned literals against whatever is installed and returns the drift, so a
mismatch is detectable instead of silent. An empty dict means no drift; a missing
incubating module is tolerated and also returns empty, which is exactly the
failure the literals defend against.

## Multi-tenancy

`bind_request(tenant_id=...)` records `lab.tenant.id` on every span in the
request, including the root.

It rides the **existing identity contextvar mechanism**, not the parenting one.
Parenting lives in `_parent_span_var` / `_root_span_var` and is resolved
explicitly by `_parent_context()` precisely because a `.set()` inside an async
generator is not visible to the caller. `tenant_id` has no such hazard: it is an
immutable string bound once per request at the outermost frame and only ever
read from deeper frames, exactly like the request, user and session ids. So it
inherits the existing guarantee without adding to the fragile part.

## Working rules

These exist so the shared vocabulary does not drift once several applications
consume it. Each rule says whether it is **enforced by code** (and by what) or
whether it is **convention** that nothing checks — the distinction matters,
because an unenforced rule is only as good as the reviewer.

### 1. Depend on a pinned tag, never a branch

```bash
pip install "observability-framework @ git+https://github.com/looking4manish/observability-framework.git@v0.1.0"
```

Or in `requirements.txt`:

```
observability-framework @ git+https://github.com/looking4manish/observability-framework.git@v0.1.0
```

Tracking `@main` means an attribute rename lands in your application the next
time you rebuild an image, with no diff in your repo to explain why your
dashboards went empty. A tag is immutable; a branch is not.

*Convention. Nothing in this package can check how you installed it.*

### 2. Renaming an attribute is a MAJOR version bump

Say plainly what happens when you rename one: **nothing fails.** There is no
import error, no exception, no failed test in the consuming application. Spans
keep being emitted, the collector keeps accepting them, and the application
behaves identically. What breaks is comparison — every dashboard, saved filter
and alert keyed on the old name silently matches nothing, and traces from before
and after the change can no longer be queried together.

Concretely: `lab.retrieval.order_tau` becoming `lab.retrieval.tau` would leave a
chart of retrieval ordering flat-lining at "no data" from the deploy onward,
with a healthy application underneath and no error anywhere to point at it.

That is why it is a major bump rather than a minor one: the cost is invisible at
the point of change and is paid later by whoever is trying to read a trace.

*Convention. Nothing enforces it — see rule 9 for the honest gap here.*

### 3. Never hardcode an attribute string at a callsite

Always reference the module:

```python
from obskit import App
sp.set_attribute(App.RERANK_BACKEND, "unreachable")   # yes
sp.set_attribute("lab.rerank.backend", "unreachable") # no
```

A split vocabulary costs more than it looks. Once one callsite spells it
`lab.rerank.backend` and another `lab.reranker.backend`, both are "working" —
spans emit, no test fails — and you now have two half-populated series that each
look like intermittent instrumentation. Finding it means grepping strings rather
than following a symbol, and renaming becomes a text search across every
consuming repo instead of one edit in `semconv.py`.

The whole point of `App` is that the rename in rule 2 is a single-file change.
A hardcoded string opts that callsite out of it.

*Convention. There is no linter config and no test asserting callsite
discipline; `tests/test_semconv.py` checks the `App` class values, not how
callers spell them.*

### 4. Do not rename anything owned by a standard

Only `lab.*` is ours. `gen_ai.*` belongs to the OpenTelemetry GenAI semantic
conventions and `service.*` / `deployment.*` are standard OTel resource
attributes. `langfuse.*`, `user.id` and `session.id` are the ingestion contract.
Renaming any of those makes the traces unreadable to every tool that speaks the
standard — which is the entire reason for using it.

The `gen_ai.*` strings are pinned at `SEMCONV_VERSION = "1.37.0"` and re-declared
as literals in `semconv.py` rather than imported, because upstream keeps them
under `opentelemetry.semconv._incubating.attributes` — a private path free to
move or vanish in a patch release, which would otherwise break tracing at import
time.

`semconv.verify_against_upstream()` compares the 19 pinned `gen_ai.*` attribute
literals — **plus, as of 0.3.0, the 3 pinned GenAI metric-name literals**
(`gen_ai.client.operation.duration`, `gen_ai.client.token.usage`,
`gen_ai.client.operation.time_to_first_chunk`, which live in a separate
incubating module and drift for the same reason) — against whatever OTel is
installed, and returns `{constant: (ours, theirs)}` for anything that drifted.
Metric drift is keyed `metric:<NAME>` so it is distinguishable from attribute
drift. Empty dict means clean.

*Enforced in part.* `verify_against_upstream()` in `src/obskit/semconv.py` does
the comparison, and `tests/test_semconv.py` proves both that it is currently
clean and that it actually fires when a literal is changed. **But nothing calls
it automatically** — not at import, not in `status()`. It is a check you have to
run. See rule 9.

### 5. Service identity is required at init, no defaults

`init()` raises `ServiceIdentityError` if `service_name`, `service_namespace` or
`deployment_environment` is missing or blank, and it validates them *before* the
`enabled_flag` short-circuit so a misconfigured deployment surfaces even in an
environment where tracing is switched off.

There is no default on purpose. A missing `service.name` does not fail visibly —
OTel falls back to `unknown_service`, every application on the collector merges
into one indistinguishable stream, and nobody notices until they try to filter by
service months later. Failing at startup is the cheaper failure.

*Enforced by code:* `_require()` and `init()` in `src/obskit/tracing.py`;
covered by `tests/test_tracing.py::test_init_requires_service_identity`,
`::test_init_rejects_blank_identity` and
`::test_identity_validated_even_when_disabled`.

### 6. Fail-soft: instrumentation must never break the application it observes

Every public function swallows its own exceptions and returns a no-op handle.
When tracing is off or span creation fails, callers get `NULL_SPAN`, which
absorbs every method call and returns itself — so
`sp.set_attribute(...).set_degraded(...)` is safe with no branching at the
callsite. A dead collector, a missing SDK or an unserialisable attribute costs
you the trace, never the request.

`setup_logging()` follows the same rule: it returns `{"status": "error", ...}`
rather than raising, and the formatter falls back to a minimal line if JSON
encoding fails, so a bad log record cannot take down the request path.

As of 0.3.0 `setup_metrics()` and the metric-recording hooks follow it too: setup
returns a status dict, and every `record()` path swallows its own exceptions, so a
label that fails to build or a Collector that is down costs the metric, never the
request.

*Enforced by code, with one deliberate exception.* There is exactly one `raise`
in the entire package — `ServiceIdentityError` in `init()` (rule 5). That is
startup-time configuration validation, not request-path instrumentation. Once
`init()` has returned, nothing in this package raises — traces, logs or metrics.

### 7. Redaction happens at the choke point

Credential scrubbing runs inside `_Span.set_attribute` and `_serialize`, not at
callsites. There is exactly one line in the package that writes an attribute to
an OTel span, and it scrubs on the way through:

```python
self._span.set_attribute(key, scrub(_attr_value(value)))
```

`set_attributes()` delegates to `set_attribute()`, and `set_input`/`set_output`/
`set_metadata`/`set_usage` all route through the same two functions, so there is
no path to an attribute that bypasses redaction. That is the point: a per-callsite
`scrub()` helper would be correct everywhere it was called and useless the first
time someone forgot.

As of 0.2.0 the same applies to logs: `logs.JsonFormatter` scrubs the message,
`extra=` fields, exception text and stack traces through the same `scrub()`. As
of 0.3.0 it applies to metric label values too — `metrics._bounded()` runs each
value through the same `scrub_text()`. Three output paths, one redaction
implementation. (Metric labels are bounded closed sets, so a secret reaching one
would be a bug elsewhere; scrubbing them anyway keeps the single-choke-point rule
intact rather than making metrics the one exception.)

*Enforced by code:* `src/obskit/redaction.py` applied at
`src/obskit/tracing.py` (`_Span.set_attribute`, `_serialize`), at
`src/obskit/logs.py` (`JsonFormatter._build`) and at
`src/obskit/metrics.py` (`_bounded`); covered by
`tests/test_redaction.py`,
`tests/test_tracing.py::test_redaction_applied_in_attribute_path` and
`tests/test_logs.py::test_redaction_uses_the_shared_module`.

### 8. Declare your dependencies explicitly

Import it, declare it. Do not rely on another package dragging it in.

The concrete case, from this extraction: Legion Chat's `backend/app/tracing.py`
and `otel_semconv.py` import `opentelemetry` directly at eight sites — `trace`,
`sdk.resources.Resource`, `sdk.trace.TracerProvider`,
`sdk.trace.export.BatchSpanProcessor`, the OTLP HTTP exporter,
`trace.Status/StatusCode` and the incubating semconv module. Its
`requirements.txt` pins exactly one relevant line: `langfuse>=3,<4`. There is no
`opentelemetry` line at all. On that host `pip show opentelemetry-sdk` reports
`Required-by: langfuse` — the entire OTel stack is present only because Langfuse
happens to depend on it.

That works until Langfuse loosens, vendors or drops an OTel dependency in a minor
release the `>=3,<4` range already permits. Then tracing breaks at import time,
in a service whose own requirements file never mentioned the package that
vanished.

This repo declares `opentelemetry-api` and `opentelemetry-sdk` directly in
`pyproject.toml`, with the OTLP exporter as an optional `[otlp]` extra since it
is only needed to actually ship spans.

*Enforced by code for this package* (`pyproject.toml`); *convention for
consumers* — nothing here can audit your requirements file.

### 9. Never leave work one step short of verification

If something is instrumented but unverified, or built but not wired up, make
that visible in a health or status surface. Work that is one step short of done
looks identical to work that is finished, and the gap is discovered by whoever
next depends on it.

`status()` exists for exactly this and returns `initialized`, `enabled`,
`endpoint`, `service`, `service_namespace`, `environment`, `semconv_version` and
`error`. Surface it on your health endpoint. The specific hole it closes: the
startup log line reporting tracing state is swallowed by uvicorn's logging
config, so without `status()` the only way to tell a live exporter from a
silently-disabled one is to send a request and go look in the collector.

As of 0.3.0 `metrics_status()` extends this to metrics and goes one step further
than the tracing `status()`: it reports not just the startup decision but **live
export health** — `exports_succeeded`, `exports_failed`, `consecutive_failures`,
`last_success`, `last_failure`, `last_error` — so a metrics pipeline that
initialised fine but is now failing to reach the Collector is visible without
guessing. Where the SDK genuinely exposes nothing (an in-memory reader has no
push exporter), the field is `None` with a `health_source` note rather than a
fabricated zero.

**This package still partly fails its own rule for the drift check, and it is
stated here rather than hidden.** `verify_against_upstream()` (rule 4) is written,
exported and tested — and as of 0.3.0 covers the metric names too — but nothing
calls it automatically: not at import, not in `status()`, which reports
`semconv_version` but never the drift result. So a consumer sees `1.37.0` and
reasonably assumes the pin has been checked, when it has only been declared.
Until that is wired up, run the check yourself in CI:

```python
from obskit import verify_against_upstream
drift = verify_against_upstream()
assert not drift, f"semconv drift: {drift}"
```

## Adopting this package

What a new application must do, in order. Based on `init()`'s actual signature,
not on intent.

1. **Pin the dependency at a tag** (rule 1). Nothing else in this list matters if
   the version can move under you.

2. **Call `init()` exactly once at startup, before any span is opened.** The
   tracer provider is process-global, so enablement is a start-up decision and
   changing it needs a restart.

   Required, no defaults: `enabled_flag`, `service_name`, `service_namespace`,
   `deployment_environment`, `endpoint`. For a Langfuse target,
   `langfuse_endpoint(host, public_key, secret_key)` returns the `endpoint` and
   `headers` kwargs:

   ```python
   import obskit

   status = obskit.init(
       enabled_flag=cfg.tracing_enabled,
       service_name="my-api",
       service_namespace="lab",
       deployment_environment="prod",
       service_version=os.environ.get("RELEASE", "dev"),
       **obskit.langfuse_endpoint(host, public_key, secret_key),
   )
   ```

   `init()` reads no settings object, no config module and no environment
   variable of its own — configuration is yours to source and pass in. Optional:
   `service_version`, `tracer_name`, `resource_attributes`,
   `max_export_batch_size`, `schedule_delay_millis`, and `span_processor` (pass a
   `SimpleSpanProcessor` over an in-memory exporter in tests and no network is
   touched).

3. **Call `setup_logging()` if you want structured logs.** Optional, and
   deliberately not done for you by `init()`. Call it after `init()` so records
   carry service identity, and before serving so startup lines are captured:

   ```python
   obskit.setup_logging(level="INFO", capture_uvicorn=True)
   ```

   Skip it entirely and your logging is untouched.

4. **Call `setup_metrics()` if you want metrics.** Optional, separate from
   `init()` for the same reason `setup_logging()` is: `init()` never builds a
   meter provider. Call it once after `init()` — it reuses `init()`'s resource
   and derives the metrics endpoint (`/v1/metrics`) from the trace endpoint, so
   no second endpoint to configure:

   ```python
   obskit.setup_metrics(enabled_flag=cfg.metrics_enabled)
   ```

   Nothing else changes at your callsites: the instruments are recorded from
   inside the span lifecycle you already drive (`start_root_span`/`end_root_span`,
   `generation()`, retriever spans), so a span you already open becomes a metric
   the moment metrics are on, and records nothing until they are. Only bounded
   labels are emitted — see [Metrics](#metrics) for the label rules and the
   tenant opt-in (`tenant_label=`, `tenant_allowlist=`). Skip this call and no
   meter provider exists and no metric is recorded.

5. **Expose `status()` and `metrics_status()` on your health endpoint** (rule 9),
   so a silently disabled trace exporter — or a metrics pipeline that initialised
   but is now failing to reach the Collector — is visible without sending a
   request and going to look.

6. **Bind identity once per request, at the outermost frame.**

   ```python
   request_id = obskit.bind_request(user_id=uid, session_id=conv_id,
                                    tenant_id=tenant)
   ```

   All four arguments are optional; `request_id` is generated if not supplied and
   returned to you. `tenant_id` lands as `lab.tenant.id` on every span in the
   request.

7. **Open a root span per unit of work and close it in a `finally`.**
   `start_root_span()` is deliberately not a context manager, because a streaming
   response has to outlive the handler that created it.

   ```python
   root = obskit.start_root_span("chat", input=user_message, tags=["mode:chat"])
   try:
       ...
   finally:
       obskit.end_root_span(root, output=answer)
   ```

   If your work continues inside an async generator (a streaming response),
   call `obskit.adopt_root(root)` at the top of the generator. FastAPI runs the
   endpoint and its `StreamingResponse` generator in the same task today, so the
   contextvars carry over — but that is an implementation detail of the ASGI
   stack, and any middleware that runs the generator in a fresh task would
   silently orphan every child span.

8. **Instrument with the span helpers, referencing `App.*` never string
   literals** (rule 3): `span()`, `generation()`, `embedding()`, `tool_span()`.
   Pass `provider=` explicitly on `generation()` and `embedding()` — there is no
   default, and when omitted `gen_ai.provider.name` is simply not set rather than
   set to something untrue.

9. **Call `shutdown()` on process stop.** Spans are exported by a background
   thread; without it a restart drops whatever was still queued. The one
   `shutdown()` also flushes and stops the meter provider when metrics are on
   (the periodic metric reader flushes on the same process-stop path), so there
   is a single shutdown to call, not two.

10. **Run the drift check in CI** (rule 9), since nothing runs it for you — as of
    0.3.0 it covers the three GenAI metric names too.

## Install

```bash
pip install observability-framework          # api + sdk
pip install "observability-framework[otlp]"  # + the OTLP/HTTP exporter
```

Requires Python 3.10+. The OTLP exporter is optional: tests use an in-memory
exporter, and an application in a collector-less environment does not need it.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

No network. Span assertions run against `InMemorySpanExporter` via a
`SimpleSpanProcessor` passed into `init(span_processor=...)`.
