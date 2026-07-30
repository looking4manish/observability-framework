"""Structured logging: correlation fields, id-format agreement with spans,
redaction, fail-soft setup, and valid JSON. In-memory only, no network."""
import io
import json
import logging

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import obskit
from obskit import tracing


@pytest.fixture()
def logstream():
    """Root logger wired to obskit's JSON handler over an in-memory stream."""
    tracing.reset_for_tests()
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    buf = io.StringIO()
    res = obskit.setup_logging(level="DEBUG", stream=buf)
    assert res["status"] == "enabled", res
    yield buf
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    tracing.reset_for_tests()


@pytest.fixture()
def traced(logstream):
    """As above, plus a live tracer over an in-memory span exporter."""
    exp = InMemorySpanExporter()
    res = obskit.init(enabled_flag=True, service_name="obskit-tests",
                      service_namespace="lab", deployment_environment="test",
                      service_version="9.9.9", endpoint="",
                      span_processor=SimpleSpanProcessor(exp))
    assert res["status"] == "enabled", res
    yield logstream, exp


def lines(buf):
    return [json.loads(l) for l in buf.getvalue().strip().splitlines() if l.strip()]


def pick(buf, logger_prefix="t."):
    """Records from OUR test loggers only.

    Necessary because setup_logging() attaches to the ROOT logger, so it also
    captures third-party library records — notably OpenTelemetry's own
    "Overriding of current TracerProvider is not allowed" WARNING, emitted the
    second time init() runs in one process. That capture is correct behaviour
    (see test_third_party_library_logs_are_captured); it just means a test must
    select its line by logger rather than by index.
    """
    return [r for r in lines(buf) if r["logger"].startswith(logger_prefix)]


# --- output shape ----------------------------------------------------------

def test_output_is_one_valid_json_object_per_line(logstream):
    log = logging.getLogger("t.shape")
    log.info("first")
    log.warning("second")
    recs = lines(logstream)
    assert len(recs) == 2
    assert [r["message"] for r in recs] == ["first", "second"]
    assert [r["level"] for r in recs] == ["INFO", "WARNING"]
    assert recs[0]["logger"] == "t.shape"
    assert recs[0]["timestamp"].endswith("+00:00")


def test_percent_style_message_is_rendered(logstream):
    logging.getLogger("t.fmt").info("hello %s, %d items", "world", 3)
    assert lines(logstream)[0]["message"] == "hello world, 3 items"


# --- correlation fields ----------------------------------------------------

def test_fields_present_when_context_bound(traced):
    buf, _ = traced
    obskit.bind_request(request_id="req-1", user_id="u1", session_id="s1",
                        tenant_id="acme")
    root = obskit.start_root_span("work")
    logging.getLogger("t.ctx").info("inside")
    obskit.end_root_span(root)
    r = pick(buf)[0]
    assert r["request_id"] == "req-1"
    assert r["user_id"] == "u1"
    assert r["session_id"] == "s1"
    assert r["tenant_id"] == "acme"
    assert r["service"] == {"name": "obskit-tests", "namespace": "lab",
                            "version": "9.9.9", "environment": "test"}


def test_fields_absent_but_no_crash_when_unbound(logstream):
    logging.getLogger("t.unbound").info("no request here")
    r = lines(logstream)[0]
    for k in ("request_id", "trace_id", "span_id", "tenant_id", "user_id", "session_id"):
        assert k not in r, f"{k} should be omitted, not null, when unbound"
    assert r["message"] == "no request here"


def test_service_block_absent_before_init(logstream):
    logging.getLogger("t.noinit").info("startup, pre-init")
    assert "service" not in lines(logstream)[0]


# --- the join: ids must match the emitted span exactly ---------------------

def test_trace_and_span_id_match_the_active_span(traced):
    buf, exp = traced
    obskit.bind_request(request_id="req-2")
    root = obskit.start_root_span("root")
    with obskit.span("child"):
        logging.getLogger("t.join").info("emitted inside child")
    obskit.end_root_span(root)

    rec = pick(buf)[0]
    spans = {s.name: s for s in exp.get_finished_spans()}
    child = spans["child"]

    assert rec["trace_id"] == format(child.context.trace_id, "032x")
    assert rec["span_id"] == format(child.context.span_id, "016x")
    # innermost, not the root — a reader should land on the exact span
    assert rec["span_id"] != format(spans["root"].context.span_id, "016x")
    assert rec["trace_id"] == format(spans["root"].context.trace_id, "032x")


def test_id_formats_are_canonical_hex(traced):
    buf, _ = traced
    root = obskit.start_root_span("root")
    logging.getLogger("t.fmt2").info("x")
    obskit.end_root_span(root)
    r = pick(buf)[0]
    assert len(r["trace_id"]) == 32 and int(r["trace_id"], 16) >= 0
    assert len(r["span_id"]) == 16 and int(r["span_id"], 16) >= 0
    assert r["trace_id"] == r["trace_id"].lower()


def test_log_ids_match_the_handle_the_app_holds(traced):
    """current_trace_id() is what the app records elsewhere; logs must agree."""
    buf, _ = traced
    root = obskit.start_root_span("root")
    logging.getLogger("t.agree").info("x")
    handle_trace = obskit.current_trace_id()
    obskit.end_root_span(root)
    assert pick(buf)[0]["trace_id"] == handle_trace


# --- redaction -------------------------------------------------------------

def test_redaction_applied_to_message(logstream):
    logging.getLogger("t.red").error(
        "connect failed: mongodb://admin:n0tArealPassw0rd@oci-p:27017/legion")
    msg = lines(logstream)[0]["message"]
    assert "n0tArealPassw0rd" not in msg
    assert "mongodb://" in msg and "oci-p:27017" in msg


def test_redaction_applied_to_extra_fields(logstream):
    logging.getLogger("t.red2").info(
        "calling out", extra={"uri": "postgres://u:p4ss@pg:5432/d",
                              "auth": "Bearer abcdefghijklmnopqrst"})
    extra = lines(logstream)[0]["extra"]
    assert "p4ss" not in json.dumps(extra)
    assert "abcdefghijklmnopqrst" not in json.dumps(extra)
    assert "pg:5432" in extra["uri"]


def test_redaction_applied_to_exception_text(logstream):
    try:
        raise RuntimeError("token=sk-lf-0123456789abcdef0123")
    except RuntimeError:
        logging.getLogger("t.red3").exception("boom")
    r = pick(logstream)[0]
    # The secret itself must be gone — that is the property that matters.
    assert "0123456789abcdef0123" not in r["exception"]
    assert "[REDACTED]" in r["exception"]
    # NOTE: this input is matched by BOTH the vendor-key and named-secret
    # patterns, and they compose into "token=[REDACTED]]" — a stray bracket, and
    # the "sk-lf-" prefix the vendor rule would otherwise have preserved is lost.
    # Cosmetic, pre-existing in redaction.py since v0.1.0, and NOT fixed here:
    # changing it would alter span-attribute redaction too, which is outside a
    # logging mission. Reported instead.


def test_redaction_uses_the_shared_module(logstream):
    """Same input, same result on a span attribute and on a log line."""
    raw = "mongodb+srv://admin:S3cret@h/db"
    logging.getLogger("t.same").info(raw)
    assert lines(logstream)[0]["message"] == obskit.scrub(raw)


# --- fail-soft -------------------------------------------------------------

def test_setup_logging_failure_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no handler for you")
    monkeypatch.setattr(logging, "StreamHandler", boom)
    res = obskit.setup_logging()
    assert res["status"] == "error"
    assert "no handler for you" in res["reason"]


def test_formatter_never_raises_on_unserialisable_extra(logstream):
    class Nasty:
        def __repr__(self):
            raise RuntimeError("nope")
    logging.getLogger("t.nasty").info("has a nasty field", extra={"bad": Nasty()})
    out = logstream.getvalue().strip()
    assert out, "line must not be dropped"
    json.loads(out.splitlines()[-1])  # still parseable


def test_emission_survives_context_lookup_failure(logstream, monkeypatch):
    monkeypatch.setattr(tracing, "current_span_ids",
                        lambda: (_ for _ in ()).throw(RuntimeError("ctx gone")))
    logging.getLogger("t.ctxfail").info("still logged")
    r = lines(logstream)[0]
    assert r["message"] == "still logged"
    assert "trace_id" not in r


# --- opt-in behaviour ------------------------------------------------------

def test_init_does_not_configure_logging():
    """An application that wants only traces keeps its own logging untouched."""
    tracing.reset_for_tests()
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        for h in saved:
            root.removeHandler(h)
        obskit.init(enabled_flag=False, service_name="s", service_namespace="n",
                    deployment_environment="e", endpoint="")
        assert root.handlers == [], "init() must not install a log handler"
    finally:
        for h in saved:
            root.addHandler(h)
        tracing.reset_for_tests()


def test_setup_logging_twice_does_not_duplicate_lines():
    root = logging.getLogger()
    saved, lvl = list(root.handlers), root.level
    try:
        buf = io.StringIO()
        obskit.setup_logging(level="INFO", stream=buf)
        obskit.setup_logging(level="INFO", stream=buf)
        logging.getLogger("t.dup").info("once")
        assert len(lines(buf)) == 1
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
        root.setLevel(lvl)


def test_static_fields_and_source_are_opt_in():
    root = logging.getLogger()
    saved, lvl = list(root.handlers), root.level
    try:
        buf = io.StringIO()
        obskit.setup_logging(level="INFO", stream=buf,
                             static_fields={"region": "ap-south-1"},
                             include_source=True)
        logging.getLogger("t.static").info("x")
        r = lines(buf)[0]
        assert r["region"] == "ap-south-1"
        assert r["source"]["function"] == "test_static_fields_and_source_are_opt_in"
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
        root.setLevel(lvl)


def test_third_party_library_logs_are_captured(logstream):
    """Attaching to root means library logs become JSON too, not just ours."""
    logging.getLogger("some.vendor.lib").warning("vendor says hello")
    recs = [r for r in lines(logstream) if r["logger"] == "some.vendor.lib"]
    assert len(recs) == 1 and recs[0]["level"] == "WARNING"
