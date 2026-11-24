"""The ``joint_grid`` spec-row form (issue #6969): one row covered only by
a *set* of benches.

Derived row coverage (#3778) reads ``spec_row`` tags from a **single** cited
envelope's ``measurements[]``. A coverage-style row -- "every implemented
bench exercised the full ratified PVT grid, with the supply and temperature
verified as applied" -- is a property of one envelope *per bench*: citing
any one envelope with ``rows: [...]`` for such a row would assert the joint
property on one bench's evidence, so the only choices were that false claim
or declaring a ``gap`` even when every bench's envelope proves its half.

The ``joint_grid`` row form closes that. A declared row may carry

``{"id": "pvt_grid_all_benches", "joint_grid": {"members": [...],
"process": [...], "supply_v": {rail: [...]}, "temperature_c": [...]}}``

(item 5 only, bare or partition-qualified -- item 6 shares the reader but
not the form). ``members`` names citations of the same evidence list,
matched by :func:`citation_label` (the citation's ``file``, or its joined
``command``); it is explicit, so dropping a bench from the evidence cannot
silently shrink the joint claim. The grid uses the same shape as a `klt
sim` request's ``corners`` block -- supply rails sweep **zipped** by index,
rails move as a set -- and the required points are its odometer product
(process x supply x temperature), with the request-side defaults for an
absent axis (``process`` ``None``, no supply rail, ``27`` C).

The row is ``covered`` (and ``row_sources[row] = "derived"``, #3778's
reader) only when **every** named member graded ``met`` through the
ordinary per-citation path **and**:

1. its envelope's ``corners[]`` contains every required grid point, every
   corner at that point ``pass`` (a point swept twice -- a ``params`` axis
   -- must pass at *every* corner, not one);
2. it carries at least one **condition probe**: a passing, ``limits``
   -bearing ``measurements[]`` entry tagged ``spec_row: <row id>`` -- the
   #3778 tag on `klt sim`'s rolled-up measurements, whose ``status`` is
   ``pass`` only when the measurement passed at every corner that reported
   it, so no new sim field is needed.

Otherwise the row is uncovered (``uncovered_rows``, item ``unmet`` /
``partial_coverage``) and ``detail.joint_rows`` lists, per member with a
shortfall, the ``missing_points`` (labels in the envelope's own
``corner_id`` PVT spelling) and whether the probe is ``missing``. A joint
row is never assertable: a citation naming one in its own ``rows`` is
``invalid_evidence`` (:func:`_members_rows_declared`), which is exactly the
one-bench false claim this form exists to prevent.

``joint_grid`` and ``gap`` are mutually exclusive on a row; a malformed
``joint_grid`` (not an object, an unknown key, an empty, shorter-than-two
or duplicate-bearing ``members``, a non-string member, an empty axis, a
non-numeric axis value, supply rails of differing lengths) degrades item 5
alone to ``invalid_evidence``, never raises -- the same contract every
other ``spec_rows`` form follows. So does a member name no citation of the
item carries, and a Monte Carlo member: sampled corners echo their base
point's ``process``/``supply_v``/``temperature_c``, but "a grid point
counts only when every sample drawn at it passed" is not a traced
``klt sim`` contract, so an MC member is stated unsupported rather than
guessed at.

The module also hosts ``_members_rows_declared`` (moved from
:mod:`.signoff_spec_row_table` as the size offset that pays for this
form's hook lines there; the naming rule now spans the free-text gap, the
justified gap and the joint forms), re-imported by its previous home so
``signoff_spec_row_table._members_rows_declared`` keeps resolving exactly
as before the split. ``citation_label`` is :mod:`.signoff_base`'s
``_citation_label`` (its home on ``main``), imported here under the public
spelling the joint readers use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._jsonnum import is_number
from .signoff_base import _citation_label as citation_label
from .signoff_derived_rows import envelope_tagged_rows


def _multi_citation_rows(
    report_list: bool,
    spec_rows: list[dict[str, Any]] | None,
    spec_rows_invalid: bool,
) -> tuple[list[dict[str, Any]] | None, bool]:
    """``(spec_rows, spec_rows_invalid)`` as :func:`_grade_multi_citation`
    sees them: a plain per-cell report list ignores ``spec_rows`` entirely.
    Moved from :mod:`.signoff` (issue #6969) for its size ceiling;
    ``signoff._multi_citation_rows`` keeps resolving through the re-import."""
    return (None, False) if report_list else (spec_rows, spec_rows_invalid)


def _members_rows_declared(
    members: list[tuple[dict[str, Any], list[str] | None]],
    spec_rows: list[dict[str, Any]] | None,
) -> bool:
    """True unless a member names an undeclared row, a declared-``gap`` row
    (either form, issue #5599), or a ``joint_grid`` row (issue #6969 -- a
    joint row is derived from the named members or uncovered, never covered
    on one citation's word). Moved from ``signoff_spec_row_table.py``."""
    if spec_rows is None:
        return True
    declared = {row["id"]: row for row in spec_rows}
    return all(
        row_id in declared
        and declared[row_id]["gap"] is None
        and declared[row_id]["justified_gap"] is None
        and declared[row_id]["joint_grid"] is None
        and declared[row_id].get("structural") is None
        for _, rows in members
        for row_id in rows or []
    )


def _format_supply_value(value: int | float) -> str:
    """Mirror `klt sim`'s corner-id rail spelling (``1.800``): the legacy
    three-decimal form when it round-trips, else the shortest round-trip
    decimal. Not imported from ``sim_corner_point`` (the sim layer stays
    off the signoff layer's imports); a test pins the two together."""
    legacy = f"{float(value):.3f}"
    if float(legacy) == float(value):
        return legacy
    return repr(value)


def _format_temperature(value: int | float) -> str:
    """Mirror `klt sim`'s corner-id temperature spelling: an int-valued
    number renders without the trailing ``.0``."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _point_label(process: str | None, supply: Mapping[str, float], temp: float) -> str:
    """One required grid point's label, in the envelope's own ``corner_id``
    PVT spelling (``tt/1.800V/-40C``), so a listed missing point greps
    straight against the cited report."""
    supply_label = (
        "novdd"
        if not supply
        else (
            f"{_format_supply_value(next(iter(supply.values())))}V"
            if len(supply) == 1
            else "_".join(
                f"{rail}={_format_supply_value(value)}"
                for rail, value in sorted(supply.items())
            )
            + "V"
        )
    )
    return (
        f"{process if process is not None else 'default'}/"
        f"{supply_label}/{_format_temperature(temp)}C"
    )


def required_grid_points(joint: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    """The grid a joint member must cover: the odometer product
    ``process x supply x temperature`` of the declared axes, in the axis
    order `klt sim` itself expands (process outermost, temperature
    innermost of the PVT three). An absent axis takes the request-side
    default -- ``process`` ``None`` (no process axis), an empty supply
    assignment, ``27`` C -- exactly as a ``corners`` block collapses a
    missing axis, so a supply-and-temperature grid declares only the axes
    it constrains."""
    processes: list[str | None] = joint.get("process") or [None]
    supply_spec = joint.get("supply_v") or {}
    if supply_spec:
        (count,) = {len(values) for values in supply_spec.values()}
        supply_points = [
            {rail: values[i] for rail, values in supply_spec.items()}
            for i in range(count)
        ]
    else:
        supply_points = [{}]
    temperatures = joint.get("temperature_c") or [27]
    return [
        (process, supply, temp)
        for process in processes
        for supply in supply_points
        for temp in temperatures
    ]


def _number_matches(actual: Any, required: float) -> bool:
    """Numeric equality after JSON parse (``27`` and ``27.0`` are one
    number); booleans are not numbers."""
    return is_number(actual) and actual == required


def _corner_at(corner: Mapping[str, Any], point: tuple[Any, ...]) -> bool:
    """Whether ``corner`` sits at the required grid ``point``: same
    ``process`` (``null`` for a process-less corner), same temperature,
    and every grid-named supply rail at the point's value -- rails the grid
    does not name do not disqualify it, the grid speaks only for its own."""
    process, supply, temp = point
    if corner.get("process") != process or not _number_matches(
        corner.get("temperature_c"), temp
    ):
        return False
    rails = corner.get("supply_v")
    return isinstance(rails, dict) and all(
        _number_matches(rails.get(rail), value) for rail, value in supply.items()
    )


def _envelope_corners(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    corners = envelope.get("corners")
    return (
        [c for c in corners if isinstance(c, dict)] if isinstance(corners, list) else []
    )


def has_mc_corners(envelope: Mapping[str, Any] | None) -> bool:
    """Whether any corner of ``envelope`` is a Monte Carlo sample (its
    ``monte_carlo`` block is set): the whole run was sample-expanded, and
    per-sample grid semantics are not a traced contract (issue #6969's
    out-of-scope note) -- such a member is refused, never guessed at."""
    return envelope is not None and any(
        c.get("monte_carlo") is not None for c in _envelope_corners(envelope)
    )


def member_shortfall(
    envelope: Mapping[str, Any] | None,
    joint: Mapping[str, Any],
    row_id: str,
) -> tuple[list[str], bool]:
    """``(missing_point_labels, probe_missing)`` for one member's envelope
    against the declared grid and the row's condition probe. An unavailable
    envelope misses every point and the probe: nothing else proves them."""
    points = required_grid_points(joint)
    if not isinstance(envelope, dict):
        return [_point_label(*point) for point in points], True
    corners = _envelope_corners(envelope)
    missing: list[str] = []
    for point in points:
        at_point = [c for c in corners if _corner_at(c, point)]
        # A point swept more than once (a params/options axis) must pass at
        # *every* corner sitting on it, not at any one of them.
        if not at_point or not all(c.get("status") == "pass" for c in at_point):
            missing.append(_point_label(*point))
    _has_tags, covering = envelope_tagged_rows(envelope)
    return missing, row_id not in covering


class JointOutcome:
    """What :func:`evaluate_joint_rows` decided about a declared row set's
    joint rows: the ``invalid_evidence`` ``problem`` (or ``None``), the row
    ids whose joint property every member proved (credited ``derived`` by
    :func:`~.signoff_derived_rows.multi_row_info`), and the per-member
    ``detail.joint_rows`` entries of the rows that fell short."""

    def __init__(self, problem: str | None, covered: frozenset[str], rows: list[dict]):
        self.problem = problem
        self.covered = covered
        self.rows = rows


def evaluate_joint_rows(
    graded: list[Any],
    spec_rows: list[dict[str, Any]] | None,
    met_envelopes: dict[int, dict[str, Any]] | None,
) -> JointOutcome:
    """Grade every declared ``joint_grid`` row against the graded members
    (:func:`signoff._grade_multi_citation`'s hook, issue #6969).

    A named member no citation carries, or a Monte Carlo member, is a
    ``problem`` (the item degrades to ``invalid_evidence``). A member that
    graded ``met`` is checked against its captured envelope's grid and
    probe; one that graded anything else misses every point -- its envelope
    is not evidence for the joint property even when it was captured
    (#6027's credit is a per-row affair, this one is per-bench). Entries
    exist only for members with a shortfall; a row with none is covered.
    """
    joints = [
        (row["id"], row["joint_grid"])
        for row in (spec_rows or [])
        if row["joint_grid"] is not None
    ]
    if not joints:
        return JointOutcome(None, frozenset(), [])
    covered: set[str] = set()
    entries: list[dict[str, Any]] = []
    for row_id, joint in joints:
        row_clean = True
        for name in joint["members"]:
            matches = [
                (spec, g) for spec, _rows, g in graded if citation_label(spec) == name
            ]
            if not matches:
                return JointOutcome(
                    f"joint_grid row '{row_id}' names an uncited member: {name}",
                    frozenset(),
                    [],
                )
            for spec, g in matches:
                envelope = (
                    (met_envelopes or {}).get(id(spec)) if g[0] == "met" else None
                )
                if has_mc_corners(envelope):
                    return JointOutcome(
                        f"joint_grid row '{row_id}' names a Monte Carlo "
                        f"member, which is unsupported: {name}",
                        frozenset(),
                        [],
                    )
                missing, probe = member_shortfall(envelope, joint, row_id)
                if missing or probe:
                    row_clean = False
                    entries.append(
                        {
                            "row": row_id,
                            "member": name,
                            "missing_points": missing,
                            "missing_probe": probe,
                        }
                    )
        if row_clean:
            covered.add(row_id)
    return JointOutcome(None, frozenset(covered), entries)


def static_joint_members(
    joint: Mapping[str, Any],
    row_id: str,
    envelopes: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """The spec-row table's static projection of one joint row:
    ``{"covered": bool | None, "members": [...]}`` -- the same per-member
    shortfall :func:`evaluate_joint_rows` grades, computed without re-running
    any command, so a command-backed or uncited member (and a Monte Carlo
    one) reads ``missing_points: null`` / ``missing_probe: null`` rather than
    a guessed verdict.

    ``covered`` is tri-state (issue #7701), the same convention as the
    per-member fields: ``False`` when any *readable* member falls short
    (definitely not covered, whatever an unreadable member would show);
    ``None`` when none does but at least one member is unevaluable from the
    report alone (unknown -- the item axis may still prove it, e.g. a
    command-backed member that ran `klt sim`); ``True`` only when every
    member is readable and clean. The item axis remains the verdict of
    record."""
    entries: list[dict[str, Any]] = []
    shortfall = False
    unevaluable = False
    for name in joint["members"]:
        envelope = envelopes.get(name)
        if envelope is None or has_mc_corners(envelope):
            entries.append(
                {"member": name, "missing_points": None, "missing_probe": None}
            )
            unevaluable = True
            continue
        missing, probe = member_shortfall(envelope, joint, row_id)
        if missing or probe:
            shortfall = True
        entries.append(
            {"member": name, "missing_points": missing, "missing_probe": probe}
        )
    covered: bool | None = False if shortfall else (None if unevaluable else True)
    return {"covered": covered, "members": entries}


def attribute_joint_rows(
    rows: list[dict[str, Any]],
    per_row: dict[str, dict[str, Any]],
    envelopes: Mapping[str, Mapping[str, Any] | None],
) -> None:
    """Fill each declared joint row's ``per_row[row_id]["joint"]`` slot with
    its :func:`static_joint_members` projection -- the spec-row table's hook
    (issue #6969). ``envelopes`` maps each cited member's
    :func:`citation_label` to the envelope the table already read (a
    command-backed or unreadable member has no entry), so the projection
    reuses the caller's resolution cache."""
    for row in rows:
        if row["joint_grid"] is not None:
            per_row[row["id"]]["joint"] = static_joint_members(
                row["joint_grid"], row["id"], envelopes
            )


def joint_table_detail(record: Mapping[str, Any]) -> str:
    """The ``--format text`` summary line for one joint record: how many
    members the row spans and whether the projection found them covered, fell
    short, or could not be evaluated from the report alone (``covered:
    null``, issue #7701 -- "unknown", never worded as a shortfall)."""
    block = record.get("joint") or {}
    members = block.get("members") or []
    covered = block.get("covered")
    if covered is None and members:
        state = "not evaluable from the report alone"
    else:
        state = "covered" if covered else "shortfall"
    return f"{len(members)} member(s), {state}"


def joint_member_lines(record: Mapping[str, Any]) -> list[str]:
    """The indented per-member lines under a joint record in the text
    table: one line per member with a shortfall (a clean member has nothing
    to say), naming the missing grid points in the envelope's own
    ``corner_id`` spelling and a missing condition probe."""
    lines: list[str] = []
    for entry in (record.get("joint") or {}).get("members") or []:
        if entry.get("missing_points") is None:
            lines.append(
                f"member {entry['member']}: not evaluable from the report alone"
            )
            continue
        parts = []
        if entry["missing_points"]:
            parts.append("missing " + ", ".join(entry["missing_points"]))
        if entry.get("missing_probe"):
            parts.append("missing condition probe")
        if parts:
            lines.append(f"member {entry['member']}: " + "; ".join(parts))
    return lines
