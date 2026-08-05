"""The gen_ai.* pin and its drift check must survive the extraction intact."""
import obskit
from obskit import semconv


def test_semconv_version_pin_unchanged():
    assert semconv.SEMCONV_VERSION == "1.37.0"
    assert semconv.SEMCONV_SCHEMA_URL.endswith("/1.37.0")


def test_drift_check_is_clean_against_installed_otel():
    """Empty dict means the pinned literals still match what is installed."""
    assert semconv.verify_against_upstream() == {}


def test_drift_check_actually_fires(monkeypatch):
    """A clean result must mean 'no drift', not 'never compared anything'."""
    monkeypatch.setattr(semconv.GenAI, "REQUEST_MODEL", "gen_ai.request.MODEL_MOVED")
    drift = semconv.verify_against_upstream()
    assert "REQUEST_MODEL" in drift
    ours, theirs = drift["REQUEST_MODEL"]
    assert ours == "gen_ai.request.MODEL_MOVED"
    assert theirs == "gen_ai.request.model"


def test_drift_check_tolerates_missing_incubating_module(monkeypatch):
    """The incubating module is allowed to vanish — that is what the pin defends."""
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if "semconv._incubating" in name:
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert semconv.verify_against_upstream() == {}


def test_genai_names_were_not_renamed():
    assert obskit.GenAI.REQUEST_MODEL == "gen_ai.request.model"
    assert obskit.GenAI.USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert obskit.GenAI.CONVERSATION_ID == "gen_ai.conversation.id"
    assert obskit.GenAI.TOOL_NAME == "gen_ai.tool.name"


def test_langfuse_and_standard_names_were_not_renamed():
    assert obskit.Langfuse.OBSERVATION_TYPE == "langfuse.observation.type"
    assert obskit.Langfuse.TRACE_USER_ID == "user.id"
    assert obskit.Langfuse.TRACE_SESSION_ID == "session.id"


def test_every_app_attribute_uses_the_lab_prefix():
    names = [v for k, v in vars(obskit.App).items()
             if not k.startswith("_") and isinstance(v, str)]
    assert names, "App namespace is empty — the rename lost everything"
    offenders = [n for n in names if not n.startswith("lab.")]
    assert offenders == [], f"not renamed: {offenders}"
    assert not any(n.startswith("legion.") for n in names)


def test_known_attributes_survived_the_rename():
    assert obskit.App.RETRIEVAL_ORDER_TAU == "lab.retrieval.order_tau"
    assert obskit.App.DEGRADED == "lab.degraded"
    assert obskit.App.REQUEST_ID == "lab.request.id"
    assert obskit.App.CONTEXT_PROMPT_UTILISATION == "lab.context.prompt_utilisation"
    assert obskit.App.TENANT_ID == "lab.tenant.id"


def test_app_attribute_count_matches_source():
    """65 attributes came across from otel_semconv.py, plus TENANT_ID added here,
    plus 3 retrieval-identity attributes (chunk_ids / chunk_kinds /
    source_message_ids) added to name WHICH chunks a retrieval returned = 69,
    plus 5 per-tier retrieval-budget attributes (tier.profile / tier.episodic /
    tier.semantic / budget / budget_backfilled) = 74."""
    names = {v for k, v in vars(obskit.App).items()
             if not k.startswith("_") and isinstance(v, str)}
    assert len(names) == 74, f"expected 74 unique app attributes, got {len(names)}"


def test_tier_budget_attributes_are_distinct_from_kind_counts():
    """The tier attributes must not collide with the by-KIND counts beside them.

    They measure different things — from_history sums episodic and raw, which are
    two different tiers — and two spellings of one idea would give two
    half-populated series, which is the failure this module exists to prevent.
    """
    tier = {obskit.App.RETRIEVAL_TIER_PROFILE,
            obskit.App.RETRIEVAL_TIER_EPISODIC,
            obskit.App.RETRIEVAL_TIER_SEMANTIC}
    kind = {obskit.App.RETRIEVAL_FROM_PROFILE,
            obskit.App.RETRIEVAL_FROM_HISTORY,
            obskit.App.RETRIEVAL_FROM_FILES}
    assert tier & kind == set()
    assert len(tier) == 3
    assert obskit.App.RETRIEVAL_TIER_PROFILE == "lab.retrieval.tier.profile"
    assert obskit.App.RETRIEVAL_BUDGET == "lab.retrieval.budget"
    assert obskit.App.RETRIEVAL_BUDGET_BACKFILLED == "lab.retrieval.budget_backfilled"
