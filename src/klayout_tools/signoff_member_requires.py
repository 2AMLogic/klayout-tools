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

    The request echo is not what every corner solved with: a
    ``corners.options`` axis writes its own ``.options`` card ahead of the
    body's and the request's (ngspice honors the first card that sets a
    name -- measured in ``tests/test_sim_options.py``), and each such corner
    reports it as ``corners[].options``. So every corner is checked too: one
    whose ``options`` sets a *required* option to another value fails the
    assertion, however the request echo reads. An override that agrees with
    the requirement, or one of an option the member does not require, is
    fine.

The assertion is an *additional* refusal: the member's own verdict, the
``rows`` it credits and every other check are unchanged. A shortfall grades
the item ``unmet`` / ``partial_coverage`` with ``detail.requires_unmet``, one
entry per failing member -- ``member``, plus ``missing_points`` (envelope
``corner_id`` spelling) when the grid fell short and ``ngspice_options`` =
``{"required", "observed"}`` when the solver profile did (``observed`` is
``null`` for an absent echo), with ``corner_overrides`` -- one
``{"corner_id", "option", "observed"}`` per conflicting corner option --
added only when a corner overrode a required option. A member without
``requires`` is untouched, so a manifest that never uses the key renders
byte-identically.

Malformed input is ``invalid_evidence`` with ``detail.problem``, never a
raise: a non-object, an unknown key, an empty ``requires``/axis/option map,
a non-numeric or non-finite axis value, a non-scalar option, ``requires`` on
any item other than 5 (:func:`misplaced_requires_problem`: refused whatever
the entry's shape, because an assertion the grader will not check must never
be dropped silently into a ``met``), and a Monte Carlo member under
``requires.grid`` (sampled corners echo their base point's PVT values, and
"a point counts only when every sample passed" is not a traced ``klt sim``
contract -- the same refusal ``joint_grid`` states).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from ._jsonnum import is_finite_number, is_number
from .signoff_base import _citation_label as citation_label
from .signoff_grid_axes import GRID_AXIS_KEYS, grid_axes_problem
from .signoff_joint_rows import _envelope_corners as envelope_corners
from .signoff_joint_rows import has_mc_corners, missing_grid_points
from .signoff_unmet_reasons import (
    _REASON_INVALID_EVIDENCE,
    _REASON_PARTIAL_COVERAGE,
    _REASON_UNGRADEABLE_BY_BUILD,
)

#: The one T1 item whose members accept ``requires``.
REQUIRES_ITEM_ID = 5

_REQUIRES_KEYS = frozenset({"grid", "ngspice_options"})

_Outcome = tuple[str, str | None, None, dict[str, Any], dict[str, Any]]


def has_requires(raw: Any) -> bool:
    """Whether a raw evidence entry carries a ``requires`` key (the routing
    test for a single, non-list citation, like ``covers``)."""
    return isinstance(raw, dict) and "requires" in raw


def misplaced_requires_problem(item_id: int, raw: Any) -> str | None:
    """The refusal for an item's raw evidence entry carrying ``requires`` on
    any item but 5, or ``None``. Item 6 and 7 keep their own mechanisms, and
    the other items have no solver profile or coordinate scope to bind.

    One gate for every entry shape -- a lone citation object, a list of them,
    and item 11's list of lists -- decided before any member is parsed,
    resolved or run. The shapes that never reach the multi-citation grader
    (a lone object on item 2, 3, 4, 7 or 8, or item 11's compound entry)
    otherwise normalize without the key and grade ``met`` with the assertion
    discarded, which a list-wrapped copy of the same object would not."""
    if item_id != REQUIRES_ITEM_ID and _carries_requires(raw):
        return f"'requires' is supported on item {REQUIRES_ITEM_ID} only"
    return None


def _carries_requires(raw: Any) -> bool:
    """:func:`has_requires` for an entry or any citation nested in lists."""
    if isinstance(raw, list):
        return any(_carries_requires(part) for part in raw)
    return has_requires(raw)


def pre_grading_refusal(
    item_id: int, raw: Any, graded_by_build: bool
) -> tuple[str, dict[str, Any]] | None:
    """The ``(reason, detail)`` of a cited item refused before any citation is
    normalized, resolved or run, or ``None`` when it goes on to be graded.

    Two refusals share this slot, both decided ahead of the evidence: an item
    this build has no rules for (``ungradeable_by_build``, issue #2176 --
    never run a command-backed entry whose answer this build could not
    interpret) and a ``requires`` on any item but 5 (issue #7807,
    :func:`misplaced_requires_problem`). An uncited item (``raw is None``) is
    refused by neither."""
    if raw is None:
        return None
    if not graded_by_build:
        return _REASON_UNGRADEABLE_BY_BUILD, {}
    if (problem := misplaced_requires_problem(item_id, raw)) is not None:
        return _REASON_INVALID_EVIDENCE, {"problem": problem}
    return None


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


def _corner_overrides(
    envelope: Mapping[str, Any] | None, required: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The corners whose own ``options`` set a *required* option to a value
    other than the required one, as ``{"corner_id", "option", "observed"}``
    in envelope order (options by name). Such a corner solved with its own
    ``.options`` card, not the request echo's value."""
    overrides: list[dict[str, Any]] = []
    for corner in envelope_corners(envelope) if isinstance(envelope, dict) else []:
        options = corner.get("options")
        if not isinstance(options, dict):
            continue
        overrides.extend(
            {
                "corner_id": corner.get("corner_id"),
                "option": name,
                "observed": options[name],
            }
            for name in sorted(options, key=str)
            if name in required and not _option_matches(options[name], required[name])
        )
    return overrides


def _options_shortfall(
    required: Mapping[str, Any], envelope: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The ``ngspice_options`` facet of a member's shortfall, ``None`` when
    the request echo carries every required option at its value **and** no
    corner overrides one (:func:`_corner_overrides`)."""
    observed = _observed_options(envelope)
    overrides = _corner_overrides(envelope, required)
    echoed = observed is not None and all(
        name in observed and _option_matches(observed[name], value)
        for name, value in required.items()
    )
    if echoed and not overrides:
        return None
    facet: dict[str, Any] = {"required": required, "observed": observed}
    if overrides:
        facet["corner_overrides"] = overrides
    return facet


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
        facet = _options_shortfall(requires["ngspice_options"], envelope)
        if facet is not None:
            shortfall["ngspice_options"] = facet
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
