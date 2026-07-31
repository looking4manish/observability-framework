"""Scrub credential-shaped values out of span attributes.

This runs inside the attribute-setting path (`tracing._Span.set_attribute`), not
at the callsites. That is the whole point: an application cannot bypass it by
forgetting to call it. Every attribute value, every input/output blob and every
serialized metadata payload passes through `scrub()` on its way to the span.

Strategy — redact the secret, keep the shape:

  A trace is a debugging tool. Replacing a whole value with "[REDACTED]" destroys
  the reason you were looking at it. So each pattern keeps the parts that identify
  *what* the value was and drops only the part that authenticates:

    mongodb://app:s3cret@db1:27017/x  ->  mongodb://app:[REDACTED]@db1:27017/x
      scheme, username, host, port and database survive; the password does not.
      The username is kept deliberately: "which credential was this" is a normal
      debugging question and the username is not the secret.

    Authorization: Bearer eyJhbGci...  ->  Authorization: Bearer [REDACTED]
      the scheme survives so you can still tell bearer from basic.

    sk-lf-abcdef0123456789            ->  sk-lf-[REDACTED]
      the vendor prefix survives so you can still tell which key type leaked.

    -----BEGIN RSA PRIVATE KEY-----   ->  [REDACTED PRIVATE KEY BLOCK]
      nothing inside a key block is worth keeping.

Deliberate limits, stated rather than hidden:

  - This is regex matching on a best-effort basis, not a proof. A credential in a
    shape not listed here passes through. It reduces blast radius; it is not a
    guarantee, and it is not a substitute for not putting secrets in traces.

  - REDACT BEFORE YOU TRUNCATE. Most patterns here are keyword-anchored: a bearer
    token is found by the preceding word "Bearer", a URI password by its
    "scheme://user:" prefix. An application that shortens, elides or summarizes a
    value BEFORE handing it over can cut the keyword away from the secret, and the
    anchored pattern then matches nothing while the credential itself survives
    intact. This is a real, observed failure, not a theoretical one.

    The choke point in `tracing._Span.set_attribute` cannot save you here: it sees
    only what it is given, and by then the evidence it needs is gone. If your
    application truncates a value that might contain a credential, scrub it FIRST
    and truncate the already-scrubbed result. `_JWT` below is a partial mitigation
    for the one shape that self-identifies; nothing can recover an opaque password
    whose scheme prefix was deleted.
  - It runs on every attribute write, so the patterns are anchored and bounded to
    keep the cost near zero on the common case (a value with no `://`, no
    `Bearer`, no `-----BEGIN`, and no long high-entropy run is returned after a
    handful of cheap substring checks).
  - It never raises. A redaction bug must not take down the request path, which
    is the same contract the rest of this package holds.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"
_KEY_BLOCK = "[REDACTED PRIVATE KEY BLOCK]"
# Unlike a URI or an Authorization header, a JWT has no non-secret part worth
# keeping: the header segment only names the algorithm. Replace the whole thing.
_REDACTED_JWT = "[REDACTED JWT]"

# Database / broker URLs carrying an inline password. Keeps scheme + user + host,
# drops the password. Deliberately covers the schemes named in the brief plus the
# +srv variant, and any user:pass@host URL for the listed schemes.
_URL_CREDS = re.compile(
    r"\b((?:mongodb\+srv|mongodb|postgresql|postgres|mysql|redis|rediss|amqp|amqps)"
    r"://[^\s:/@]+:)([^\s@]+)(@)",
    re.IGNORECASE,
)

# Any other scheme://user:pass@host — a generic backstop for URLs not listed above.
_URL_CREDS_GENERIC = re.compile(
    r"\b([a-z][a-z0-9+.\-]{1,20}://[^\s:/@]+:)([^\s@]{3,})(@)",
    re.IGNORECASE,
)

# Authorization headers and bare bearer tokens.
_BEARER = re.compile(
    r"\b(Bearer|Basic|Token)\s+([A-Za-z0-9._\-+/=]{8,})",
    re.IGNORECASE,
)

# JWTs, matched WITHOUT a preceding keyword.
#
# _BEARER above is anchored on the word "Bearer", which is the right trade for cost
# but has a failure mode worth naming: if an application truncates a value BEFORE it
# reaches this module, the cut can sever the keyword from the token and leave the
# credential itself intact. The anchored pattern then matches nothing and the raw
# token is written to the span. That is not hypothetical — it was observed in a live
# trace, where an elision cut "Bearer" down to "er" and the token body survived.
#
# A JWT is one of the few credential shapes that can be recognised with no
# surrounding context, because it carries its own marker: the header segment is
# base64url of a JSON object, so it begins "eyJ". That marker is INSIDE the
# credential, so it survives the loss of everything around it.
#
# This is defence in depth, not a fix. It cannot rescue an opaque secret — a
# truncated "user:PASSWORD@host" is indistinguishable from ordinary text once the
# scheme is gone. The complete fix is ordering: redact before you truncate. See the
# note in the module docstring.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_=-]{4,}\.[A-Za-z0-9._=+/-]{4,}")

# Vendor-prefixed API keys: sk-..., pk-..., sk-lf-..., ghp_..., xoxb-..., AKIA...
_VENDOR_KEY = re.compile(
    r"\b((?:sk|pk|rk)-(?:[a-z]{2,6}-)?|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|"
    r"xox[baprs]-|AKIA|ASIA|glpat-|hf_|AIza)([A-Za-z0-9_\-]{8,})",
)

# key=value / "key": "value" where the key name says secret.
_NAMED_SECRET = re.compile(
    r"(?i)\b(pass(?:word|wd)?|secret|token|api[_\-]?key|access[_\-]?key|"
    r"secret[_\-]?key|private[_\-]?key|auth|credential)s?"
    r"(\"?\s*[:=]\s*\"?)"
    r"([^\s,;&\"'}\]]{4,})",
)

# PEM-style key blocks, including the body.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# A truncated block (common when a value was clipped) — header with no terminator.
_PEM_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# Cheap pre-filter. If none of these appear, no pattern above can match, so the
# common attribute (a number, a short label, a model name) skips every regex.
_TRIGGERS = ("://", "bearer", "basic ", "token", "-----begin", "sk-", "pk-", "rk-",
             "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "xox", "akia",
             "asia", "glpat-", "hf_", "aiza", "pass", "secret", "key", "auth",
             "credential", "eyj")


def _looks_interesting(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in _TRIGGERS)


def scrub_text(text: str) -> str:
    """Redact credential-shaped substrings in `text`. Never raises."""
    if not text or not isinstance(text, str):
        return text
    try:
        if not _looks_interesting(text):
            return text
        out = _PEM_BLOCK.sub(_KEY_BLOCK, text)
        out = _PEM_HEADER.sub(_KEY_BLOCK, out)
        out = _URL_CREDS.sub(lambda m: m.group(1) + REDACTED + m.group(3), out)
        out = _URL_CREDS_GENERIC.sub(lambda m: m.group(1) + REDACTED + m.group(3), out)
        out = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
        # After _BEARER on purpose: when the keyword survived, the token is already
        # gone and this matches nothing. It earns its keep only when the keyword did
        # not survive.
        out = _JWT.sub(_REDACTED_JWT, out)
        out = _VENDOR_KEY.sub(lambda m: m.group(1) + REDACTED, out)
        out = _NAMED_SECRET.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
        return out
    except Exception:  # noqa: BLE001 — redaction must never break the request path
        return text


def scrub(value):
    """Redact a value of any type on its way to a span attribute.

    Strings are scrubbed. Containers are walked so a redacted secret cannot hide
    one level down inside a list or dict that is about to be JSON-serialized.
    Everything else (int, float, bool, None) is returned unchanged.
    """
    try:
        if isinstance(value, str):
            return scrub_text(value)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            scrubbed = [scrub(v) for v in value]
            return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
    except Exception:  # noqa: BLE001
        return value
    return value
