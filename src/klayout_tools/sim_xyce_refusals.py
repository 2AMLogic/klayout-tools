"""``engine: "xyce"`` request checks and diagnostic precedence.

Split out of sim.py to keep it under its file-size ceiling."""

from __future__ import annotations

from typing import Any

from . import (
    sim_crossings,
    sim_cycle_mean,
    sim_energy,
    sim_eye,
    sim_op_check,
    sim_search,
    sim_settled,
    sim_slope_band_meas,
    sim_snm_meas,
    sim_step_resolution,
    sim_stress,
    sim_transfer_meas,
)
from .sim_errors import SimError
from .sim_validate import _expr_measurements


def refuse_control_block_measurements(
    measurements_spec: list[dict[str, Any]],
) -> None:
    """The ``engine: "xyce"`` refusals for measurement forms that exist only
    as commands in klt's generated ngspice ``.control`` block (``expr``,
    issue #2533; ``crossings``, issue #2868; ``energy``, issue #3033). Split out of
    :func:`enforce_support_boundary` to keep that gate under the
    complexity limit. The table's own fixed form order picks the first
    applicable diagnostic, so a request declaring several unsupported forms
    is refused identically whatever order it declared them in."""
    for find, message in (
        (
            _expr_measurements,
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
            sim_crossings.crossings_measurements,
            # Issue #2868: the series is dumped by ngspice `let`/`write`
            # commands in the generated `.control` block -- same boundary as
            # `expr`, refused by name for the same reason.
            "measurements[].crossings is not supported for engine 'xyce': "
            "the signal is dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            sim_cycle_mean.cycle_mean_measurements,
            # Issue #2947: same `.control` let/write boundary as `crossings`.
            "measurements[].cycle_mean is not supported for engine 'xyce': "
            "the signals are dumped by ngspice `let`/`write` commands inside "
            "a `.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            sim_energy.energy_measurements,
            # Issue #3033: the source current/voltage are dumped by ngspice
            # `let`/`write` commands in the generated `.control` block -- same
            # boundary as `crossings`.
            "measurements[].energy is not supported for engine 'xyce': the "
            "source current and voltage are dumped by ngspice `let`/`write` "
            "commands inside a `.control` block, and the Xyce deck has none; "
            "use engine 'ngspice'",
        ),
        (
            sim_stress.stress_measurements,
            # Issue #4592: the node-pair voltage is dumped by ngspice
            # `let`/`write` commands in the generated `.control` block -- same
            # boundary as `energy`.
            "measurements[].stress is not supported for engine 'xyce': the "
            "node voltages are dumped by ngspice `let`/`write` commands "
            "inside a `.control` block, and the Xyce deck has none; use "
            "engine 'ngspice'",
        ),
        (
            sim_transfer_meas.transfer_measurements,
            # Issue #2923: same `.control` let/write boundary as `cycle_mean`.
            "measurements[].transfer is not supported for engine 'xyce': the "
            "AC vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            sim_snm_meas.snm_measurements,
            # Issue #3895: same `.control` let/write boundary as `transfer`.
            "measurements[].snm is not supported for engine 'xyce': the sweep "
            "vectors are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            sim_slope_band_meas.slope_band_measurements,
            # Issue #5518: same `.control` let/write boundary as `snm`.
            "measurements[].slope_band is not supported for engine 'xyce': "
            "the sweep vectors are dumped by ngspice `let`/`write` commands "
            "inside a `.control` block, and the Xyce deck has none; use "
            "engine 'ngspice'",
        ),
        (
            sim_search.search_measurements,
            # Issue #4121: the bisection is an ngspice `.control` `while` loop.
            "measurements[].search is not supported for engine 'xyce': the "
            "bisection runs as an ngspice `.control` `while` loop, and the "
            "Xyce deck has no control block; use engine 'ngspice'",
        ),
        (
            sim_eye.eye_measurements,
            # Issue #4292: same `.control` let/write boundary as `crossings`.
            "measurements[].eye is not supported for engine 'xyce': the "
            "signal is dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
        (
            sim_step_resolution.step_resolution_measurements,
            # Issue #4773: same `.control` let/write boundary as `crossings`.
            "measurements[].step_resolution is not supported for engine "
            "'xyce': the signal is dumped by ngspice `let`/`write` commands "
            "inside a `.control` block, and the Xyce deck has none; use "
            "engine 'ngspice'",
        ),
        (
            sim_settled.settled_measurements,
            # Issue #4780: same `.control` let/write boundary as `crossings`.
            "measurements[].settled is not supported for engine 'xyce': the "
            "signals are dumped by ngspice `let`/`write` commands inside a "
            "`.control` block, and the Xyce deck has none; use engine "
            "'ngspice'",
        ),
    ):
        if find(measurements_spec):
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


def _refuse_params_axis(corners_spec: dict[str, Any]) -> None:
    """The ``engine: "xyce"`` refusal for ``corners.params``. Split out of
    :func:`enforce_support_boundary` to keep that gate under the
    complexity limit."""
    if corners_spec.get("params") or corners_spec.get("temperature_param"):
        raise SimError(
            "engine 'xyce' does not support corners.params or "
            "corners.temperature_param: the parameter "
            "axis is applied with ngspice's `alterparam` + `reset` control "
            "commands, which Xyce has no netlist equivalent of -- use engine "
            "'ngspice'"
        )


def _refuse_alter_corner_axes(corners_spec: dict[str, Any]) -> None:
    """The ``engine: "xyce"`` refusals for corner axes that are applied with
    ngspice's ``alter`` control command (``supply_v``; ``variants``, issue
    #3252). Split out of :func:`enforce_support_boundary` to keep that
    gate under the complexity limit."""
    if corners_spec.get("supply_v") or any(
        isinstance(unit, dict) and unit.get("supply_v")
        for unit in corners_spec.get("units") or []
    ):
        raise SimError(
            "engine 'xyce' does not support corners.supply_v: Xyce has "
            "no netlist equivalent of ngspice's `alter` control command, "
            "so per-corner supply rails cannot be patched into the deck "
            "-- leave corners.supply_v unset (fixed supply, hardcoded in "
            "the netlist body) or use engine 'ngspice'"
        )
    if corners_spec.get("variants"):
        raise SimError(
            "engine 'xyce' does not support corners.variants: variant "
            "sources are applied with ngspice's `alter` control command, "
            "which Xyce has no netlist equivalent of -- leave "
            "corners.variants unset or use engine 'ngspice'"
        )


def enforce_support_boundary(
    *,
    engine: str,
    backend: str,
    corners_spec: dict[str, Any],
    monte_carlo_declared: bool,
    fail_fast_probe: bool,
    measurements_spec: list[dict[str, Any]] | None = None,
    osdi_preload_declared: bool = False,
    save_mode_declared: bool = False,
    save_vectors_declared: bool = False,
    ngspice_options_declared: bool = False,
    nodeset_declared: bool = False,
    initial_conditions_declared: bool = False,
    analysis_steps_declared: bool = False,
    linearize_declared: bool = False,
    op_check_declared: bool = False,
) -> None:
    """Refuse the ``engine: "xyce"`` combinations the v1 path does not
    implement (issue #2016) -- up front, with the same clean
    :class:`SimError` the engine/backend gates use, rather than as
    per-corner errors after dispatch. Each refusal names what *is*
    supported so a caller can re-shape the request instead of guessing;
    the syntax divergences behind each refusal are recorded in
    ``sim._write_xyce_deck``'s docstring and docs/design/xyce-oracle.md. A
    no-op for every other engine.
    """
    if engine != "xyce":
        return
    refuse_linearize(linearize_declared)
    sim_op_check.refuse_xyce(op_check_declared)  # issue #5077
    if analysis_steps_declared:
        # Issue #2482: an ordered step sequence is `alter`/analysis/`let`
        # commands inside one ngspice `.control` block. The Xyce deck has no
        # control block and no `alter`, and no equivalent ordered-analysis
        # contract is implemented for it -- refused rather than run with the
        # sequence silently reduced to something else. Checked first: the
        # flattened step measurements are `expr`s too, and this is the more
        # specific refusal.
        raise SimError(
            "analysis_steps is not supported for engine 'xyce': the ordered "
            "step sequence is generated as `alter`/analysis/`let` commands in "
            "an ngspice `.control` block, which the Xyce deck does not have; "
            "use engine 'ngspice'"
        )
    if backend not in ("local", "local-parallel"):
        raise SimError(
            f"engine 'xyce' supports only the local backends (local, "
            f"local-parallel), not '{backend}' -- the remote/batch "
            "provisioning path pins the ngspice toolchain"
        )
    _refuse_alter_corner_axes(corners_spec)
    _refuse_params_axis(corners_spec)
    refuse_options_axis(corners_spec)
    if monte_carlo_declared:
        raise SimError(
            "engine 'xyce' does not implement monte_carlo sampling -- "
            "ngspice's `.options seed=`/rndseed mechanism has no Xyce "
            "equivalent wired up yet; use engine 'ngspice'"
        )
    if fail_fast_probe:
        raise SimError(
            "options.fail_fast_probe is not implemented for engine "
            "'xyce': the calibration probe reads ngspice's own rawfile "
            "stream; use engine 'ngspice' or drop the option"
        )
    if osdi_preload_declared:
        # Issue #2513: `pre_osdi` is an ngspice control command and Xyce has
        # no `.control` block to carry it; Xyce compiles Verilog-A into a
        # plugin through its own `buildxyceplugin` flow and loads it with
        # `-plugin`, which a `.osdi` file is not. Refused by name rather
        # than silently dropped -- a deck that ran without its preload would
        # report every OSDI device as an undefined model.
        raise SimError(
            "options.osdi_preload is not supported for engine 'xyce': "
            "`pre_osdi` is an ngspice control command, and Xyce loads "
            "Verilog-A models as its own compiled plugins (`-plugin`), not "
            "OSDI shared libraries; use engine 'ngspice' or drop the option"
        )
    refuse_control_block_measurements(measurements_spec or [])
    for declared, message in (
        # Issue #2732: `save_mode` selects whether klt's generated ngspice
        # `.control` block carries `save all`; the Xyce deck has no control
        # block and no generated save card to suppress. Refused by name,
        # even for "all", rather than silently accepted with no effect.
        (
            save_mode_declared,
            "options.save_mode is not supported for engine 'xyce': it "
            "controls the `save all` card in klt's generated ngspice "
            "`.control` block, which the Xyce deck does not have; use "
            "engine 'ngspice' or omit options.save_mode",
        ),
        # Issue #3211: `save_vectors` rewrites the ngspice `save` card.
        (
            save_vectors_declared,
            "options.save_vectors is not supported for engine 'xyce': it "
            "replaces the `save all` card in klt's generated ngspice "
            "`.control` block, which the Xyce deck does not have; use "
            "engine 'ngspice' or omit options.save_vectors",
        ),
        # Issue #3111: the names are ngspice `.options` solver controls; the
        # Xyce deck has its own `.options` namespaces, so they are refused
        # by name rather than silently mapped or dropped.
        (
            ngspice_options_declared,
            "options.ngspice_options is not supported for engine 'xyce': "
            "its names are ngspice solver `.options`; use engine 'ngspice' "
            "or omit options.ngspice_options",
        ),
    ):
        if declared:
            raise SimError(message)
    refuse_nodeset(nodeset_declared)
    if initial_conditions_declared:
        raise SimError(
            "options.initial_conditions is not supported for engine 'xyce': "
            "start states are applied as ngspice `.ic` cards with `uic`; use "
            "engine 'ngspice' or omit options.initial_conditions"
        )
