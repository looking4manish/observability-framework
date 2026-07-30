"""Structured JSON logging that joins to the trace.

A span tells you THAT something failed. The error text lives in a log line, and
without a shared key there is no way to get from one to the other. This module
emits one JSON object per line to stdout, and every record automatically carries
the same request id, trace id and span id the spans carry — so a value copied out
of a log line pastes straight into the trace UI and lands on the right span.

Named `logs` rather than `logging` deliberately: a module called
`obskit/logging.py` is legal under absolute imports but makes every
`import logging` in this package read ambiguously to a human.

Four things worth knowing about the design:

**Setup is explicit and opt-in.** `init()` does not call `setup_logging()` and
never will. An application that wants only traces gets its logging left exactly
as it found it — reconfiguring a host application's root logger as a side effect
of asking for traces would be a genuinely hostile default.

**Context comes from the tracing contextvars, not from the callsite.** The
application writes `log.info("...")` exactly as it does today; request_id,
trace_id, span_id, tenant_id, user_id and session_id are read from the same
contextvars `bind_request()` and the span helpers already populate. No callsite
changes, no logger adapters to thread through.

**Redaction is the existing one.** Messages and structured fields go through
`redaction.scrub`, the same function that scrubs span attributes. There is no
second implementation to drift.

**Fail-soft, like the rest of the package.** `setup_logging()` catches its own
exceptions and reports them in its return value rather than raising, and the
formatter falls back to a plain line if JSON encoding fails. Logging must not be
able to take down the application it is describing.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from . import tracing
from .redaction import scrub

# Attributes the stdlib puts on every LogRecord. Anything NOT in here that an
# application passed via `extra=` is user data and gets promoted into the JSON.
_STDLIB_RECORD_FIELDS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "taskName", "thread", "threadName",
}

DEFAULT_FIELD_ORDER = ("timestamp", "level", "logger", "message")


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON, enriched from the trace context.

    Enrichment is best-effort by design: a log line emitted outside any request
    (startup, a background task, a CLI) simply omits the correlation fields
    rather than emitting nulls or failing. Absent is more honest than null here —
    a null request_id would imply one was expected.
    """

    def __init__(self, *, static_fields: dict | None = None,
                 include_source: bool = False):
        super().__init__()
        self._static = dict(static_fields or {})
        self._include_source = include_source

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        try:
            return json.dumps(self._build(record), default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            # Last resort: never lose the line, never raise out of a handler.
            try:
                return json.dumps({"level": record.levelname,
                                   "logger": record.name,
                                   "message": scrub(record.getMessage()),
                                   "log_format_error": True})
            except Exception:  # noqa: BLE001
                return f"{record.levelname} {record.name} <unformattable log record>"

    def _build(self, record: logging.LogRecord) -> dict:
        out: dict = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }

        # --- correlation, read from the tracing contextvars ------------------
        try:
            trace_id, span_id = tracing.current_span_ids()
            for key, value in (("request_id", tracing.request_id()),
                               ("trace_id", trace_id),
                               ("span_id", span_id),
                               ("tenant_id", tracing.tenant_id()),
                               ("user_id", tracing.user_id()),
                               ("session_id", tracing.session_id())):
                if value:
                    out[key] = value
        except Exception:  # noqa: BLE001
            pass

        # --- service identity, whatever init() was given --------------------
        try:
            ident = tracing.service_identity()
            if ident.get("service_name"):
                out["service"] = {
                    "name": ident["service_name"],
                    "namespace": ident.get("service_namespace"),
                    "version": ident.get("service_version"),
                    "environment": ident.get("deployment_environment"),
                }
        except Exception:  # noqa: BLE001
            pass

        if self._static:
            out.update(self._static)

        if self._include_source:
            out["source"] = {"file": record.pathname, "line": record.lineno,
                             "function": record.funcName}

        # --- anything the callsite passed via extra= -------------------------
        extras = {k: v for k, v in record.__dict__.items()
                  if k not in _STDLIB_RECORD_FIELDS and not k.startswith("_")}
        if extras:
            out["extra"] = scrub(extras)

        if record.exc_info:
            out["exception"] = scrub(self.formatException(record.exc_info))
        if record.stack_info:
            out["stack"] = scrub(self.formatStack(record.stack_info))
        return out


# Loggers uvicorn configures for itself. It never touches the root logger, so an
# application logger has no handler at all unless something adds one — which is
# why a plain log.info() in a uvicorn-hosted app currently goes nowhere.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def setup_logging(*, level: int | str = "INFO",
                  stream=None,
                  capture_uvicorn: bool = False,
                  static_fields: dict | None = None,
                  include_source: bool = False,
                  replace_existing: bool = True) -> dict:
    """Install the JSON handler on the ROOT logger. Explicit, optional, idempotent.

    Returns a status dict rather than raising, so a logging misconfiguration
    cannot stop a service from starting.

    level            root log level.
    stream           defaults to sys.stdout, so journald/docker capture it.
    capture_uvicorn  also route uvicorn's own loggers through this handler, so
                     access lines become JSON too. Off by default: it mutates
                     loggers this package does not own.
    static_fields    merged into every record (e.g. {"region": "ap-south-1"}).
    include_source   add file/line/function. Off by default — it is noise in
                     production and useful in development.
    replace_existing remove handlers already on root before adding ours. On by
                     default so calling this twice does not double every line.
    """
    try:
        root = logging.getLogger()
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        handler.setFormatter(JsonFormatter(static_fields=static_fields,
                                           include_source=include_source))
        handler.set_name("obskit-json")

        if replace_existing:
            for h in list(root.handlers):
                root.removeHandler(h)
        root.addHandler(handler)
        root.setLevel(level)

        captured = []
        if capture_uvicorn:
            for name in _UVICORN_LOGGERS:
                lg = logging.getLogger(name)
                for h in list(lg.handlers):
                    lg.removeHandler(h)
                lg.propagate = True   # fall through to root's JSON handler
                captured.append(name)

        return {"status": "enabled", "level": logging.getLevelName(root.level),
                "handler": "obskit-json", "captured_uvicorn": captured}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}


def get_logger(name: str) -> logging.Logger:
    """Plain `logging.getLogger`. Here only so an application can import one name."""
    return logging.getLogger(name)
