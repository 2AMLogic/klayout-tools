"""The ``measurements[].derive`` rollup for ``klt sim`` (issue #7184).

:func:`rollup_fields` evaluates one ``derive`` entry over the reported
corners -- one record per group of the remaining axes -- and
:func:`attach_coverage` turns those records into per-group checked-work
identities. The form itself (request validation, the axis coordinates, the
per-corner spec split) is :mod:`klayout_tools.sim_derive`. Derived values
use the same scalar grading rules as per-corner measurements.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_finite_number
from .coverage import work_id
from .sim_corner_point import _format_number, _format_param_value, _format_supply_value
from .sim_derive import EXPECTED_KEY, axis_value, fields, is_derive
from .sim_errors import SimError
from .sim_measurement_grading import (
    _ABSENT_STATUS,
    _INCONCLUSIVE_STATUS,
    _RECOGNISED_LIMIT_KEYS,
    IMPLAUSIBLE_DIAGNOSTIC_CODE,
    NOT_REACHED_STATUS,
    _grade_measurement_value,
)

#: Why a group was not graded (its ``reason``); a graded group carries ``null``.
REASON_MISSING_AXIS_POINTS = "missing_axis_points"
REASON_SOURCE_UNAVAILABLE = "source_unavailable"
REASON_ZERO_REFERENCE = "zero_reference"
REASON_NUMERIC_RANGE = "numeric_range"
REASON_SOURCE_INCONCLUSIVE = "source_inconclusive"
REASON_IMPLAUSIBLE_VALUE = "implausible_value"

Point = tuple[dict[str, Any], Any]  # (corner report, its position on the axis)


def rollup_fields(
    spec: dict[str, Any], corners: list[dict[str, Any]]
) -> dict[str, Any]:
    """The fields a ``derive`` spec lays over its rollup entry: ``status``,
    ``worst_case``, ``min``, ``max`` and the ``derive`` block. ``{}`` for
    every other measurement, so a caller spreads it into any spec's entry."""
    if not is_derive(spec):
        return {}
    body = spec["derive"]
    axis, group_by = body["axis"], body["group_by"]
    groups: dict[tuple[Any, ...], list[Point]] = {}
    observed: set[Any] = set()
    for corner in corners:
        row = fields(corner)
        position = axis_value(axis, row)
        observed.add(position)
        key = tuple(axis_value(group_axis, row) for group_axis in group_by)
        groups.setdefault(key, []).append((corner, position))
    expected = spec.get(EXPECTED_KEY)
    if expected is None:  # a caller that skipped validate_grid: what ran, ran
        expected = sorted(observed)
    records = [
        _reduce_group(spec, dict(zip(group_by, key, strict=True)), points, expected)
        for key, points in groups.items()
    ]
    return {**_summary(records), "derive": _echo(body, expected, records)}


def _source(corner: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (m for m in corner.get("measurements") or [] if m.get("name") == name), None
    )


def _value(corner: dict[str, Any], name: str) -> Any:
    """The source measurement's value -- present once :func:`_untrusted` has
    accepted the corner."""
    measurement = _source(corner, name)
    assert measurement is not None
    return measurement["value"]


def _reduce_group(
    spec: dict[str, Any],
    group: dict[str, Any],
    points: list[Point],
    expected: list[Any],
) -> dict[str, Any]:
    """One group's record. A group that cannot be trusted to grade is
    ``error``/``inconclusive`` with a ``reason`` -- never a pass, never a
    substituted number."""
    body = spec["derive"]
    record: dict[str, Any] = {
        "group_id": _group_id(body["group_by"], group),
        "group": group,
        "status": "error",
        "value": None,
        "margin": None,
        "reason": None,
    }
    missing = [value for value in expected if value not in {p for _, p in points}]
    if missing:
        return {
            **record,
            "reason": REASON_MISSING_AXIS_POINTS,
            "missing_axis_values": missing,
        }
    reason = _untrusted(points, body["from"])
    if reason is not None:
        inconclusive = reason == REASON_SOURCE_INCONCLUSIVE
        return {
            **record,
            "status": _INCONCLUSIVE_STATUS if inconclusive else "error",
            "reason": reason,
        }
    value, reason = _reduce(body, points)
    if reason is not None:
        return {**record, "reason": reason}
    if not is_finite_number(value):
        return {**record, "reason": REASON_NUMERIC_RANGE}
    status, margin, diagnostics = _grade_measurement_value(spec, value)
    if margin is not None and not is_finite_number(margin):
        return {**record, "reason": REASON_NUMERIC_RANGE}
    if status == _INCONCLUSIVE_STATUS:
        return {
            **record,
            "status": status,
            "value": value,
            "reason": REASON_IMPLAUSIBLE_VALUE,
            "diagnostics": diagnostics,
        }
    return {**record, "status": status, "value": value, "margin": margin}


def _untrusted(points: list[Point], source: str) -> str | None:
    """Why the group's inputs cannot be reduced: a missing, errored,
    unreached, absent or non-finite source value (``source_unavailable``),
    else a corner or source graded ``inconclusive`` (``source_inconclusive``);
    ``None`` when every input is trusted. An error outranks an inconclusive."""
    unavailable = ("error", NOT_REACHED_STATUS, _ABSENT_STATUS)
    inconclusive = False
    for corner, _ in points:
        measurement = _source(corner, source)
        if (
            measurement is None
            or corner.get("status") == "error"
            or measurement.get("status") in unavailable
            or not is_finite_number(measurement.get("value"))
        ):
            return REASON_SOURCE_UNAVAILABLE
        inconclusive = inconclusive or _INCONCLUSIVE_STATUS in (
            corner.get("status"),
            measurement.get("status"),
        )
    return REASON_SOURCE_INCONCLUSIVE if inconclusive else None


def _reduce(
    body: dict[str, Any], points: list[Point]
) -> tuple[float | None, str | None]:
    """``(value, reason)``: the reduction in double precision over the
    group's trusted values in axis order, or a reason it cannot be graded."""
    ordered = sorted(points, key=lambda point: point[1])
    values = [_value(corner, body["from"]) for corner, _ in ordered]
    span = max(values) - min(values)
    reduce = body["reduce"]
    if reduce == "range":
        return span, None
    if reduce == "max_abs_step":
        steps = [b - a for a, b in zip(values, values[1:], strict=False)]
        return max(map(abs, steps)), None
    denominator, reason = _denominator(body, ordered)
    if denominator is None:
        return None, reason
    if reduce == "box_spread":
        axis_span = ordered[-1][1] - ordered[0][1]
        if not is_finite_number(axis_span):
            return None, REASON_NUMERIC_RANGE
        denominator *= axis_span
    # Finite inputs can still overflow the span or round the scaled divisor
    # to zero or infinity. Infinity as a divisor would give a spurious zero.
    if (
        not is_finite_number(span)
        or not is_finite_number(denominator)
        or denominator == 0
    ):
        return None, REASON_NUMERIC_RANGE
    return span / denominator, None


def _denominator(
    body: dict[str, Any], points: list[Point]
) -> tuple[float | None, str | None]:
    """``abs(value at the reference point)`` or the constant ``divisor``."""
    if body.get("divisor") is not None:
        return float(body["divisor"]), None
    (target,) = body["reference"].values()
    for corner, position in points:
        if position == target or _same_number(position, target):
            reference = abs(_value(corner, body["from"]))
            return (
                (None, REASON_ZERO_REFERENCE) if reference == 0 else (reference, None)
            )
    raise SimError(
        f"derive.reference names {body['axis']}={target!r}, which no corner of "
        "this group sits on"
    )


def _same_number(a: Any, b: Any) -> bool:
    return is_finite_number(a) and is_finite_number(b) and float(a) == float(b)


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """``status`` (``error > inconclusive > fail > pass`` across groups),
    ``worst_case`` (smallest margin; a ``null`` margin is worst of all, as in
    ``sim._rollup_measurements``) and the ``min``/``max`` group over finite
    values. ``corner_id`` repeats the ``group_id`` so every reader of a
    per-corner rollup entry keeps working."""

    def _rank(record: dict[str, Any]) -> float:
        margin = record["margin"]
        return margin if margin is not None else float("-inf")

    def _point(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "corner_id": record["group_id"],
            "group_id": record["group_id"],
            "value": record["value"],
        }

    def _extreme(pick: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if pick is None else _point(pick)

    def _by_value(record: dict[str, Any]) -> Any:
        return record["value"]

    statuses = {record["status"] for record in records}
    status = next(
        (
            token
            for token in ("error", _INCONCLUSIVE_STATUS, "fail")
            if token in statuses
        ),
        "pass",
    )
    worst = min(records, key=_rank, default=None)
    finite = [record for record in records if is_finite_number(record["value"])]
    worst_case = None
    if worst is not None:
        worst_case = {
            **_point(worst),
            "group": worst["group"],
            "margin": worst["margin"],
        }
    return {
        "status": status,
        "worst_case": worst_case,
        "min": _extreme(min(finite, key=_by_value, default=None)),
        "max": _extreme(max(finite, key=_by_value, default=None)),
    }


def _echo(
    body: dict[str, Any], expected: list[Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    """The entry's ``derive`` block: the request as normalized, the axis's
    expected values and one record per group."""
    echo = {key: body[key] for key in ("from", "axis", "reduce", "group_by")}
    for key in ("reference", "divisor"):
        if body.get(key) is not None:
            echo[key] = body[key]
    return {**echo, "expected_axis_values": list(expected), "groups": records}


def _format_axis_value(axis: str, value: Any) -> str:
    """A coordinate spelled the way ``corner_id`` spells it."""
    head = axis.partition(".")[0]
    if head in ("process", "variant"):
        return str(value)
    if head == "temperature_c":
        return _format_number(value)
    if head == "supply_v":
        return f"{_format_supply_value(value)}V"
    return _format_param_value(value)


def _group_id(group_by: list[str], group: dict[str, Any]) -> str:
    """``process=a/supply_v=1.000V``, or ``all`` with nothing to group by."""
    if not group_by:
        return "all"
    return "/".join(
        f"{axis}={_format_axis_value(axis, group[axis])}" for axis in group_by
    )


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


def attach_coverage(
    measurements: list[dict[str, Any]],
    checked: list[str],
    skipped: list[dict[str, str]],
    inapplicable: list[dict[str, str]],
) -> None:
    """Append checked-work records from each derived measurement's graded groups."""
    for entry in measurements:
        if not is_derive(entry):
            continue
        limits = entry.get("limits")
        if not isinstance(limits, dict):
            limits = {}
        for group in entry["derive"]["groups"]:
            _group_coverage(
                entry["name"], limits, group, checked, skipped, inapplicable
            )


def _group_coverage(
    name: str,
    limits: dict[str, Any],
    group: dict[str, Any],
    checked: list[str],
    skipped: list[dict[str, str]],
    inapplicable: list[dict[str, str]],
) -> None:
    """The ``sim._measurement_coverage`` rules, per group: a
    ``derived_measurement`` observation and one ``derived_limit`` per bound
    applied; an ungraded group skips them, naming why."""
    group_id = group["group_id"]
    observation = work_id("derived_measurement", name, group_id) + "/observation"
    if not limits:
        inapplicable.append(
            {"id": observation + "/limits", "reason": "characterization_without_limits"}
        )
        if group["value"] is None:
            observation_reason = (
                REASON_SOURCE_INCONCLUSIVE
                if group["reason"] == REASON_SOURCE_INCONCLUSIVE
                else "unavailable_measurement"
            )
            skipped.append({"id": observation, "reason": observation_reason})
        else:
            checked.append(observation)
    for key, value in limits.items():
        bound = work_id("derived_limit", name, group_id, key)
        reason = _bound_skip_reason(key, value, group)
        if reason is None:
            checked.append(bound)
        else:
            skipped.append({"id": bound, "reason": reason})


def _bound_skip_reason(key: str, value: Any, group: dict[str, Any]) -> str | None:
    if key not in _RECOGNISED_LIMIT_KEYS:
        return "unrecognized_limit_key"
    if value is None:
        return "missing_limit_value"
    if group["status"] == "error":
        return "unavailable_measurement"
    if group["status"] == _INCONCLUSIVE_STATUS:
        return (
            IMPLAUSIBLE_DIAGNOSTIC_CODE
            if group["reason"] == REASON_IMPLAUSIBLE_VALUE
            else REASON_SOURCE_INCONCLUSIVE
        )
    return None
