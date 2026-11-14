"""Probe-deck generation and lockstep bisection for setup/hold constraint
arcs (issue #4756); the request-facing API is
:mod:`klayout_tools.characterize_constraints`.

A constraint is measured by **bisection over data-vs-clock separation**. Each
probe is one cell instance whose data pin makes a single transition at a
chosen offset from the clock's active edge; the probe *passes* when the
cell's output still makes the expected transition and its clock-to-output
delay is within ``(1 + degradation)`` of the reference delay measured with the
data edge far from the clock (the window's wide end). The constraint is the
smallest passing offset, to within ``tolerance_ns``.

- **setup**: the data pin switches to the value the clock edge must capture,
  ``s`` before the clock edge.
- **hold**: the data pin holds the value to be captured through the clock
  edge and switches away ``h`` after it.

The same search measures an **asynchronous** pin's recovery/removal
(issue #5520), with the pin's *release* edge as the constrained edge:

- **recovery**: the async pin is released ``r`` before the clock edge, and
  the probe passes on the same criterion as setup/hold -- the output makes
  the captured transition within the degraded delay.
- **removal**: the async pin is still asserted at the clock edge and
  released ``h`` after it, and the passing outcome is the **opposite**: the
  clock edge must be *ignored*, so a pass is the output making no
  transition at all (``.meas`` finds no crossing) and ``degradation`` does
  not apply. The bracket checks invert with it: the window's wide end must
  *not* capture and its narrow end must.

**Latches (issue #5521)** reuse all of that with two substitutions. The
reference edge is the enable's **closing** edge: the enable opens once, early,
and the probe's data waveform -- unchanged -- is placed relative to the close
at ``t_clk``. And the pass criterion has to change, because a transparent cell
has no clock-to-output delay to degrade: the output already moved when the
data moved, well before the closing edge. A latch probe instead **passes when
both** hold:

1. the **transparent data-to-output delay** -- the probe's capturing data edge
   to the output edge it produces -- is within ``(1 + degradation)`` of the
   same delay measured at the window's wide end (the reference), and
2. the output still **holds the captured level** at the end of the run, after
   the enable has closed and the data has moved on.

(2) is what actually discriminates: a setup probe whose data arrives too late
closes the latch before the new value propagates, and a hold probe whose data
leaves too early is followed transparently back to the old value. (1) keeps
the criterion from accepting a point whose capture only completed because the
transparent path itself had degraded.

Every probe of an iteration lives in one deck (one ``klt sim`` run), bisected
in lockstep across all points, so the cost is
``2 + ceil(log2(window / tolerance))`` simulations. A point whose bracket does
not contain a boundary raises :class:`ConstraintError` -- never a fabricated
value.
"""

from __future__ import annotations

import math
from typing import Any

from . import liberty_writer
from .characterize_bisect_deck import (
    ASYNC_KINDS,
    ConstraintCase,
    ConstraintError,
    DeckRunner,
    build_deck,
)


def _held_captured(case: ConstraintCase, held: Any, vdd: float, latch: bool) -> bool:
    """Did the probe's output still hold the captured value at the end of the
    run?

    Vacuously true for an edge-triggered cell: its capture is already proven
    by the clock-to-output delay the single card measures. For a latch it is
    the discriminating half of the criterion -- ``out_rising`` is the
    direction of the output edge the capture produces, so the captured level
    is high exactly when it is true.
    """
    if not latch:
        return True
    if held is None:
        return False
    return (held > 0.5 * vdd) == case.out_rising


def _where(case: ConstraintCase, ci: int, di: int, grid: tuple[float, ...]) -> str:
    what = "release edge" if case.kind in ASYNC_KINDS else "data edge"
    return (
        f"{case.kind} on '{case.pin}' ({case.edge} {what}) at clock "
        f"transition {grid[ci]} ns / {case.pin} transition {grid[di]} ns"
    )


def _passes(
    case: ConstraintCase,
    delay: float | None,
    held: Any,
    reference: float | None,
    degradation: float,
    vdd: float,
    latch: bool,
) -> bool:
    """Whether one probe is on the *safe* side of the boundary.

    Removal inverts the criterion: the clock edge must be ignored, so a pass
    is the output making no transition at all and the degraded reference
    delay has nothing to say about it.

    A latch has to additionally still hold the captured level at the end of
    the run (:func:`_held_captured`, vacuously true for a flop). The two
    never combine: ``async_pins`` requires ``clock_pin``, which a latch may
    not declare.
    """
    if case.kind == "removal":
        return delay is None
    assert reference is not None  # every non-removal point brackets one
    return (
        delay is not None
        and delay <= reference * (1.0 + degradation)
        and _held_captured(case, held, vdd, latch)
    )


def _bracket(
    case: ConstraintCase,
    where: str,
    wide: float | None,
    wide_held: Any,
    narrow: float | None,
    narrow_held: Any,
    options: dict[str, Any],
    vdd: float,
    latch: bool,
    reference_noun: str,
) -> float | None:
    """Check one point's bracket and return its reference delay.

    The wide end of the window must be on the *safe* side of the boundary
    and the narrow end on the unsafe side, or the boundary is not inside the
    search window at all. Removal inverts both (see :func:`_passes`) and has
    no reference delay to return; a latch's safe side additionally requires
    the captured level to still be held.
    """
    wlo, whi = options["window_ns"]
    moved = "release edge" if case.kind in ASYNC_KINDS else "data edge"
    if case.kind == "removal":
        if wide is not None:
            raise ConstraintError(
                f"{where}: the clock edge was still captured with the "
                f"release edge {whi} ns after it, so the constraint cannot "
                "be bracketed (widen options.constraint_window_ns, or check "
                "the cell's netlist and cell.async_pins)"
            )
        if narrow is None:
            raise ConstraintError(
                f"{where}: the clock edge is ignored even with the release "
                f"edge {wlo} ns from it, so the boundary lies below the "
                "search window (lower options.constraint_window_ns[0])"
            )
        return None
    if wide is None or not _held_captured(case, wide_held, vdd, latch):
        missed = (
            "the output did not settle to and hold the captured value"
            if latch
            else "the output never made the expected transition"
        )
        raise ConstraintError(
            f"{where}: {missed} even with the {moved} {whi} ns from the "
            f"{reference_noun} -- the constraint cannot be bracketed (widen "
            "options.constraint_window_ns, or check the cell's function/"
            "static_inputs)"
        )
    if (
        narrow is not None
        and narrow <= wide * (1.0 + options["degradation"])
        and _held_captured(case, narrow_held, vdd, latch)
    ):
        raise ConstraintError(
            f"{where}: the cell still captures with the {moved} {wlo} ns "
            f"from the {reference_noun}, so the boundary lies below the "
            "search window (lower options.constraint_window_ns[0])"
        )
    return wide


def run_bisection(
    *,
    cell: dict[str, Any],
    cases: tuple[ConstraintCase, ...],
    grid_ns: tuple[float, ...],
    rail_nodes: dict[str, str],
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    ramps_ns: tuple[float, ...],
    settle_ns: float,
    load_pf: float,
    options: dict[str, Any],
    run_deck: DeckRunner,
) -> dict[str, Any]:
    """Bisect every (case, clock slew, data slew) point in lockstep.

    Returns ``{"values": {(case, ci, di): offset_ns}, "reference_ns":
    {...}, "iterations": n}``; a removal point's ``reference_ns`` is ``None``
    because its pass criterion has no reference delay. Raises
    :class:`ConstraintError` when a point's bracket does not contain a
    boundary.

    A flop point passes when its clock-to-output delay is within
    ``(1 + degradation)`` of the reference; a latch point additionally has to
    still hold the captured level at the end of the run (see this module's
    docstring, and :func:`_held_captured`).
    """
    wlo, whi = options["window_ns"]
    degradation = options["degradation"]
    latch = bool(cell.get("latch"))
    reference_noun = "enable's closing edge" if latch else "clock edge"
    points = [
        (c, ci, di)
        for c in range(len(cases))
        for ci in range(len(grid_ns))
        for di in range(len(grid_ns))
    ]

    def run(probes: list[tuple[int, int, int, float]], label: str):
        text, measurements, stop = build_deck(
            cell=cell,
            cases=cases,
            probes=probes,
            rail_nodes=rail_nodes,
            vdd=vdd,
            thresholds=thresholds,
            ramps_ns=ramps_ns,
            settle_ns=settle_ns,
            window_ns=options["window_ns"],
            load_pf=load_pf,
            edge=cell["clock_edge"],
            latch=latch,
        )
        raw = run_deck(text, measurements, label, stop)
        return [(raw.get(f"c{k}_d"), raw.get(f"c{k}_h")) for k in range(len(probes))]

    probes = [(c, ci, di, whi) for c, ci, di in points] + [
        (c, ci, di, wlo) for c, ci, di in points
    ]
    results = run(probes, "bracket")
    wide, narrow = results[: len(points)], results[len(points) :]
    reference: dict[tuple[int, int, int], float | None] = {}
    for point, (ref, ref_held), (low, low_held) in zip(
        points, wide, narrow, strict=True
    ):
        case = cases[point[0]]
        reference[point] = _bracket(
            case,
            _where(case, point[1], point[2], grid_ns),
            ref,
            ref_held,
            low,
            low_held,
            options,
            vdd,
            latch,
            reference_noun,
        )

    low_side = {p: wlo for p in points}
    high_side = {p: whi for p in points}
    iterations = max(1, math.ceil(math.log2((whi - wlo) / options["tolerance_ns"])))
    for n in range(iterations):
        mids = {p: 0.5 * (low_side[p] + high_side[p]) for p in points}
        measured = run(
            [(c, ci, di, mids[(c, ci, di)]) for c, ci, di in points], f"iter{n}"
        )
        for point, (delay, held) in zip(points, measured, strict=True):
            passed = _passes(
                cases[point[0]],
                delay,
                held,
                reference[point],
                degradation,
                vdd,
                latch,
            )
            if passed:
                high_side[point] = mids[point]
            else:
                low_side[point] = mids[point]
    return {
        "values": dict(high_side),
        "reference_ns": {
            p: (None if v is None else v * 1e9) for p, v in reference.items()
        },
        "iterations": iterations,
    }
