"""The probe-analysis validator of ``measurements[].search`` (issue #4121).

Split out of :mod:`klayout_tools.sim_search` (per-file size ratchet, #2766).
"""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError

ANALYSIS_KINDS = ("tran", "dc", "op")


def no_control_chars(text: str) -> bool:
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def validate_analysis(name: Any, analysis: Any) -> None:
    if not isinstance(analysis, dict) or set(analysis) - {"kind", "args"}:
        raise SimError(
            f"measurement '{name}': search.analysis must be an object with "
            'only "kind" and "args"'
        )
    kind = analysis.get("kind")
    if not isinstance(kind, str) or kind.lower() not in ANALYSIS_KINDS:
        raise SimError(
            f"measurement '{name}': search.analysis.kind must be one of "
            f"{list(ANALYSIS_KINDS)}; got {kind!r}"
        )
    args = analysis.get("args", "")
    if not isinstance(args, str) or not no_control_chars(args):
        raise SimError(
            f"measurement '{name}': search.analysis.args must be a single-line string"
        )
    if kind.lower() != "op" and not args.strip():
        raise SimError(
            f"measurement '{name}': search.analysis.args is required for kind {kind!r}"
        )
    if "{" in args or "}" in args:
        raise SimError(
            f"measurement '{name}': search.analysis.args may not use a "
            "{param} brace reference (ngspice does not expand it in a "
            ".control command)"
        )
