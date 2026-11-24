"""The ``requires`` evidence-member assertion on item 5 (issue #7807).

An item-5 citation pins the cited run's *request netlist* (``content_hash``),
and that artifact is invariant across the run's coordinate scope: a
2-coordinate smoke probe and the 45-coordinate matrix minted from the same
request ship a byte-identical netlist, so both grade ``met`` and either may
be cited in the other's place. The same holds for a sensitivity companion
minted at tighter solver tolerances. Nothing in the member shape bounded what
the cited run actually *covered*.

A member may now state what it must cover, and the grader checks that
against the cited envelope itself -- no ``klt sim`` change, the information
is already there::

    {"file": "sim/bench_a.json", "rows": ["gain"],
     "requires": {
       "grid": {"process": ["tt", "ss", "ff"],
                "supply_v": {"vdd": [1.62, 1.8, 1.98]},
                "temperature_c": [-40, 27, 125]},
       "ngspice_options": {"reltol": 0.001}}}

``requires.grid``
    The shape of a ``joint_grid`` row's axes (:mod:`.signoff_grid_axes`),
    without ``members``. The envelope's ``corners[]`` must carry every
    required point with every corner at it ``pass`` -- the one reader
    :func:`.signoff_joint_rows.missing_grid_points` the joint row uses, so a
    point swept twice must pass at *every* corner and ``27`` / ``27.0`` are
    one point.

``requires.ngspice_options``
    A literal **subset** check against the envelope's
    ``environment.ngspice_options`` (the options the run declared, which
    ``klt sim`` echoes only when it declared some). Every named option must
    be present and equal (numbers compare as numbers, a ``bool`` is never a
    number). An envelope with **no** echo cannot prove a tolerance profile,
    so an absent echo is unmet, never credited.

The assertion is an *additional* refusal: the member's own verdict, the
``rows`` it credits and every other check are unchanged. A shortfall grades
the item ``unmet`` / ``partial_coverage`` with ``detail.requires_unmet``, one
entry per failing member -- ``member``, plus ``missing_points`` (envelope
``corner_id`` spelling) when the grid fell short and ``ngspice_options`` =
``{"required", "observed"}`` when the solver profile did (``observed`` is
``null`` for an absent echo). A member without ``requires`` is untouched, so
a manifest that never uses the key renders byte-identically.

Malformed input is ``invalid_evidence`` with ``detail.problem``, never a
raise: a non-object, an unknown key, an empty ``requires``/axis/option map,
a non-numeric axis value, a non-scalar option, ``requires`` on any item
other than 5, and a Monte Carlo member under ``requires.grid`` (sampled
corners echo their base point's PVT values, and "a point counts only when
every sample passed" is not a traced ``klt sim`` contract -- the same
refusal ``joint_grid`` states).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from ._jsonnum import is_finite_number, is_number
from .signoff_base import _citation_label as citation_label
from .signoff_grid_axes import GRID_AXIS_KEYS, grid_axes_problem
from .signoff_joint_rows import has_mc_corners, missing_grid_points
from .signoff_unmet_reasons import _REASON_INVALID_EVIDENCE, _REASON_PARTIAL_COVERAGE

#: The one T1 item whose members accept ``requires``.
REQUIRES_ITEM_ID = 5

_REQUIRES_KEYS = frozenset({"grid", "ngspice_options"})

_Outcome = tuple[str, str | None, None, dict[str, Any], dict[str, Any]]


def has_requires(raw: Any) -> bool:
    """Whether a raw evidence entry carries a ``requires`` key (the routing
    test for a single, non-list citation, like ``covers``)."""
    return isinstance(raw, dict) and "requires" in raw


def _unknown_keys_problem(name: str, keys: Any) -> str:
    return f"{name} has an unknown key: " + ", ".join(sorted(map(str, keys)))


def _grid_problem(grid: Any) -> str | None:
    if not isinstance(grid, dict):
        return "'requires.grid' must be an object of PVT axes"
    if unknown := set(grid) - GRID_AXIS_KEYS:
        return _unknown_keys_problem("'requires.grid'", unknown)
    if all(grid.get(axis) is None for axis in GRID_AXIS_KEYS):
        return "'requires.grid' must declare at least one axis"
    problem = grid_axes_problem(grid)
    return None if problem is None else f"'requires.grid' {problem}"


def _option_value_ok(value: Any) -> bool:
    return (
        isinstance(value, bool)
        or is_finite_number(value)
        or (isinstance(value, str) and bool(value))
    )


def _options_problem(options: Any) -> str | None:
    if not isinstance(options, dict) or not options:
        return "'requires.ngspice_options' must be a non-empty object"
    for name, value in options.items():
        if not isinstance(name, str) or not name or not _option_value_ok(value):
            return (
                f"'requires.ngspice_options' option {name!r} must be a "
                "finite number, a boolean or a non-empty string"
            )
    return None


def read_requires(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize one member's ``requires`` object to ``(requires, None)``,
    or ``(None, <problem>)`` when it is malformed. The problem names the
    offending key and starts with it (the caller prefixes the member), so it
    reads like ``_parse_multi_members``' own ``'rows'`` refusals."""
    if not isinstance(raw, dict):
        return None, "'requires' must be an object"
    if unknown := set(raw) - _REQUIRES_KEYS:
        return None, _unknown_keys_problem("'requires'", unknown)
    if not raw:
        return None, "'requires' must declare 'grid' and/or 'ngspice_options'"
    for key, check in (("grid", _grid_problem), ("ngspice_options", _options_problem)):
        if key in raw and (problem := check(raw[key])) is not None:
            return None, problem
    return dict(raw), None


def requires_item_problem(
    members: list[tuple[dict[str, Any], list[str] | None]], requires_item: bool
) -> str | None:
    """The refusal for parsed ``(spec, rows)`` members carrying ``requires``
    on any item but 5 (item 6 and 7 keep their own mechanisms, and the
    per-cell report lists have no solver profile to bind); ``None``
    otherwise. Checked before any member is graded or run."""
    if not requires_item and any("requires" in spec for spec, _rows in members):
        return f"'requires' is supported on item {REQUIRES_ITEM_ID} only"
    return None


def _option_matches(observed: Any, required: Any) -> bool:
    """Equality of one solver option: a ``bool`` only equals the same
    ``bool`` (``True == 1`` in Python, not in JSON), numbers compare as
    numbers (``0.001`` and ``1e-3`` are one value), strings literally."""
    if isinstance(required, bool) or isinstance(observed, bool):
        return observed is required
    if is_number(required):
        return is_number(observed) and observed == required
    return observed == required


def _observed_options(envelope: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The envelope's ``environment.ngspice_options`` echo, ``None`` when it
    carries none (`klt sim` echoes it only when options were declared)."""
    environment = (envelope or {}).get("environment")
    echo = environment.get("ngspice_options") if isinstance(environment, dict) else None
    return echo if isinstance(echo, dict) and echo else None


def _member_shortfall(
    requires: Mapping[str, Any], envelope: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The failing facets of one member's ``requires`` (``{}`` = satisfied)."""
    shortfall: dict[str, Any] = {}
    if "grid" in requires:
        missing = missing_grid_points(envelope, requires["grid"])
        if missing:
            shortfall["missing_points"] = missing
    if "ngspice_options" in requires:
        required = requires["ngspice_options"]
        observed = _observed_options(envelope)
        if observed is None or not all(
            name in observed and _option_matches(observed[name], value)
            for name, value in required.items()
        ):
            shortfall["ngspice_options"] = {"required": required, "observed": observed}
    return shortfall


class RequiresOutcome(NamedTuple):
    """What :func:`evaluate_requires` decided: the ``invalid_evidence``
    ``problem`` (or ``None``) and the ``requires_unmet`` entries."""

    problem: str | None
    unmet: list[dict[str, Any]]


def evaluate_requires(
    graded: list[Any], met_envelopes: Mapping[int, Mapping[str, Any]] | None
) -> RequiresOutcome:
    """Check every ``graded`` ``(spec, rows, outcome)`` member that carries
    ``requires`` against the envelope its grading captured. A member with no
    captured envelope proves nothing (every point missing, no echo)."""
    unmet: list[dict[str, Any]] = []
    for spec, _rows, _outcome in graded:
        requires = spec.get("requires")
        if requires is None:
            continue
        name = citation_label(spec)
        envelope = (met_envelopes or {}).get(id(spec))
        if "grid" in requires and has_mc_corners(envelope):
            return RequiresOutcome(
                f"'requires.grid' names a Monte Carlo member, which is "
                f"unsupported: {name}",
                [],
            )
        shortfall = _member_shortfall(requires, envelope)
        if shortfall:
            unmet.append({"member": name, **shortfall})
    return RequiresOutcome(None, unmet)


def requires_outcome(
    graded: list[Any],
    met_envelopes: Mapping[int, Mapping[str, Any]] | None,
    row_info: dict[str, Any],
    extra: dict[str, Any],
) -> _Outcome | None:
    """The ``unmet`` outcome a failed ``requires`` decides, in
    :func:`signoff._grade_multi_citation`'s tuple shape; ``None`` when no
    member carries one or every one is satisfied."""
    outcome = evaluate_requires(graded, met_envelopes)
    if outcome.problem is not None:
        invalid: dict[str, Any] = {"problem": outcome.problem}
        return "unmet", _REASON_INVALID_EVIDENCE, None, invalid, extra
    if outcome.unmet:
        partial = {**row_info, "requires_unmet": outcome.unmet}
        return "unmet", _REASON_PARTIAL_COVERAGE, None, partial, extra
    return None
