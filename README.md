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
