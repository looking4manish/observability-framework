# observability-framework (`obskit`)

OpenTelemetry span-tree tracing for LLM applications, exported to Langfuse over
OTLP/HTTP.

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

`semconv.verify_against_upstream()` compares all 19 pinned literals against
whatever OTel is installed and returns `{constant: (ours, theirs)}` for anything
that drifted. Empty dict means clean.

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

*Enforced by code, with one deliberate exception.* There is exactly one `raise`
in the entire package — `ServiceIdentityError` in `init()` (rule 5). That is
startup-time configuration validation, not request-path instrumentation. Once
`init()` has returned, nothing in this package raises.

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

*Enforced by code:* `src/obskit/redaction.py` applied at
`src/obskit/tracing.py` (`_Span.set_attribute`, `_serialize`); covered by
`tests/test_redaction.py` and
`tests/test_tracing.py::test_redaction_applied_in_attribute_path`.

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

**This package currently fails its own rule, and it is stated here rather than
hidden.** `verify_against_upstream()` (rule 4) is written, exported and tested,
but nothing calls it — not at import, not in `status()`, which reports
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

3. **Expose `status()` on your health endpoint** (rule 9), so a silently
   disabled exporter is visible without sending a request and going to look.

4. **Bind identity once per request, at the outermost frame.**

   ```python
   request_id = obskit.bind_request(user_id=uid, session_id=conv_id,
                                    tenant_id=tenant)
   ```

   All four arguments are optional; `request_id` is generated if not supplied and
   returned to you. `tenant_id` lands as `lab.tenant.id` on every span in the
   request.

5. **Open a root span per unit of work and close it in a `finally`.**
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

6. **Instrument with the span helpers, referencing `App.*` never string
   literals** (rule 3): `span()`, `generation()`, `embedding()`, `tool_span()`.
   Pass `provider=` explicitly on `generation()` and `embedding()` — there is no
   default, and when omitted `gen_ai.provider.name` is simply not set rather than
   set to something untrue.

7. **Call `shutdown()` on process stop.** Spans are exported by a background
   thread; without it a restart drops whatever was still queued.

8. **Run the drift check in CI** (rule 9), since nothing runs it for you.

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
