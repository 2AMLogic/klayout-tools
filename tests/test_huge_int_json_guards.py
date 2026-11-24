"""Huge-int request-JSON number guards across ``klt`` verbs (issue #5994).

The stdlib ``json`` parser turns a 400-digit integer literal into a Python
``int`` too large for a ``float``, so every request-JSON guard that calls
``math.isfinite`` on the raw value escapes as an uncaught ``OverflowError``
traceback instead of its module's documented error envelope. One parametrized
probe per fixed site; the curated per-module regressions live in each
module's own test file, and sites whose test files sit at their
file-size-baseline ceiling (``sim``, ``sim_crossings``, ``sim_steps``,
``sim_tian``, ``synthesize``) are covered here.
"""

from __future__ import annotations

import pytest

from klayout_tools import (
    _geode,
    _sta_clock_transition,
    _sta_clock_uncertainty,
    corner_matrix,
    em,
    erc_spec,
    fill,
    gen_compose_footprints,
    lvs_timeout,
    place_and_route_groups,
    post_route_sta,
    resistance_path,
    signoff_attest,
    sim,
    sim_corner_point,
    sim_crossings,
    sim_noise,
    sim_slope_band_meas,
    sim_steps,
    sim_supply_relative,
    sim_tian,
    synthesize,
    synthesize_timeout,
    yield_analysis,
)
from klayout_tools import (
    place_and_route_request as pnr_request,
)
from klayout_tools.resistance_path import GraphResistor
from klayout_tools.sim_errors import SimError

HUGE = 10**400  # a 400-digit int literal: an int too large for a float

#: ``(label, thunk, expected exception, message regex)`` -- one entry per
#: fixed site; a thunk raising anything else fails the probe.
CASES = [
    (
        "sta_clock_transition",
        lambda: _sta_clock_transition.validate_clock_transition(
            {"clock_transition_ns": HUGE}
        ),
        Exception,
        "must be a finite number",
    ),
    (
        "sta_clock_uncertainty",
        lambda: _sta_clock_uncertainty.validate_clock_uncertainty(
            {"clock_uncertainty_ns": HUGE}
        ),
        Exception,
        "must be a finite number",
    ),
    (
        "corner_matrix",
        lambda: corner_matrix._measurement_nonfinite(
            {"measurements": [{"value": HUGE}]}
        ),
        None,  # returns True: flagged non-finite, not crashed on
        None,
    ),
    (
        "erc_spec",
        lambda: erc_spec._validate_antenna_max_ratio(HUGE, "spec.json", "where"),
        erc_spec.ErcError,
        "max_ratio must be a finite",
    ),
    (
        "place_and_route_groups",
        lambda: place_and_route_groups._is_finite_number(HUGE),
        None,  # returns False
        None,
    ),
    (
        "pnr_max_extent_um",
        lambda: pnr_request._check_max_extent_um(HUGE, "where"),
        pnr_request.PlaceAndRouteError,
        "max_extent_um must be a number > 0",
    ),
    (
        "pnr_positive_number",
        lambda: pnr_request._positive_number(HUGE, "where"),
        pnr_request.PlaceAndRouteError,
        "must be a positive number",
    ),
    (
        "pnr_floorplan_tracks",
        lambda: pnr_request._validate_floorplan_tracks(
            [
                {
                    "layer": "m1",
                    "x_offset_um": HUGE,
                    "x_pitch_um": 1,
                    "y_offset_um": 0,
                    "y_pitch_um": 1,
                }
            ]
        ),
        pnr_request.PlaceAndRouteError,
        "must be a finite number",
    ),
    (
        "pnr_tapcell",
        lambda: pnr_request._validate_tapcell({"distance_um": HUGE}, None),
        pnr_request.PlaceAndRouteError,
        "distance_um",
    ),
    (
        "pnr_pin_range",
        lambda: pnr_request._validate_pin_range([0, HUGE], "where"),
        pnr_request.PlaceAndRouteError,
        "range_um must be",
    ),
    (
        "resistance_path",
        lambda: resistance_path._validate_graph(
            ["a", "b"],
            [GraphResistor(id="r1", node_a="a", node_b="b", resistance_ohm=HUGE)],
        ),
        resistance_path.ResistanceGraphError,
        "resistance_ohm must be a finite",
    ),
    (
        "signoff_attest",
        lambda: signoff_attest._command_spec(
            {"argv": ["true"], "cwd": ".", "timeout_s": HUGE}, "check", "."
        ),
        signoff_attest.AttestationError,
        "timeout_s",
    ),
    (
        "sim_ngspice_options",
        lambda: sim._validate_ngspice_options({"reltol": HUGE}),
        SimError,
        "must be finite",
    ),
    (
        "sim_nodeset_match",
        lambda: sim._nodeset_match_number(HUGE, "request.where"),
        SimError,
        "must be a finite number",
    ),
    (
        "sim_mismatch_scale",
        lambda: sim._resolve_mismatch_scale({"mismatch_scale": HUGE}, None),
        SimError,
        "mismatch_scale must be a finite non-negative number",
    ),
    (
        "sim_corner_point",
        lambda: sim_corner_point._validate_param_scalar(HUGE, "where"),
        SimError,
        "must be a finite number",
    ),
    (
        "sim_crossings_level",
        lambda: sim_crossings._validate_level("m", HUGE),
        SimError,
        "crossings.level is required and must be",
    ),
    (
        "sim_crossings_sampled_offset",
        lambda: sim_crossings._validate_sampled_offset(
            "m", {"sample_at": "50n", "offset": HUGE}
        ),
        SimError,
        "offset must be a finite number",
    ),
    (
        "sim_steps_pwl",
        lambda: sim_steps._resolve_pwl_alter({"pwl": [0, 1, 2, HUGE]}, "VX", "s"),
        SimError,
        "alter pwl",
    ),
    (
        "sim_steps_alter",
        lambda: sim_steps._resolve_alter({"VA": HUGE}, "s"),
        SimError,
        "alter value",
    ),
    (
        "sim_supply_relative",
        lambda: sim_supply_relative.validate(
            {"frac_of": "vdd", "fraction": HUGE}, "where"
        ),
        sim_supply_relative.SupplyRelativeError,
        "fraction must be a finite number >= 0",
    ),
    (
        "sim_tian_from",
        lambda: sim_tian._validate_from(
            "m", {"metric": "peak_above_crossover_db", "from_ratio": HUGE}
        ),
        SimError,
        "from_ratio must be a finite number > 0",
    ),
    (
        "synthesize_abc_constr",
        lambda: synthesize._resolve_abc_constr_request(
            {"abc_constr": {"driving_cell": "X", "load_ff": HUGE}},
            "",
            resolved_globs=(),
        ),
        synthesize.SynthesizeError,
        "load_ff",
    ),
    (
        "synthesize_timeout",
        lambda: synthesize_timeout.bind_yosys_timeout(
            {"yosys_timeout_s": HUGE}, ValueError
        ),
        ValueError,
        "yosys_timeout_s must be a finite number",
    ),
    (
        "em_mesh_length_unit",
        lambda: em._validate_mesh({"path": "m.msh", "length_unit_m": HUGE}),
        em.EmError,
        "length_unit_m must be a finite number",
    ),
    (
        "geode_capacitance_matrix",
        lambda: _geode._validated_matrix(["a"], [[HUGE]]),
        em.EmError,
        "non-finite or non-numeric entry",
    ),
    (
        "fill_finite_positive",
        lambda: fill._finite_positive(HUGE, "min_width_um"),
        fill.FillError,
        "min_width_um must be a finite number",
    ),
    (
        "gen_compose_gate_bbox",
        lambda: gen_compose_footprints._require_gate_bbox(
            {"x0": 0, "y0": 0, "x1": HUGE, "y1": 1}, "where"
        ),
        ValueError,
        "where.x1 must be a finite number",
    ),
    (
        "lvs_compare_timeout",
        lambda: lvs_timeout.parse_compare_timeout(
            {"compare_timeout_s": HUGE}, "klayout"
        ),
        lvs_timeout.LvsError,
        "compare_timeout_s must be a positive number",
    ),
    (
        "pnr_io_delay",
        lambda: pnr_request._validate_io_delay(HUGE, "input"),
        pnr_request.PlaceAndRouteError,
        "input_delay_ns must be a non-negative number",
    ),
    (
        "post_route_sta_io_delay",
        lambda: post_route_sta._validate_io_delay(HUGE, "output"),
        post_route_sta.PostRouteStaError,
        "output_delay_ns must be a non-negative number",
    ),
    (
        "sim_slope_band_number",
        lambda: sim_slope_band_meas.validate_slope_band_spec(
            "m",
            {
                "y": "v(out)",
                "x": "v(in)",
                "anchor": HUGE,
                "min_slope": 1,
                "max_slope": 2,
                "report": "y_span",
            },
        ),
        SimError,
        "slope_band.anchor must be a finite number",
    ),
    (
        "yield_analysis",
        lambda: yield_analysis._read_qualification_keys(
            {"target_sigma": HUGE}, {}, "where: "
        ),
        yield_analysis.YieldError,
        "target_sigma must be > 0",
    ),
]


@pytest.mark.parametrize("label,thunk,exc,match", CASES, ids=[c[0] for c in CASES])
def test_huge_int_is_a_documented_error_not_overflow_error(label, thunk, exc, match):
    if exc is None:
        # Flagging sites: the huge int is classified, never crashes.
        assert thunk() in (True, False)
    else:
        with pytest.raises(exc, match=match):
            thunk()


def test_huge_int_is_not_a_positive_number_for_noise_frequencies():
    """``sim_noise._positive_number`` is a predicate, not a raiser: a huge int
    used to pass its ``0 < value < inf`` test and then overflow in the
    ``hz / f1`` interpolation."""
    assert sim_noise._positive_number(HUGE) is False
    assert sim_noise._positive_number(1e3) is True
