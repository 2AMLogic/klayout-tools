"""The PVT-axis validator shared by every signoff "grid" declaration.

Two manifest forms name a PVT grid in the shape of a `klt sim` request's
``corners`` block (``process`` list, ``supply_v`` rails sweeping **zipped**
by index, ``temperature_c`` list):

- the ``joint_grid`` spec-row form (issue #6969, :mod:`.signoff_joint_rows`),
  a property of a *set* of benches; and
- the ``requires.grid`` member assertion (issue #7807,
  :mod:`.signoff_member_requires`), a property of *one* cited run.

They must accept and refuse exactly the same axis shapes, so the check lives
here once. This is a leaf module (it imports only :mod:`._jsonnum`): the
``joint_grid`` reader sits in :mod:`.signoff_justified_gap`, which
:mod:`.signoff_joint_rows` cannot be imported from without a cycle
(``signoff_joint_rows`` -> ``signoff_derived_rows`` ->
``signoff_justified_gap``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._jsonnum import is_number

#: The PVT axes a grid declaration may carry. An absent (or ``null``) axis
#: takes the request-side default when the required points are expanded
#: (:func:`.signoff_joint_rows.required_grid_points`).
GRID_AXIS_KEYS = frozenset({"process", "supply_v", "temperature_c"})


def _supply_problem(supply: Any) -> str | None:
    if not isinstance(supply, dict) or not supply:
        return "'supply_v' must be a non-empty object mapping rails to value lists"
    lengths: set[int] = set()
    for rail, values in supply.items():
        if (
            not isinstance(rail, str)
            or not rail
            or not isinstance(values, list)
            or not values
            or not all(is_number(v) for v in values)
        ):
            return f"'supply_v' rail {rail!r} must be a non-empty list of numbers"
        lengths.add(len(values))
    if len(lengths) != 1:  # rails sweep zipped, as in a sim request
        return "'supply_v' rails must list the same number of values"
    return None


def grid_axes_problem(raw: Mapping[str, Any]) -> str | None:
    """Why the PVT axes of ``raw`` are malformed, or ``None`` when every
    axis it carries is well formed. An absent or ``null`` axis is fine (the
    request-side default applies); a present one must be non-empty with
    well-typed values: ``process`` a list of non-empty strings,
    ``supply_v`` an object of equal-length numeric lists (one per rail),
    ``temperature_c`` a list of numbers (``bool`` is not a number). Keys
    other than the three axes are the caller's concern."""
    process = raw.get("process")
    if process is not None and (
        not isinstance(process, list)
        or not process
        or not all(isinstance(p, str) and p for p in process)
    ):
        return "'process' must be a non-empty list of non-empty strings"
    supply = raw.get("supply_v")
    if supply is not None and (problem := _supply_problem(supply)) is not None:
        return problem
    temperature = raw.get("temperature_c")
    if temperature is not None and (
        not isinstance(temperature, list)
        or not temperature
        or not all(is_number(t) for t in temperature)
    ):
        return "'temperature_c' must be a non-empty list of numbers"
    return None
