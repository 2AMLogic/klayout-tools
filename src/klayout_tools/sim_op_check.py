"""Metastable operating-point detection for ``klt sim`` (issue #5077).

A DC operating point of a circuit with a bistable element (cross-coupled
latch, SRAM bit, parked flip-flop) is under-determined: besides the two stable
states it has an unstable mid-rail solution, and the engine can converge onto
it. The corner then grades ``pass`` with a supply current wrong by orders of
magnitude. ``analysis.op_check`` is an opt-in *detection* (it never forces a
state)::

    {"analysis": {"kind": "op", "args": "",
                  "op_check": {"nodes": ["x1.q", "x1.qb"], "band": 0.15}}}

After the ``op`` the generated ``.control`` block reads ``v(<node>)`` for each
declared node (``let`` + ``print``, the shape the ``expr`` measurements use).
A node within ``band * supply`` of mid-rail (``supply / 2``) earns an
``op_metastable`` diagnostic at ``severity: "warning"`` -- the corner's grade
is unchanged unless the caller lists ``op_metastable`` in
``options.fail_on_diagnostic`` (then it grades ``inconclusive``).

The supply that defines the rail is, in order: ``op_check.supply`` (a fixed
number of volts), the ``corners.supply_v`` rail named by ``op_check.rail``, the
corner's only rail, or -- with several rails -- the largest one. A corner with
none of those cannot be checked and reports an ``error`` diagnostic instead of
silently skipping the check.

Report shape (additive): ``corners[].op_check`` =
``{"supply_v", "band", "nodes": {node: volts|null}, "metastable": [node...]}``,
present on every corner of a request that declares ``op_check``.

``analysis`` is already part of the resume fingerprint and is forwarded to
off-host workers verbatim, so changing ``op_check`` invalidates a checkpoint
and a request without it is byte-identical to before.
"""

from __future__ import annotations

import math
import re
from typing import Any

from ._jsonnum import is_number
from .sim_errors import SimError
from .sim_remote import _UNRUN_MESSAGES

#: Fraction of the supply used when ``op_check.band`` is omitted.
DEFAULT_BAND = 0.15

OP_CHECK_KEYS = ("nodes", "band", "rail", "supply")

#: Same node grammar as ``options.nodeset`` (one bare SPICE identifier).
_NODE_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.:]*|[0-9]+)")

_VECTOR_PREFIX = "__klt_opc_"
_VALUE_RE = re.compile(
    r"^"
    + _VECTOR_PREFIX
    + r"(\d+)\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*$",
    re.MULTILINE,
)

OP_METASTABLE_CODE = "op_metastable"
OP_CHECK_UNSUPPORTED_WORKER_CODE = "op_check_unsupported_worker"


def is_declared(analysis: dict[str, Any]) -> bool:
    return isinstance(analysis, dict) and analysis.get("op_check") is not None


def _number(value: Any, where: str) -> float:
    if not is_number(value):
        raise SimError(f"{where} must be a number (got {value!r})")
    if not math.isfinite(value):
        raise SimError(f"{where} must be finite (got {value!r})")
    return float(value)


def validate(analysis: dict[str, Any]) -> None:
    """Refuse a malformed ``analysis.op_check`` before any engine launch."""
    if "op_check" not in analysis:
        return
    spec = analysis["op_check"]
    if str(analysis.get("kind", "")).lower() != "op":
        raise SimError(
            'analysis.op_check requires analysis.kind "op" '
            f"(got {analysis.get('kind')!r}): it inspects an operating-point "
            "solution and has no meaning for other analyses"
        )
    if not isinstance(spec, dict):
        raise SimError("analysis.op_check must be an object")
    unknown = sorted(set(spec) - set(OP_CHECK_KEYS))
    if unknown:
        raise SimError(
            f"analysis.op_check has unknown key(s) {unknown}; "
            f"allowed: {list(OP_CHECK_KEYS)}"
        )
    _validate_nodes(spec.get("nodes"))
    _validate_levels(spec)


def _validate_nodes(nodes: Any) -> None:
    if not isinstance(nodes, list) or not nodes:
        raise SimError(
            "analysis.op_check.nodes must be a non-empty array of node names"
        )
    seen: set[str] = set()
    for i, node in enumerate(nodes):
        where = f"analysis.op_check.nodes[{i}]"
        if not isinstance(node, str) or not _NODE_RE.fullmatch(node):
            raise SimError(
                f"{where} must be a bare SPICE node name (letters, digits, "
                f"'_', '.', ':'); got {node!r}"
            )
        if node.lower() in seen:
            raise SimError(f"{where} repeats node {node!r}")
        seen.add(node.lower())


def _validate_levels(spec: dict[str, Any]) -> None:
    if "band" in spec:
        band = _number(spec["band"], "analysis.op_check.band")
        if not 0 < band <= 0.5:
            raise SimError(
                "analysis.op_check.band must be in (0, 0.5] (a fraction of "
                f"the supply around mid-rail); got {band!r}"
            )
    if "rail" in spec and "supply" in spec:
        raise SimError("analysis.op_check.rail and .supply are mutually exclusive")
    if "rail" in spec and (not isinstance(spec["rail"], str) or not spec["rail"]):
        raise SimError("analysis.op_check.rail must be a non-empty string")
    if "supply" in spec and _number(spec["supply"], "analysis.op_check.supply") <= 0:
        raise SimError("analysis.op_check.supply must be positive")


def _nodes(analysis: dict[str, Any]) -> list[str]:
    return [str(n).lower() for n in analysis["op_check"]["nodes"]]


def control_lines(analysis: dict[str, Any]) -> list[str]:
    """``let``/``print`` pairs reading each declared node; none without
    ``op_check``."""
    if not is_declared(analysis):
        return []
    lines: list[str] = []
    for k, node in enumerate(_nodes(analysis)):
        lines.append(f"let {_VECTOR_PREFIX}{k} = v({node})")
        lines.append(f"print {_VECTOR_PREFIX}{k}")
    return lines


def _supply(analysis: dict[str, Any], supply_v: dict[str, float]) -> float | None:
    spec = analysis["op_check"]
    if "supply" in spec:
        return float(spec["supply"])
    rails = {str(k).lower(): float(v) for k, v in supply_v.items()}
    if "rail" in spec:
        return rails.get(str(spec["rail"]).lower())
    if not rails:
        return None
    return max(rails.values(), key=abs)


def evaluate(
    analysis: dict[str, Any],
    supply_v: dict[str, float],
    log_text: str,
    *,
    skip: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The corner's ``op_check`` report field and its diagnostics. ``skip``
    (the corner timed out or the engine never ran the analysis) reports
    neither."""
    if skip or not is_declared(analysis):
        return {}, []
    spec = analysis["op_check"]
    band = float(spec.get("band", DEFAULT_BAND))
    names = _nodes(analysis)
    found = {int(m.group(1)): float(m.group(2)) for m in _VALUE_RE.finditer(log_text)}
    values: dict[str, float | None] = {
        node: found.get(k) for k, node in enumerate(names)
    }
    diagnostics: list[dict[str, str]] = []
    for node, value in values.items():
        if value is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "no_such_vector",
                    "message": (
                        f"analysis.op_check could not read v({node}): the "
                        "node does not exist or the operating point did not "
                        "complete"
                    ),
                }
            )
    supply = _supply(analysis, supply_v)
    metastable: list[str] = []
    if supply is None:
        diagnostics.append(
            {
                "severity": "error",
                "code": "measurement",
                "message": (
                    "analysis.op_check cannot locate mid-rail for this "
                    "corner: it has no corners.supply_v rail"
                    + (f" named {spec['rail']!r}" if "rail" in spec else "")
                    + "; set analysis.op_check.supply to the rail voltage"
                ),
            }
        )
    else:
        mid = supply / 2.0
        half_width = band * abs(supply)
        for node, value in values.items():
            if value is not None and abs(value - mid) <= half_width:
                metastable.append(node)
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": OP_METASTABLE_CODE,
                        "message": (
                            f"v({node}) = {value:.6g} V is within "
                            f"{band:g} x supply of mid-rail ({mid:.6g} V of a "
                            f"{supply:.6g} V rail): the operating point may "
                            "be the unstable solution of a bistable circuit, "
                            "so supply-current and node-voltage results are "
                            "suspect. Pin the node (hold source) or fix the "
                            "initial state and re-run"
                        ),
                    }
                )
    field = {
        "supply_v": supply,
        "band": band,
        "nodes": values,
        "metastable": metastable,
    }
    return {"op_check": field}, diagnostics


def reject_in_steps(request: dict[str, Any]) -> None:
    """``op_check`` is only available on a scalar ``op`` analysis."""
    analysis = request.get("analysis")
    if isinstance(analysis, dict) and "op_check" in analysis:
        raise SimError(
            "analysis.op_check is not supported with analysis_steps[]: it "
            "is only available on a scalar `op` analysis"
        )


def refuse_xyce(declared: bool) -> None:
    """``op_check`` reads node voltages through an ngspice ``.control``
    block, which the Xyce deck does not have."""
    if declared:
        raise SimError(
            "analysis.op_check is not supported for engine 'xyce': it reads "
            "node voltages through an ngspice `.control` block, which the Xyce "
            "deck does not have; use engine 'ngspice' or drop the field"
        )


def require_echo(
    analysis: dict[str, Any],
    corners: list[dict[str, Any]],
    backend: str,
    offhost: tuple[str, ...],
) -> None:
    """Fail when an off-host worker silently dropped ``analysis.op_check``.
    A worker that supports it adds ``op_check`` to every corner it ran; one
    built before the field returns none, and its corners would be graded
    without the check. Corners that never ran are exempt."""
    if backend not in offhost or not is_declared(analysis):
        return
    unrun = set(_UNRUN_MESSAGES)
    for corner in corners:
        if "op_check" in corner:
            continue
        if any(d.get("code") in unrun for d in corner.get("diagnostics") or []):
            continue
        raise SimError(
            "the remote/batch worker did not honor analysis.op_check -- "
            f"corner {corner.get('corner_id')!r} came back without an "
            "`op_check` field. A worker built before the field would grade "
            "the corner without the metastability check; update the runner "
            "image to this klt release, or use a local backend. No result "
            "was reported",
            code=OP_CHECK_UNSUPPORTED_WORKER_CODE,
        )
