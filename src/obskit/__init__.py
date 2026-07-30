"""obskit — OpenTelemetry span-tree tracing for LLM applications, exported to Langfuse.

Extracted from the Legion Chat application so other applications can share the
same vocabulary and the same tracing behaviour.

Three things it gives you that raw OTel does not:

  * a Langfuse-shaped span tree (generation / embedding / tool / retriever /
    agent observation types) with token usage and model parameters;
  * explicit parenting that survives async generators, so deep callsites need
    no arguments threaded through them;
  * soft-degradation marking — a dependency that failed over silently still
    shows up as WARNING with `lab.degraded=true`, on the leaf AND on the root.

Quick start:

    import obskit
    obskit.init(
        enabled_flag=True,
        service_name="my-api",
        service_namespace="lab",
        deployment_environment="prod",
        **obskit.langfuse_endpoint(host, public_key, secret_key),
    )

Service identity is required. There are no defaults — see ServiceIdentityError.
"""
from .semconv import (SEMCONV_SCHEMA_URL, SEMCONV_VERSION, App, GenAI,
                      GenAIOperation, Langfuse, ObservationLevel, ObservationType,
                      verify_against_upstream)
from .logs import JsonFormatter, get_logger, setup_logging
from .redaction import scrub, scrub_text
from .tracing import (NULL_SPAN, ServiceIdentityError, adopt_root, bind_request,
                      current_span_ids, current_trace_id, degradations, embedding, enabled,
                      end_root_span, flush, generation, init, init_error,
                      mark_degraded_from_request, new_request_id, record_degradation,
                      record_generation, redact_messages, request_id, reset_for_tests,
                      service_identity, session_id, set_context_state, shutdown, span,
                      start_root_span, status, tenant_id, tool_span, user_id)

__version__ = "0.2.0"


def langfuse_endpoint(host: str, public_key: str, secret_key: str) -> dict:
    """Build the `endpoint` + `headers` kwargs for init() from Langfuse keys.

    Langfuse's OTLP ingestion sits at /api/public/otel/v1/traces and takes HTTP
    Basic of public_key:secret_key. Kept as a helper rather than baked into
    init() so the package stays usable against any OTLP collector.
    """
    import base64

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return {
        "endpoint": f"{host.rstrip('/')}/api/public/otel/v1/traces",
        "headers": {"Authorization": f"Basic {auth}",
                    "x-langfuse-public-key": public_key},
    }


__all__ = [
    "__version__", "langfuse_endpoint",
    # semconv
    "App", "GenAI", "GenAIOperation", "Langfuse", "ObservationLevel",
    "ObservationType", "SEMCONV_VERSION", "SEMCONV_SCHEMA_URL",
    "verify_against_upstream",
    # redaction
    "scrub", "scrub_text",
    # structured logging (opt-in; init() never configures logging for you)
    "setup_logging", "JsonFormatter", "get_logger",
    # lifecycle
    "init", "shutdown", "flush", "status", "enabled", "init_error",
    "ServiceIdentityError", "reset_for_tests",
    # request identity
    "bind_request", "new_request_id", "request_id", "user_id", "session_id",
    "tenant_id",
    # spans
    "span", "generation", "embedding", "tool_span", "start_root_span",
    "adopt_root", "end_root_span", "current_trace_id", "current_span_ids",
    "service_identity", "NULL_SPAN",
    # degradation
    "record_degradation", "degradations", "mark_degraded_from_request",
    "set_context_state",
    # helpers / back-compat
    "redact_messages", "record_generation",
]
