"""Per-corner analysis windows and transient start-state helpers.

``request.analysis_overrides`` (issue #4970) uses the same ``match`` selector
and per-point resolution as ``options.nodeset``, but the selected value is an
``analysis.args`` string that replaces the request's for that point only. The
``uic`` start-state helpers of ``options.initial_conditions`` (issue #3881)
live here too: both edit the analysis command line of one point.

The rule validator needs ``sim``'s nodeset-match grammar, so it is imported
lazily; ``sim`` re-exports every name below under its historical spelling.
"""

from __future__ import annotations

import json
from typing import Any

from . import sim_steps
from .sim_errors import SimError

AO_LABEL = "analysis_overrides"
AO_UNSUPPORTED_WORKER_CODE = "analysis_overrides_unsupported_worker"
IC_LABEL = "options.initial_conditions"
IC_UNSUPPORTED_WORKER_CODE = "initial_conditions_unsupported_worker"


def require_plain_tran_for_ic(declared: bool, analysis: dict[str, Any]) -> None:
    """``uic`` is a ``tran`` flag, so a start state needs a plain transient
    analysis; step/pass/two-port sequences and other kinds are refused up
    front rather than silently run without it."""
    if not declared:
        return
    if (
        sim_steps.is_steps_analysis(analysis)
        or str(analysis.get("kind", "")).lower() != "tran"
    ):
        raise SimError(
            f"{IC_LABEL} requires a single analysis of kind 'tran' "
            "(`.ic` is applied with the `uic` flag of the transient command)"
        )


def ic_cards(point: Any) -> list[str]:
    """One file-scope ``.ic`` card per node of the point's start state."""
    return [
        f".ic v({node})={volts!r}"
        for node, volts in sorted(
            (getattr(point, "initial_conditions", None) or {}).items()
        )
    ]


def ic_analysis_args(point: Any, args: str) -> str:
    """``args`` with ``uic`` appended for a point carrying a start state,
    unless the request already spells it; untouched otherwise."""
    if not getattr(point, "initial_conditions", None):
        return args
    if "uic" in str(args).lower().split():
        return args
    return f"{args} uic".strip()


def validate_analysis_overrides(
    value: Any, analysis: dict[str, Any], engine: str
) -> list[dict[str, Any]]:
    """Validate ``request.analysis_overrides`` into normalized rules
    ``[{"match": {...}, "values": "<args>"}]``; ``[]`` when omitted/empty.
    Overrides replace ``analysis.args`` only, never ``kind``."""
    from . import sim  # lazy: sim imports this module

    if value is None:
        return []
    if not isinstance(value, list):
        raise SimError(
            f"{AO_LABEL} must be an array of {{match, args}} rules "
            f"(got {json.dumps(value, default=repr)})"
        )
    if not value:
        return []
    if sim_steps.is_steps_analysis(analysis):
        raise SimError(
            f"{AO_LABEL} cannot be combined with analysis_steps: overrides "
            "replace one `analysis.args` and a step sequence has several"
        )
    if not analysis or "args" not in analysis:
        raise SimError(
            f"{AO_LABEL} requires a request with a scalar `analysis` "
            "(deck-list / commands[] requests carry none)"
        )
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(value):
        where = f"{AO_LABEL}[{index}]"
        if not isinstance(rule, dict) or set(rule) != {"match", "args"}:
            raise SimError(
                f"{where} must be an object with exactly the keys match, args"
            )
        args = rule["args"]
        if not isinstance(args, str) or not args.strip():
            raise SimError(f"{where}.args must be a non-empty string")
        sim._reject_param_braces({"args": args}, engine)
        rules.append(
            {
                "match": sim._validate_nodeset_match(rule["match"], f"{where}.match"),
                "values": args.strip(),
            }
        )
    return rules


def effective_analysis_args(point: Any, analysis: dict[str, Any]) -> str:
    """The ``analysis.args`` this point runs: its override, else the request's."""
    return getattr(point, "analysis_args", None) or analysis["args"]


def report_field(point: Any) -> dict[str, Any]:
    """The additive ``analysis_args`` field of a corner report (issue #4970):
    present only for a point an ``analysis_overrides`` rule selected."""
    return {"analysis_args": point.analysis_args} if point.analysis_args else {}


def deck_args(point: Any, analysis: dict[str, Any]) -> str:
    """The analysis arguments of a point's ngspice deck line: its window (or
    the request's) with ``uic`` added for a start state."""
    return ic_analysis_args(point, effective_analysis_args(point, analysis))


def resolve(points: list[Any], rules: list[dict[str, Any]]) -> None:
    """Select each point's window from ``rules`` (the nodeset resolution)."""
    from . import sim  # lazy: sim imports this module

    sim._apply_nodeset_rules(points, rules, label=AO_LABEL, attr="analysis_args")


def require_echo(points: list[Any], corners: list[Any], backend: str) -> None:
    """Refuse a remote/batch worker that did not echo each point's window."""
    from . import sim  # lazy: sim imports this module

    sim._require_nodeset_echo(
        points,
        corners,
        backend=backend,
        label=AO_LABEL,
        attr="analysis_args",
        code=AO_UNSUPPORTED_WORKER_CODE,
    )


def windows_by_corner(points: list[Any]) -> dict[str, str]:
    """``{corner_id: window}`` of the points a rule selected (fingerprint key:
    a changed per-corner window changes the result)."""
    return {p.corner_id: p.analysis_args for p in points if p.analysis_args}


def wire_args(entry: dict[str, Any]) -> str | None:
    """The validated ``analysis_args`` of one ``_explicit_points`` wire entry."""
    raw = entry.get("analysis_args")
    if raw is not None and (not isinstance(raw, str) or not raw.strip()):
        raise SimError(
            "request._explicit_points[].analysis_args must be a non-empty string"
        )
    return raw
