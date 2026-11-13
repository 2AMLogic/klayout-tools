"""``engine: "xyce"`` refusals for control-block-only measurement forms.

Split out of sim.py to keep it under its file-size ceiling."""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError


def refuse_control_block_measurements(
    *,
    expr_declared: bool,
    crossings_declared: bool,
    cycle_mean_declared: bool,
    energy_declared: bool,
    stress_declared: bool = False,
    transfer_declared: bool = False,
    snm_declared: bool = False,
    slope_band_declared: bool = False,
    search_declared: bool = False,
    eye_declared: bool = False,
    step_resolution_declared: bool = False,
    settled_declared: bool = False,
) -> None:
    """The ``engine: "xyce"`` refusals for measurement forms that exist only
    as commands in klt's generated ngspice ``.control`` block (``expr``,
    issue #2533; ``crossings``, issue #2868; ``energy``, issue #3033). Split out of
    :func:`klayout_tools.sim._enforce_xyce_support_boundary` to keep that gate under the
    complexity limit."""
    for declared, message in (
        (
            expr_declared,
            # Issue #2533: an `expr` measurement is emitted as ngspice
            # `let`/`print` commands inside the generated `.control` block, and
            # the Xyce deck has no control block to carry them (see
            # `_write_xyce_deck`'s docstring). Refused by name rather than
            # silently dropped: a corner that ran without its `expr` lines would
            # report every such measurement as "produced no value", which reads
            # as a circuit regression rather than an unsupported request field.
            "measurements[].expr is not supported for engine 'xyce': it is "
            "evaluated by ngspice `let`/`print` commands inside a `.control` "
            "block, and the Xyce deck has none (its analysis is a plain "
            "top-level dot card); use engine 'ngspice', or express the "
            "quantity as a `.meas` card in measurements[].spice",
        ),
        (
            crossings_declared,
            # Issue #2868: the series is dumped by ngspice `let`/`write`
            # commands in the generated `.control` block -- same boundary as
            # `expr`, refused by name for the same reason.
            "measurements[].crossings is not supported for engine 'xyce': "
            "the signal is dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            cycle_mean_declared,
            # Issue #2947: same `.control` let/write boundary as `crossings`.
            "measurements[].cycle_mean is not supported for engine 'xyce': "
            "the signals are dumped by ngspice `let`/`write` commands inside "
            "a `.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            energy_declared,
            # Issue #3033: the source current/voltage are dumped by ngspice
            # `let`/`write` commands in the generated `.control` block -- same
            # boundary as `crossings`.
            "measurements[].energy is not supported for engine 'xyce': the "
            "source current and voltage are dumped by ngspice `let`/`write` "
            "commands inside a `.control` block, and the Xyce deck has none; "
            "use engine 'ngspice'",
        ),
        (
            stress_declared,
            # Issue #4592: the node-pair voltage is dumped by ngspice
            # `let`/`write` commands in the generated `.control` block -- same
            # boundary as `energy`.
            "measurements[].stress is not supported for engine 'xyce': the "
            "node voltages are dumped by ngspice `let`/`write` commands "
            "inside a `.control` block, and the Xyce deck has none; use "
            "engine 'ngspice'",
        ),
        (
            transfer_declared,
            # Issue #2923: same `.control` let/write boundary as `cycle_mean`.
            "measurements[].transfer is not supported for engine 'xyce': the "
            "AC vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            snm_declared,
            # Issue #3895: same `.control` let/write boundary as `transfer`.
            "measurements[].snm is not supported for engine 'xyce': the sweep "
            "vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            slope_band_declared,
            # Issue #5518: same `.control` let/write boundary as `snm`.
            "measurements[].slope_band is not supported for engine 'xyce': "
            "the sweep vectors are dumped by ngspice `let`/`write` commands "
            "inside a `.control` block, and the Xyce deck has none; use "
            "engine 'ngspice'",
        ),
        (
            search_declared,
            # Issue #4121: the bisection is an ngspice `.control` `while` loop.
            "measurements[].search is not supported for engine 'xyce': the "
            "bisection runs as an ngspice `.control` `while` loop, and the "
            "Xyce deck has no control block; use engine 'ngspice'",
        ),
        (
            eye_declared,
            # Issue #4292: same `.control` let/write boundary as `crossings`.
            "measurements[].eye is not supported for engine 'xyce': the "
            "signal is dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            step_resolution_declared,
            # Issue #4773: same `.control` let/write boundary as `crossings`.
            "measurements[].step_resolution is not supported for engine "
            "'xyce': the signal is dumped by ngspice `let`/`write` commands "
            "inside a `.control` block, and the Xyce deck has none; use "
            "engine 'ngspice'",
        ),
        (
            settled_declared,
            # Issue #4780: same `.control` let/write boundary as `crossings`.
            "measurements[].settled is not supported for engine 'xyce': the "
            "signals are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
    ):
        if declared:
            raise SimError(message)


def refuse_nodeset(declared: bool) -> None:
    """Issue #3036: the hints are rendered as ngspice `.nodeset v(node)=`
    cards; the Xyce deck is not given an equivalent."""
    if declared:
        raise SimError(
            "options.nodeset is not supported for engine 'xyce': initial "
            "guesses are applied as ngspice `.nodeset` cards; use engine "
            "'ngspice' or omit options.nodeset"
        )


def refuse_linearize(declared: bool) -> None:
    """Issue #4014: `linearize` is an ngspice control command; the Xyce deck
    has no `.control` block to carry it."""
    if declared:
        raise SimError(
            "analysis.linearize is not supported for engine 'xyce': it is an "
            "ngspice `.control` command, which the Xyce deck does not have; "
            "use engine 'ngspice' or drop the field"
        )


def refuse_options_axis(corners_spec: dict[str, Any]) -> None:
    """The ``engine: "xyce"`` refusal for ``corners.options`` (issue #3125):
    the axis is a generated ngspice ``.options`` card."""
    if corners_spec.get("options"):
        raise SimError(
            "engine 'xyce' does not support corners.options: the axis is "
            "emitted as ngspice solver `.options` cards, whose names Xyce "
            "does not share -- use engine 'ngspice'"
        )
