"""Pair each of *our* ``leakage_power`` states with the reference group that
covers it, for ``klt characterize --compare-to`` (issue #2503).

Each of our ``when`` conditions is expanded to the input assignments it
covers and the reference group whose own ``when`` holds under that assignment
is found. A vendor ``when`` may also name output pins (e.g. IHP's
``"A&!Y"``), so output values are computed from the reference cell's own
``function``. Pure: works on the ``CellData`` records of
:mod:`klayout_tools.liberty_compare` by attribute only.
"""

from __future__ import annotations

import itertools
from typing import Any

from .characterize_arcs import FunctionError, parse_function


def _reference_conditions(
    reference: Any, missing: list[str]
) -> list[tuple[str, float, Any]]:
    names = frozenset(reference.inputs) | frozenset(reference.outputs)
    conditions = []
    for when, value in reference.leakage:
        try:
            expr = parse_function(when, allowed_pins=names) if when else None
        except FunctionError:
            expr = None
            missing.append(
                f'leakage_power when "{when}": reference condition unparseable'
            )
        conditions.append((when, value, expr))
    return conditions


def _output_exprs(reference: Any) -> dict[str, Any]:
    exprs = {}
    for pin in reference.outputs:
        function = reference.functions.get(pin)
        if function is None:
            continue
        try:
            exprs[pin] = parse_function(
                function, allowed_pins=frozenset(reference.inputs)
            )
        except FunctionError:
            continue
    return exprs


def _matching_groups(
    ours: Any,
    ours_expr: Any,
    ref_conditions: list[tuple[str, float, Any]],
    output_exprs: dict[str, Any],
) -> tuple[int, set[int]]:
    """``(covered input assignments, indices of matching reference groups)``."""
    matches: set[int] = set()
    covered = 0
    for bits in itertools.product((False, True), repeat=len(ours.inputs)):
        assignment = dict(zip(ours.inputs, bits, strict=True))
        if not ours_expr.evaluate(assignment):
            continue
        covered += 1
        full = dict(assignment)
        for pin, expr in output_exprs.items():
            full[pin] = expr.evaluate(assignment)
        for index, (_, _, expr) in enumerate(ref_conditions):
            if expr is not None and _holds(expr, full):
                matches.add(index)
    return covered, matches


def match_leakage_states(
    ours: Any, reference: Any, missing: list[str]
) -> list[tuple[str, float, str, float]]:
    """``(our_when, our_value, reference_when, reference_value)`` for every
    state of ``ours`` that matches exactly one reference group; anything
    else is described in ``missing``."""
    pairs = []
    ref_conditions = _reference_conditions(reference, missing)
    output_exprs = _output_exprs(reference)

    for when, value in ours.leakage:
        try:
            ours_expr = parse_function(when, allowed_pins=frozenset(ours.inputs))
        except FunctionError:
            missing.append(f'leakage_power when "{when}": our condition unparseable')
            continue
        covered, matches = _matching_groups(
            ours, ours_expr, ref_conditions, output_exprs
        )
        if covered == 0 or len(matches) != 1:
            missing.append(
                f'leakage_power when "{when}": '
                + (
                    "no reference leakage_power group covers this state"
                    if not matches
                    else "matches more than one reference leakage_power group"
                )
            )
            continue
        ref_when, ref_value, _ = ref_conditions[matches.pop()]
        pairs.append((when, value, ref_when, ref_value))
    return pairs


def _holds(expr: Any, state: dict[str, bool]) -> bool:
    try:
        return bool(expr.evaluate(state))
    except KeyError:
        return False
