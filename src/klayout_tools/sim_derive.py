"""``measurements[].derive`` for ``klt sim`` (issue #7184): a deterministic
figure reduced **across the corners of one request along one swept axis**.

A ``derive`` entry is not measured inside any corner. It names a per-corner
source measurement, the swept axis to read it along, a closed reduction and
the remaining axes to group by::

    {"name": "code_range_rel",
     "derive": {"from": "vout", "axis": "params.code", "reduce": "range_rel",
                "reference": {"params.code": 128}, "group_by": ["process", "supply_v"]},
     "limits": {"min": 0.30}, "spec_row": "trim-range"}

That is the gradeable form for a spec row whose bounded quantity only exists
*between* corners -- a trim knob's range or step resolution, a box-method
temperature spread -- which a ``.meas`` card cannot express (it evaluates
inside one corner's run). ``limits``, ``plausible_range`` and ``spec_row``
apply as to any other measurement, so the entry rolls up, counts in
``coverage`` and credits a `klt signoff` item-5 row like one.

This module is the request side: the form's own validation
(:func:`validate_form_exclusive`), its validation against the expanded corner
grid (:func:`validate_grid`), the axis coordinates both sides share, and the
per-corner view of the spec list (:func:`per_corner_measurements`) that keeps
every deck byte-identical. :mod:`klayout_tools.sim_derive_rollup` evaluates
the entries once the corners are in.
"""

from __future__ import annotations

import re
from typing import Any

from ._jsonnum import is_finite_number
from .sim_errors import SimError
from .sim_forms import other_forms, validate_exclusive_form

#: The closed reduction set -- deliberately not an expression language.
#: ``range``: ``max - min`` over the group's axis points. ``range_rel``: that
#: span over a denominator (``reference`` or ``divisor``). ``box_spread``:
#: the span over ``((axis_max - axis_min) * denominator)``, the box method,
#: in ``1/<axis unit>``. ``max_abs_step``: the largest adjacent-setting
#: difference along the axis.
REDUCTIONS = ("range", "range_rel", "box_spread", "max_abs_step")
#: Axes with no order: only ``range`` and ``range_rel`` reduce along them.
UNORDERED_AXES = ("process", "variant")

_FIXED_AXES = ("process", "variant", "temperature_c", "supply_v")
_KEYED_AXES = ("supply_v", "params", "options")
_REQUIRED_KEYS = ("from", "axis", "reduce", "group_by")
_KEYS = (*_REQUIRED_KEYS, "reference", "divisor")
_DENOMINATED = ("range_rel", "box_spread")
_ORDERED = ("box_spread", "max_abs_step")
#: Entry-level keys that only mean something for a per-corner result, as
#: ``(key, refuse only a literal ``True``)``: a false flag is the default.
_REFUSED_SPEC_KEYS = (
    ("optional", True),
    ("intermediate", True),
    ("on_unreached", False),
    ("k_sigma", False),
)

#: Where :func:`validate_grid` records the axis's declared values on the spec,
#: so the rollup can tell an excluded point from a present one.
EXPECTED_KEY = "_derive_expected_axis_values"

#: Every other measurement form an entry must not also declare, derived from
#: the registry like every other exclusive-form module (#5695).
_FORMS = other_forms("derive")

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_AXES_HINT = (
    "'temperature_c', 'process', 'variant', 'supply_v', 'supply_v.<rail>', "
    "'params.<key>' or 'options.<key>'"
)
_EXAMPLE = '{"from": "vout", "axis": "params.code", "reduce": "range", "group_by": []}'


# --------------------------------------------------------------------------- #
# The form
# --------------------------------------------------------------------------- #


def is_derive(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``derive``."""
    return spec.get("derive") is not None


def derive_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``measurements[]`` entries declared with ``derive``."""
    return [spec for spec in measurements_spec if is_derive(spec)]


def per_corner_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every entry evaluated *inside* a corner's run: all but ``derive``.

    What deck generation, corner execution, the checkpoint fingerprint, the
    per-corner coverage rows and the off-host worker request consume."""
    return [spec for spec in measurements_spec if not is_derive(spec)]


def without_derive(request: dict[str, Any]) -> dict[str, Any]:
    """``request`` for an off-host worker: ``measurements[]`` without its
    ``derive`` entries, so a shard (which holds only part of the grid) and a
    runner image that predates the form both run the per-corner measurements
    exactly as before. Returns ``request`` itself when there is none."""
    measurements = request.get("measurements")
    if not isinstance(measurements, list) or not derive_measurements(measurements):
        return request
    return {**request, "measurements": per_corner_measurements(measurements)}


def validate_step_form(spec: dict[str, Any], step: str) -> None:
    """A step-scoped measurement's form (``analysis_steps[].measurements[]``):
    every per-corner form, but never ``derive`` -- a cross-corner reduction
    cannot run inside one corner's deck, so it belongs to the request's
    top-level ``measurements[]``."""
    from .sim_validate import _validate_measurement_form

    _validate_measurement_form(spec)
    if is_derive(spec):
        raise SimError(
            f"step {step!r}: measurement {spec['name']!r}: 'derive' reduces across "
            "the corners of a whole request -- declare it in the top-level "
            "measurements[] (its 'from' names '<step>.<name>')"
        )


def top_level_split(
    measurements_spec: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The top-level ``measurements[]`` of an ``analysis_steps[]`` request as
    ``(derive entries, per-corner entries)``. A derived ``expr`` that names a
    ``derive`` entry is refused: the figure is reduced after every corner has
    run, so it is no vector inside a corner's deck."""
    from .sim_steps import _BARE_NAME_RE, _TOKEN_RE

    derive = derive_measurements(measurements_spec)
    names = {str(spec["name"]).lower() for spec in derive}
    for spec in measurements_spec:
        bare = _TOKEN_RE.sub(" ", str(spec.get("expr") or ""))
        hit = next(
            (
                m.group(1)
                for m in _BARE_NAME_RE.finditer(bare)
                if m.group(1).lower() in names
            ),
            None,
        )
        if hit is not None:
            raise SimError(
                f"measurement {spec['name']!r} references derive measurement {hit!r}: "
                "a 'derive' entry is reduced after every corner has run, so it is no "
                "vector inside a corner's deck -- reference the per-corner "
                "'<step>.<measurement>' results"
            )
    return derive, per_corner_measurements(measurements_spec)


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``derive``: refuse any other form beside it and
    validate the body. Returns ``True`` when the entry is a ``derive`` entry
    (so the caller has nothing more to validate)."""
    return validate_exclusive_form(spec, "derive", _FORMS, validate_derive_spec)


def validate_derive_spec(name: Any, spec: dict[str, Any]) -> None:
    """The ``derive`` rules that need no corner grid; :func:`validate_grid`
    holds the ones that do. Normalizes ``axis``, ``group_by`` and the
    ``reference`` key in place."""
    for key, only_true in _REFUSED_SPEC_KEYS:
        value = spec.get(key)
        if value is None or (only_true and value is not True):
            continue
        raise SimError(
            f"measurement '{name}': '{key}' is not supported on a 'derive' "
            "measurement -- the figure is reduced after every corner has run, "
            "from per-corner results that carry their own such declarations"
        )
    body = spec["derive"]
    where = f"measurement '{name}': derive"
    if not isinstance(body, dict):
        raise SimError(f"{where} must be an object, e.g. {_EXAMPLE}")
    unknown = sorted(set(body) - set(_KEYS))
    if unknown:
        raise SimError(f"{where} has unknown key(s) {unknown}; allowed: {list(_KEYS)}")
    missing = [key for key in _REQUIRED_KEYS if key not in body]
    if missing:
        raise SimError(f"{where} requires {missing}")
    if not isinstance(body["from"], str) or not body["from"].strip():
        raise SimError(
            f"{where}.from must be the name of another measurement in this request"
        )
    body["axis"] = axis = _validate_axis_name(body["axis"], f"{where}.axis")
    _validate_reduce(body["reduce"], axis, where)
    body["group_by"] = _validate_group_by(body["group_by"], axis, where)
    _validate_denominator(body, axis, where)


def _validate_reduce(reduce: Any, axis: str, where: str) -> None:
    if reduce not in REDUCTIONS:
        raise SimError(
            f"{where}.reduce must be one of {list(REDUCTIONS)} (got {reduce!r})"
        )
    if reduce in _ORDERED and axis in UNORDERED_AXES:
        raise SimError(
            f"{where}: reduce '{reduce}' walks the axis in order, and {axis!r} has "
            "no order -- use 'range' or 'range_rel', or an ordered numeric axis "
            "('temperature_c', 'supply_v', 'params.<key>', 'options.<key>')"
        )


def _validate_group_by(value: Any, axis: str, where: str) -> list[str]:
    if not isinstance(value, list):
        raise SimError(
            f"{where}.group_by must be an array of the remaining swept axes "
            f"([] when the axis is the only swept one); got {value!r}"
        )
    label = f"{where}.group_by"
    axes = [_validate_axis_name(entry, label) for entry in value]
    if len(set(axes)) != len(axes):
        raise SimError(f"{label} repeats an axis: {axes}")
    if axis in axes:
        raise SimError(
            f"{label} names the reduced axis {axis!r}: it holds the remaining "
            "axes, one reduced entry per combination of their values"
        )
    return axes


def _validate_denominator(body: dict[str, Any], axis: str, where: str) -> None:
    """``range_rel``/``box_spread`` take exactly one of ``reference`` (the
    group's value at that axis point) or ``divisor`` (a positive constant);
    the other reductions take neither."""
    has_reference = body.get("reference") is not None
    has_divisor = body.get("divisor") is not None
    if body["reduce"] not in _DENOMINATED:
        if has_reference or has_divisor:
            raise SimError(
                f"{where}: 'reference' and 'divisor' only apply to "
                f"'range_rel' / 'box_spread'"
            )
        return
    if has_reference == has_divisor:
        raise SimError(
            f"{where}: reduce '{body['reduce']}' needs exactly one of "
            "'reference' ({\"<axis>\": <value>}: the group's value there is the "
            "denominator) or 'divisor' (a positive constant)"
        )
    if has_divisor:
        divisor = body["divisor"]
        if not is_finite_number(divisor) or divisor <= 0:
            raise SimError(
                f"{where}.divisor must be a positive finite number (got {divisor!r})"
            )
    else:
        _validate_reference(body, axis, where)


def _validate_reference(body: dict[str, Any], axis: str, where: str) -> None:
    reference = body["reference"]
    if not isinstance(reference, dict) or len(reference) != 1:
        raise SimError(
            f'{where}.reference must be exactly one {{"<axis>": <value>}} '
            f"(got {reference!r})"
        )
    ((ref_axis, value),) = reference.items()
    if _validate_axis_name(ref_axis, f"{where}.reference") != axis:
        raise SimError(
            f"{where}.reference names axis {ref_axis!r}, but derive.axis is "
            f"{axis!r}: the reference point lies on the reduced axis"
        )
    categorical = axis in UNORDERED_AXES
    valid = (
        isinstance(value, str) and bool(value)
        if categorical
        else is_finite_number(value)
    )
    if not valid:
        raise SimError(
            f"{where}.reference value must be a finite {axis} name"
            if categorical
            else f"{where}.reference value must be a finite {axis} (got {value!r})"
        )
    body["reference"] = {axis: value}


def _validate_axis_name(name: Any, where: str) -> str:
    """A normalized axis name: ``process``, ``variant``, ``temperature_c``,
    ``supply_v`` (a single-rail request), ``supply_v.<rail>``,
    ``params.<key>`` or ``options.<key>`` (keys case-insensitive)."""
    if not isinstance(name, str) or not name:
        raise SimError(f"{where} must be an axis name (got {name!r}): {_AXES_HINT}")
    head, dot, tail = name.partition(".")
    head = head.lower()
    if head not in _FIXED_AXES and head not in _KEYED_AXES:
        raise SimError(f"{where} names an unknown axis {name!r}; expected {_AXES_HINT}")
    if not dot:
        if head in ("params", "options"):
            raise SimError(f"{where} must name a key: '{head}.<key>'")
        return head
    if head not in _KEYED_AXES or not _IDENT_RE.fullmatch(tail):
        raise SimError(f"{where} is malformed (got {name!r}); expected {_AXES_HINT}")
    return f"{head}.{tail if head == 'supply_v' else tail.lower()}"


# --------------------------------------------------------------------------- #
# The corner grid
# --------------------------------------------------------------------------- #


def validate_grid(
    measurements_spec: list[dict[str, Any]],
    corner_points: list[Any],
    request: dict[str, Any],
) -> None:
    """Check every ``derive`` entry against the request's expanded corner
    grid, before any corner is dispatched (so ``--dry-run`` runs it too).

    The axis needs at least two declared values, every axis named must be on
    the grid, no two dispatched corners may share one ``(group, axis_value)``
    coordinate (a swept axis missing from ``group_by``), and a ``reference``
    point must be declared. A declared point that ``exclude`` removed is not
    an error here: it makes its group ``missing_axis_points`` at rollup -- the
    full-grid rule the corner merge already applies.
    """
    derive = derive_measurements(measurements_spec)
    if not derive:
        return
    _validate_sources(measurements_spec)
    if request.get("monte_carlo") is not None:
        raise SimError(
            f"measurement {derive[0]['name']!r}: 'derive' is not supported with "
            "monte_carlo -- a derived figure is reduced over the request's "
            "deterministic corner grid; sample statistics are the monte_carlo "
            "statistics block"
        )
    declared = _declared_points(request.get("corners") or {}, corner_points)
    for spec in derive:
        _validate_on_grid(spec, corner_points, declared)


def _validate_sources(measurements_spec: list[dict[str, Any]]) -> None:
    """``derive.from`` names a measurement that runs inside every corner --
    not a missing one, not the entry itself, not another ``derive`` entry."""
    sources = {str(spec["name"]) for spec in per_corner_measurements(measurements_spec)}
    for spec in derive_measurements(measurements_spec):
        source = spec["derive"]["from"]
        if source not in sources:
            raise SimError(
                f"measurement {spec['name']!r}: derive.from names {source!r}, which is "
                "not a per-corner measurement of this request (missing, this entry "
                "itself, or another 'derive' entry); name a measurement that runs "
                "inside each corner"
            )


def _declared_points(corners_spec: dict[str, Any], fallback: list[Any]) -> list[Any]:
    """Every declared corner point, before ``exclude`` (the union across
    ``corners.units``): where the axis's expected values come from."""
    from .sim_corners import expand_corners

    try:
        return expand_corners(corners_spec, [])
    except SimError:
        # Only the un-excluded grid collides (exclude kept one of a set that
        # shares a corner_id): the dispatched points are what was declared.
        return fallback


def _distinct(points: list[Any], axis: str) -> list[Any]:
    """A corner grid's values on ``axis``, sorted, each once."""
    values = {axis_value(axis, fields(point)) for point in points}
    return sorted(values)


def _validate_on_grid(
    spec: dict[str, Any], points: list[Any], declared: list[Any]
) -> None:
    body = spec["derive"]
    axis = body["axis"]
    expected = _distinct(declared, axis)
    if len(expected) < 2:
        raise SimError(
            f"measurement {spec['name']!r}: derive.axis {axis!r} has fewer than "
            f"two declared values ({expected}): a cross-corner reduction needs "
            "a swept axis with at least two points"
        )
    for group_axis in body["group_by"]:
        _distinct(points, group_axis)
    seen: dict[tuple[Any, ...], str] = {}
    for point in points:
        row = fields(point)
        coordinate = (
            *(axis_value(group_axis, row) for group_axis in body["group_by"]),
            axis_value(axis, row),
        )
        if coordinate in seen:
            raise SimError(
                f"measurement {spec['name']!r}: corners {seen[coordinate]!r} and "
                f"{point.corner_id!r} share one (group, {axis}) coordinate "
                f"{coordinate[-1]!r}: a swept axis is missing from "
                "derive.group_by -- add every axis that tells them apart"
            )
        seen[coordinate] = point.corner_id
    reference = body.get("reference")
    if reference is not None:
        (target,) = reference.values()
        if not any(_same_point(value, target) for value in expected):
            raise SimError(
                f"measurement {spec['name']!r}: derive.reference names "
                f"{axis}={target!r}, which the request does not declare "
                f"(declared: {expected})"
            )
    spec[EXPECTED_KEY] = expected


# --------------------------------------------------------------------------- #
# Axis coordinates
# --------------------------------------------------------------------------- #


def fields(source: Any) -> dict[str, Any]:
    """The axis-bearing coordinates of a :class:`~klayout_tools.sim.CornerPoint`
    or of a report's ``corners[]`` entry, under one shape."""
    get = source.get if isinstance(source, dict) else lambda key: getattr(source, key)
    return {
        "process": get("process"),
        "supply_v": get("supply_v") or {},
        "temperature_c": get("temperature_c"),
        "params": get("params") or {},
        "options": get("options") or {},
        "variant": get("variant"),
    }


def axis_value(axis: str, row: dict[str, Any]) -> Any:
    """One corner's coordinate on ``axis`` (``"default"`` for an absent
    process/variant, as ``corner_id`` spells it)."""
    head, _, tail = axis.partition(".")
    if head == "temperature_c":
        return float(row["temperature_c"])
    if head == "supply_v" and not tail:
        if len(row["supply_v"]) != 1:
            raise SimError(
                f"axis 'supply_v' is ambiguous for a request with several supply "
                f"rails ({sorted(row['supply_v'])}): name the rail, 'supply_v.<rail>'"
            )
        return next(iter(row["supply_v"].values()))
    if head in ("params", "options") or tail:
        if tail not in row[head]:
            raise SimError(
                f"axis {axis!r} is not on the corner grid ({head}: {sorted(row[head])})"
            )
        return row[head][tail]
    value = row[head]
    return "default" if value is None else value


def _same_point(a: Any, b: Any) -> bool:
    if is_finite_number(a) and is_finite_number(b):
        return float(a) == float(b)
    return a == b
