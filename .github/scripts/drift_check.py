#!/usr/bin/env python3
"""Fail the build when obskit's pinned semconv literals drift from installed OTel.

obskit pins the gen_ai.* attribute and metric names as string literals instead of
importing them from opentelemetry.semconv._incubating, because that module is
incubating: it is allowed to be renamed, moved or removed, and importing from it
would make obskit break on an unrelated OTel upgrade.

The cost of pinning is that the pins can go stale silently. verify_against_upstream()
compares them, and until now it was tested but called by nothing. This script is the
caller. It exits non-zero on drift, so CI fails rather than printing a warning that
nobody reads.

Exit codes:
  0  pins match the installed OTel, and the comparison genuinely ran
  1  drift detected (or the comparison could not run at all)
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from obskit import semconv
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot import obskit.semconv: {type(exc).__name__}: {exc}")
        return 1

    try:
        import opentelemetry.sdk.version as sdkver

        installed = sdkver.__version__
    except Exception:  # noqa: BLE001
        installed = "unknown"
    print(f"installed opentelemetry-sdk: {installed}")

    # A clean result must mean "compared and matched", not "never compared
    # anything". verify_against_upstream() deliberately swallows the ImportError
    # when the incubating module is missing and returns {} — which is correct for
    # the library but would make this gate vacuous. So prove the module is there
    # before trusting an empty result.
    try:
        from opentelemetry.semconv._incubating.attributes import (
            gen_ai_attributes as up,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            "FAIL: opentelemetry.semconv._incubating.attributes.gen_ai_attributes "
            f"is not importable ({type(exc).__name__}: {exc}).\n"
            "      The drift check cannot compare anything, so a clean result would "
            "be meaningless.\n"
            "      If upstream really did remove the module, update this script and "
            "verify_against_upstream() deliberately."
        )
        return 1

    sample = getattr(up, "GEN_AI_REQUEST_MODEL", None)
    if sample is None:
        print("FAIL: incubating module imported but GEN_AI_REQUEST_MODEL is absent.")
        return 1
    print(f"upstream comparison anchor: GEN_AI_REQUEST_MODEL = {sample!r}")

    drift = semconv.verify_against_upstream()
    if not drift:
        print("OK: pinned semconv literals match the installed OTel SDK.")
        return 0

    print(f"FAIL: {len(drift)} pinned semconv literal(s) drifted from upstream:")
    for name, (ours, theirs) in sorted(drift.items()):
        print(f"  {name}\n    obskit pins : {ours!r}\n    upstream has: {theirs!r}")
    print(
        "\nThis is not a warning. Either update the pinned literal in "
        "src/obskit/semconv.py\nand the tests that assert it, or decide "
        "deliberately to keep the old name and\nrecord why."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
