"""One place for every OpenTelemetry / Langfuse span attribute string.

Part of `obskit` (distribution: observability-framework). Extracted verbatim from
the Legion Chat application; the only change to the vocabulary itself is that the
application namespace moved from `legion.` to `lab.`.

Two vocabularies meet on the same span and both are pinned here so no attribute
name is ever typed inline at a callsite:

1. **OpenTelemetry GenAI semantic conventions** (`gen_ai.*`) — the portable
   vocabulary. Any OTel-aware backend understands these. They live in the
   upstream package under `opentelemetry.semconv._incubating.attributes`, and
   that leading underscore is the whole reason we re-declare the strings as
   literals here: GenAI semconv is still *incubating*, so upstream is free to
   move, rename, or delete that module in a patch release. Importing from it
   directly would make an SDK bump able to break tracing at import time. The
   literals below are frozen at SEMCONV_VERSION; `verify_against_upstream()`
   re-checks them against whatever is installed so drift is detectable instead
   of silent.

2. **Langfuse OTel attributes** (`langfuse.*`) — how Langfuse's OTLP ingestion
   decides what an observation *is*. A span with no `langfuse.observation.type`
   renders as a plain span with no input/output/model/usage, so these are what
   make a generation show up as a generation. Mirrored from
   `langfuse._client.attributes.LangfuseOtelSpanAttributes` (langfuse 3.15.0)
   for the same reason: it is a private module path.

Nothing here imports OpenTelemetry, so this module is safe to import from
anywhere regardless of whether the SDK is installed.
"""

# Pinned OTel semantic-convention release these strings were taken from.
# Bump deliberately, together with verify_against_upstream() coming back clean.
SEMCONV_VERSION = "1.37.0"
SEMCONV_SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"

# Langfuse SDK release the langfuse.* strings were mirrored from.
LANGFUSE_ATTR_SOURCE_VERSION = "3.15.0"


class GenAI:
    """OTel GenAI semantic conventions (incubating, pinned to SEMCONV_VERSION)."""

    OPERATION_NAME = "gen_ai.operation.name"
    PROVIDER_NAME = "gen_ai.provider.name"
    SYSTEM = "gen_ai.system"

    REQUEST_MODEL = "gen_ai.request.model"
    REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    REQUEST_TOP_P = "gen_ai.request.top_p"
    REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    REQUEST_STREAM = "gen_ai.request.stream"

    RESPONSE_MODEL = "gen_ai.response.model"
    RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

    CONVERSATION_ID = "gen_ai.conversation.id"
    AGENT_NAME = "gen_ai.agent.name"

    TOOL_NAME = "gen_ai.tool.name"
    TOOL_TYPE = "gen_ai.tool.type"
    TOOL_CALL_ID = "gen_ai.tool.call.id"
    TOOL_DESCRIPTION = "gen_ai.tool.description"

    EMBEDDINGS_DIMENSION_COUNT = "gen_ai.embeddings.dimension.count"


class GenAIOperation:
    """Values for GenAI.OPERATION_NAME."""

    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    EXECUTE_TOOL = "execute_tool"
    INVOKE_AGENT = "invoke_agent"


class GenAIMetric:
    """OTel GenAI *metric* names (incubating, pinned to SEMCONV_VERSION).

    Re-declared as frozen literals for exactly the reason the gen_ai.* attribute
    strings are (see module docstring): these live in
    `opentelemetry.semconv._incubating.metrics.gen_ai_metrics`, an underscored,
    still-incubating module upstream is free to move or rename in a patch release.
    Importing it directly would let an SDK bump break metrics at import time.
    `verify_against_upstream()` re-checks these against whatever is installed.

    Instrument types and units are fixed by the convention: all three are
    Histograms; duration/TTFT in seconds, token usage in `{token}`.
    """

    OPERATION_DURATION = "gen_ai.client.operation.duration"     # Histogram, s
    TOKEN_USAGE = "gen_ai.client.token.usage"                   # Histogram, {token}
    TIME_TO_FIRST_CHUNK = "gen_ai.client.operation.time_to_first_chunk"  # Histogram, s


class LabMetric:
    """Application metric names with no OTel standard. `lab.` prefix, same rule as
    the App.* attributes: the only prefix an adopting application may rename.

    OTel metric naming convention followed: dotted namespace, lowercase, no unit
    in the name (the unit rides on the instrument), singular noun for what is
    measured — matching the shape of the stable `http.server.request.duration`.
    """

    REQUEST_DURATION = "lab.request.duration"       # Histogram, s
    RETRIEVAL_DURATION = "lab.retrieval.duration"   # Histogram, s


class MetricLabel:
    """The ONLY keys permitted as metric labels. Every one is a bounded/closed
    set — service identity, route, model, provider, operation, outcome, token
    type — never a per-request identifier. metrics._labels() enforces this."""

    ROUTE = "lab.route"          # app route classifier output (closed set)
    OUTCOME = "lab.outcome"      # ok | error | degraded | aborted
    TENANT = "lab.tenant.id"     # opt-in only, allow-listed + capped
    MODEL = "gen_ai.request.model"
    OPERATION = "gen_ai.operation.name"
    PROVIDER = "gen_ai.provider.name"
    TOKEN_TYPE = "gen_ai.token.type"  # input | output


class TokenType:
    """Values for MetricLabel.TOKEN_TYPE (gen_ai.token.type)."""

    INPUT = "input"
    OUTPUT = "output"


class Langfuse:
    """Langfuse OTLP ingestion attributes (mirrored, see module docstring)."""

    TRACE_NAME = "langfuse.trace.name"
    TRACE_USER_ID = "user.id"
    TRACE_SESSION_ID = "session.id"
    TRACE_TAGS = "langfuse.trace.tags"
    TRACE_INPUT = "langfuse.trace.input"
    TRACE_OUTPUT = "langfuse.trace.output"
    TRACE_METADATA = "langfuse.trace.metadata"

    OBSERVATION_TYPE = "langfuse.observation.type"
    OBSERVATION_INPUT = "langfuse.observation.input"
    OBSERVATION_OUTPUT = "langfuse.observation.output"
    OBSERVATION_LEVEL = "langfuse.observation.level"
    OBSERVATION_STATUS_MESSAGE = "langfuse.observation.status_message"
    OBSERVATION_METADATA = "langfuse.observation.metadata"
    OBSERVATION_MODEL = "langfuse.observation.model.name"
    OBSERVATION_MODEL_PARAMETERS = "langfuse.observation.model.parameters"
    OBSERVATION_USAGE_DETAILS = "langfuse.observation.usage_details"
    OBSERVATION_COMPLETION_START_TIME = "langfuse.observation.completion_start_time"

    ENVIRONMENT = "langfuse.environment"
    RELEASE = "langfuse.release"
    VERSION = "langfuse.version"


class ObservationType:
    """Valid values for Langfuse.OBSERVATION_TYPE (langfuse _client.constants)."""

    SPAN = "span"
    GENERATION = "generation"
    EMBEDDING = "embedding"
    AGENT = "agent"
    TOOL = "tool"
    CHAIN = "chain"
    RETRIEVER = "retriever"
    GUARDRAIL = "guardrail"
    EVALUATOR = "evaluator"


class ObservationLevel:
    DEBUG = "DEBUG"
    DEFAULT = "DEFAULT"
    WARNING = "WARNING"
    ERROR = "ERROR"


# Non-standard attributes applications add. Namespaced under `lab.` so they can
# never collide with a future real semconv attribute.
#
# `lab.` is the ONLY prefix that may be renamed when this package is adopted by a
# new application. gen_ai.* is defined by the OTel GenAI semantic conventions and
# is pinned to SEMCONV_VERSION (see verify_against_upstream); langfuse.* is the
# ingestion contract; user.id / session.id / service.* / deployment.* are standard
# OTel. None of those are ours to rename.
class App:
    # Multi-tenant deployments: which tenant this request belongs to. Bound per
    # request through the same contextvar mechanism as request/user/session ids,
    # so deep callsites need no argument threading. See tracing.bind_request.
    TENANT_ID = "lab.tenant.id"

    REQUEST_ID = "lab.request.id"
    ROUTE = "lab.route"
    AGENT_MODE = "lab.agent_mode"
    DEEP_MODE = "lab.deep_mode"
    WEB_SEARCH = "lab.web_search"
    INJECTED_MEMORY = "lab.injected_memory"
    HOT_MESSAGES = "lab.hot_messages"
    IMAGES = "lab.images"
    ABORTED = "lab.aborted"
    ERROR = "lab.error"

    # Soft degradation: the turn succeeded, but a dependency took a worse path.
    # One boolean across every subsystem so a single alert rule covers all of
    # them — you should not need to know that `lab.rerank.backend` exists to
    # notice the reranker died. See tracing._Span.set_degraded.
    DEGRADED = "lab.degraded"
    DEGRADED_REASON = "lab.degraded.reason"
    DEGRADED_COUNT = "lab.degraded.count"

    AGENT_STEP = "lab.agent.step"
    AGENT_TOOLS_OFFERED = "lab.agent.tools_offered"
    AGENT_TOOL_CALLS = "lab.agent.tool_calls"
    # Did the model reach for something it was not given? Injection 2's wrong-tool
    # answer was invisible because nothing compared attempted names against offered
    # ones. Counts both structured tool_calls and names the model emitted as plain
    # text (which is what it does when its tools vanish mid-conversation).
    AGENT_TOOLS_ATTEMPTED = "lab.agent.tools_attempted"
    AGENT_TOOLS_UNAVAILABLE = "lab.agent.tools_unavailable"

    MCP_SERVER = "lab.mcp.server"
    MCP_TRANSPORT = "lab.mcp.transport"
    MCP_OUTCOME = "lab.mcp.outcome"

    RETRIEVAL_QUERY = "lab.retrieval.query"
    RETRIEVAL_CANDIDATES = "lab.retrieval.candidates"
    RETRIEVAL_RETURNED = "lab.retrieval.returned"
    RETRIEVAL_RERANKED = "lab.retrieval.reranked"

    EMBED_INPUT_COUNT = "lab.embed.input_count"
    EMBED_BACKEND = "lab.embed.backend"

    RERANK_CANDIDATES = "lab.rerank.candidates"
    RERANK_TOP_K = "lab.rerank.top_k"
    RERANK_BACKEND = "lab.rerank.backend"
    RERANK_HOSTS_FAILED = "lab.rerank.hosts_failed"

    RETRIEVAL_TOP_SCORE = "lab.retrieval.top_score"
    RETRIEVAL_RERANK_BACKEND = "lab.retrieval.rerank_backend"
    # Origin of the injected chunks. `injected_memory` alone cannot tell you whether
    # an answer leaned on verbatim profile facts or on reconstructed history.
    RETRIEVAL_FROM_HISTORY = "lab.retrieval.from_history"   # episodic + raw
    RETRIEVAL_FROM_PROFILE = "lab.retrieval.from_profile"
    RETRIEVAL_FROM_FILES = "lab.retrieval.from_files"

    # --- context window ---------------------------------------------------
    # Utilisation is the early warning: recall degrades as the hot window fills,
    # and nothing previously computed prompt_tokens/num_ctx anywhere.
    CONTEXT_WINDOW = "lab.context.num_ctx"
    CONTEXT_PROMPT_UTILISATION = "lab.context.prompt_utilisation"
    CONTEXT_TOTAL_UTILISATION = "lab.context.total_utilisation"
    # Truncation state of the CONVERSATION, so a post-compression turn is
    # identifiable on its own instead of only by diffing against a control.
    CONTEXT_COMPRESSED_MESSAGES = "lab.context.compressed_messages"
    CONTEXT_HOT_TOKENS = "lab.context.hot_tokens"
    CONTEXT_TRUNCATED = "lab.context.truncated"
    # Trace id of the compression that last reshaped this conversation's context.
    # Span links cannot express this (see tracing.set_context_state).
    CONTEXT_COMPRESSED_BY_TRACE = "lab.context.compressed_by_trace"

    # --- episodic summary coverage ---------------------------------------
    # The episodic summary is OVERWRITTEN on each compression, so a conversation's
    # earlier arc survives only as vector chunks. These measure how much of the
    # compressed history the current summary still speaks to. Observational only —
    # deliberately not wired to any degradation threshold until calibrated.
    SUMMARY_COVERAGE = "lab.summary.coverage"
    SUMMARY_TURNS_CONSIDERED = "lab.summary.turns_considered"
    SUMMARY_MEAN_SIMILARITY = "lab.summary.mean_similarity"
    SUMMARY_MIN_SIMILARITY = "lab.summary.min_similarity"
    SUMMARY_GENERATION = "lab.summary.generation"   # how many times rolled forward
    # Cost of rolling the note forward instead of rewriting it: the prior note is now
    # part of the summarisation input, so this is what that input grew by.
    SUMMARY_PRIOR_TOKENS = "lab.summary.prior_tokens"
    SUMMARY_BUDGET_TOKENS = "lab.summary.budget_tokens"
    SUMMARY_OUTPUT_TOKENS = "lab.summary.output_tokens"
    SUMMARY_SUPERSEDED_CHUNKS = "lab.summary.superseded_chunks"

    # --- retrieved-set ordering -------------------------------------------
    # Normalised Kendall tau distance between the returned order and the
    # chronological order of the same chunks: 0.0 = chronological, 1.0 = reversed.
    # High tau is NOT a defect on its own — it only matters when the question is
    # sequence-dependent, which `order_query` gates. Recorded, never alerted on:
    # the threshold needs calibration against a labelled set first.
    RETRIEVAL_ORDER_TAU = "lab.retrieval.order_tau"
    RETRIEVAL_SPAN_MESSAGES = "lab.retrieval.span_messages"
    RETRIEVAL_CONTIGUOUS = "lab.retrieval.contiguous"
    RETRIEVAL_ORDER_QUERY = "lab.retrieval.order_query"

    # --- which chunks (retrieval identity) --------------------------------
    # Everything above says HOW MANY chunks came back, their KIND mix and their
    # SCORES. None of it says WHICH ones, so a bad answer cannot be traced back
    # to the specific retrieval that fed it. These close that gap: identifiers
    # only (never chunk text — the id round-trips to the full chunk in one DB
    # lookup), and bounded by payload size, not cardinality. `chunk_ids` and
    # `chunk_kinds` are index-aligned (position i describes the same returned
    # chunk) and inherently short (one entry per returned chunk, <= retrieve_k).
    # `source_message_ids` is the provenance union and is capped at the callsite;
    # the exact, uncapped count already lives in RETRIEVAL_SPAN_MESSAGES.
    RETRIEVAL_CHUNK_IDS = "lab.retrieval.chunk_ids"
    RETRIEVAL_CHUNK_KINDS = "lab.retrieval.chunk_kinds"
    RETRIEVAL_SOURCE_MESSAGE_IDS = "lab.retrieval.source_message_ids"

    # --- per-tier retrieval budget ----------------------------------------
    # A retriever that takes the top k on similarity alone lets a large tier crowd
    # out a small one: a handful of durable user facts, phrased generally, rank
    # below hundreds of conversation chunks that share the question's exact
    # vocabulary. That failure is invisible in every attribute above — candidates,
    # returned, reranked and top_score all read healthy while the facts the answer
    # needed were never in the returned set at all.
    #
    # These describe the returned set per TIER, which is the unit a budget is
    # expressed in. They do NOT replace RETRIEVAL_FROM_PROFILE / FROM_HISTORY /
    # FROM_FILES: those count by chunk KIND and are what existing dashboards are
    # keyed on. The two disagree deliberately — from_history sums episodic and raw,
    # which belong to different tiers.
    RETRIEVAL_TIER_PROFILE = "lab.retrieval.tier.profile"
    RETRIEVAL_TIER_EPISODIC = "lab.retrieval.tier.episodic"
    RETRIEVAL_TIER_SEMANTIC = "lab.retrieval.tier.semantic"
    # The budget actually in force for this retrieval, compactly rendered
    # ("profile=3,episodic=2,semantic=5,total=10"). Recorded per retrieval rather
    # than inferred from config, because it is live-tunable and a trace read a week
    # later has no other way to know what the rules were when it ran.
    RETRIEVAL_BUDGET = "lab.retrieval.budget"
    # Slots a tier did not fill, handed back to the general pool. This is the
    # attribute that shows the budget is not costing recall: for a user with no
    # profile facts it equals the profile allowance, and the returned set is
    # exactly what an unbudgeted retrieval would have produced.
    RETRIEVAL_BUDGET_BACKFILLED = "lab.retrieval.budget_backfilled"

    WEBSEARCH_PROVIDER = "lab.websearch.provider"
    WEBSEARCH_PROVIDER_REQUESTED = "lab.websearch.provider_requested"
    WEBSEARCH_RESULTS = "lab.websearch.results"

    MCP_SERVERS_CONNECTED = "lab.mcp.servers_connected"
    MCP_SERVERS_FAILED = "lab.mcp.servers_failed"

    COMPRESS_TURNS = "lab.compress.turns"
    COMPRESS_CHUNKS = "lab.compress.chunks"

    OLLAMA_HOST = "lab.ollama.host"
    OLLAMA_FALLBACK_USED = "lab.ollama.fallback_used"


def verify_against_upstream() -> dict[str, tuple[str, str]]:
    """Compare the pinned gen_ai.* literals with the installed OTel package.

    Returns {constant_name: (ours, theirs)} for every attribute that drifted;
    an empty dict means the pin still matches what is installed. Import errors
    are swallowed — the incubating module is allowed to not exist, which is
    exactly the failure mode these literals defend against.
    """
    try:
        from opentelemetry.semconv._incubating.attributes import (  # noqa: PLC0415
            gen_ai_attributes as up,
        )
    except Exception:  # noqa: BLE001
        return {}

    pairs = {
        "OPERATION_NAME": "GEN_AI_OPERATION_NAME",
        "PROVIDER_NAME": "GEN_AI_PROVIDER_NAME",
        "SYSTEM": "GEN_AI_SYSTEM",
        "REQUEST_MODEL": "GEN_AI_REQUEST_MODEL",
        "REQUEST_TEMPERATURE": "GEN_AI_REQUEST_TEMPERATURE",
        "REQUEST_TOP_P": "GEN_AI_REQUEST_TOP_P",
        "REQUEST_MAX_TOKENS": "GEN_AI_REQUEST_MAX_TOKENS",
        "REQUEST_STREAM": "GEN_AI_REQUEST_STREAM",
        "RESPONSE_MODEL": "GEN_AI_RESPONSE_MODEL",
        "RESPONSE_FINISH_REASONS": "GEN_AI_RESPONSE_FINISH_REASONS",
        "USAGE_INPUT_TOKENS": "GEN_AI_USAGE_INPUT_TOKENS",
        "USAGE_OUTPUT_TOKENS": "GEN_AI_USAGE_OUTPUT_TOKENS",
        "CONVERSATION_ID": "GEN_AI_CONVERSATION_ID",
        "AGENT_NAME": "GEN_AI_AGENT_NAME",
        "TOOL_NAME": "GEN_AI_TOOL_NAME",
        "TOOL_TYPE": "GEN_AI_TOOL_TYPE",
        "TOOL_CALL_ID": "GEN_AI_TOOL_CALL_ID",
        "TOOL_DESCRIPTION": "GEN_AI_TOOL_DESCRIPTION",
        "EMBEDDINGS_DIMENSION_COUNT": "GEN_AI_EMBEDDINGS_DIMENSION_COUNT",
    }
    drift: dict[str, tuple[str, str]] = {}
    for ours_name, theirs_name in pairs.items():
        ours = getattr(GenAI, ours_name)
        theirs = getattr(up, theirs_name, None)
        if theirs is not None and theirs != ours:
            drift[ours_name] = (ours, theirs)

    # GenAI *metric* names live in a separate incubating module and drift for the
    # same reason. Check them here too, keyed distinctly so a reader can tell an
    # attribute drift from a metric drift.
    try:
        from opentelemetry.semconv._incubating.metrics import (  # noqa: PLC0415
            gen_ai_metrics as upm,
        )
        metric_pairs = {
            "OPERATION_DURATION": "GEN_AI_CLIENT_OPERATION_DURATION",
            "TOKEN_USAGE": "GEN_AI_CLIENT_TOKEN_USAGE",
            "TIME_TO_FIRST_CHUNK": "GEN_AI_CLIENT_OPERATION_TIME_TO_FIRST_CHUNK",
        }
        for ours_name, theirs_name in metric_pairs.items():
            ours = getattr(GenAIMetric, ours_name)
            theirs = getattr(upm, theirs_name, None)
            if theirs is not None and theirs != ours:
                drift[f"metric:{ours_name}"] = (ours, theirs)
    except Exception:  # noqa: BLE001
        pass

    return drift
