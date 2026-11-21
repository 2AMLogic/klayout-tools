"""``measurements[].noise.top_n`` -- the largest noise contributors by name (#5558).

Validation, ranking, deck emission and response assembly, all without an
engine. The engine-gated runs are in `test_sim_noise_mechanism_ngspice.py`
(they share its deck); the leaf vocabulary being ranked is covered by
`test_sim_noise_leaves.py`.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from klayout_tools import (
    sim,
    sim_measurement_results,
    sim_noise,
    sim_noise_contributors,
    sim_noise_mechanism,
)

ARGS = "v(out) V1 dec 20 10 1e7 1"
ANALYSIS = {"kind": "noise", "args": ARGS}
ANALYSIS_NO_PTS = {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7"}
#: A linear 3-point sweep, for the synthetic (flat-spectrum) rawfiles below.
LIN = {"kind": "noise", "args": "v(out) V1 lin 3 1 3 1"}


def _spec(top_n=3, name="vn", **noise):
    body = {"quantity": "onoise", "top_n": top_n, **noise}
    if "at_hz" not in body and "band_hz" not in body:
        body["at_hz"] = 2.0
    return {"name": name, "noise": body, "unit": "V"}


def _validate(spec, analysis=ANALYSIS):
    sim._validate_measurement_forms([spec])
    sim_noise.validate_noise_context([spec], analysis)


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("top_n", [1, 3, 100])
def test_a_positive_integer_top_n_is_accepted_on_either_form(top_n):
    _validate(_spec(top_n, at_hz=1e3))
    _validate(_spec(top_n, at_hz=1e3, mechanism="flicker"))
    _validate(_spec(top_n, band_hz=[100, 1e6]))


@pytest.mark.parametrize("top_n", [0, -1, 1.5, 3.0, True, False, "3", None, [3]])
def test_anything_but_a_positive_integer_is_refused_naming_the_entry(top_n):
    with pytest.raises(sim.SimError, match=r"'vn'.*top_n.*positive integer"):
        _validate(_spec(top_n, at_hz=1e3))


def test_inoise_top_n_is_refused_with_the_reason():
    spec = {"name": "vn", "noise": {"quantity": "inoise", "at_hz": 1e3, "top_n": 2}}
    with pytest.raises(sim.SimError, match="per-contributor input-referred") as exc:
        _validate(spec)
    assert "'vn'" in str(exc.value) and "top_n" in str(exc.value)


def test_missing_pts_per_summary_is_refused_naming_top_n_and_the_fix():
    with pytest.raises(sim.SimError, match="pts_per_summary") as exc:
        _validate(_spec(2, at_hz=1e3), ANALYSIS_NO_PTS)
    message = str(exc.value)
    assert "'vn'" in message and "noise.top_n 2" in message
    assert "noise.mechanism" not in message
    assert "'v(out) V1 dec 20 10 1e7 1'" in message


def test_a_mechanism_entry_still_names_mechanism_when_pts_is_missing():
    spec = _spec(2, at_hz=1e3, mechanism="thermal")
    with pytest.raises(sim.SimError, match="noise.mechanism 'thermal'"):
        _validate(spec, ANALYSIS_NO_PTS)


def test_an_entry_without_top_n_or_mechanism_still_needs_no_pts_per_summary():
    total = {"name": "n0", "noise": {"quantity": "onoise", "at_hz": 1e3}}
    _validate(total, ANALYSIS_NO_PTS)


# ------------------------------------------------------------------ emission


def _control(specs, analysis=ANALYSIS, corner_dir="/corner"):
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=sim_noise.lower_measurements(specs, analysis),
        raw_path=None,
        osdi_preload=(),
        corner_dir=corner_dir,
    )


def test_a_total_entry_with_top_n_dumps_the_spectra_plot():
    lines = _control([_spec(2, at_hz=1e3)])
    dump = lines.index("write /corner/noise_mechanism.raw")
    assert lines[dump - 1] == "setplot noise1"
    assert lines[dump + 1] == "setplot $__klt_nzm_plot"


def test_top_n_leaves_the_lowered_value_path_untouched():
    """`value` of a total entry must stay what the entry returns without
    `top_n`: the very same `let`/`print` lines, not a rawfile reduction."""
    plain = {"name": "vn", "noise": {"quantity": "onoise", "at_hz": 1e3}}
    ranked = _spec(2, at_hz=1e3) | {"unit": None}
    (lowered_plain,) = sim_noise.lower_measurements([plain], ANALYSIS)
    (lowered_ranked,) = sim_noise.lower_measurements([ranked], ANALYSIS)
    assert lowered_ranked["expr"] == lowered_plain["expr"]
    assert lowered_ranked["expr_prelude"] == lowered_plain["expr_prelude"]
    plain_deck, ranked_deck = _control([plain]), _control([ranked])
    # The ranked deck is the plain deck plus exactly the spectra dump group.
    assert [line for line in ranked_deck if line in plain_deck] == plain_deck
    assert [line for line in ranked_deck if line not in plain_deck] == [
        "set filetype=ascii",
        *sim_noise_mechanism.control_lines([ranked], "/corner"),
    ]


def test_one_dump_group_for_top_n_and_mechanism_entries_together():
    lines = _control([_spec(2, name="a"), _spec(2, name="b", mechanism="thermal")])
    assert lines.count("write /corner/noise_mechanism.raw") == 1


# ------------------------------------------------------------------- ranking


def _rawfile(tmp_path: Path, names: list[str], rows: list[list[float]]) -> None:
    """A minimal ngspice ASCII rawfile of a noise spectral-density plot."""
    body = [
        "Title: synthetic",
        "Plotname: Noise Spectral Density Curves",
        "Flags: real",
        f"No. Variables: {len(names)}",
        f"No. Points: {len(rows)}",
        "Variables:",
    ]
    for index, name in enumerate(names):
        body.append(f"\t{index}\t{name}\tvoltage-density")
    body.append("Values:")
    for point, row in enumerate(rows):
        body.append(f" {point}\t{row[0]:.15e}")
        body.extend(f"\t{value:.15e}" for value in row[1:])
    (tmp_path / "noise_mechanism.raw").write_text("\n".join(body) + "\n")


#: Two devices and a diode, in an order that is not the ranking order.
NAMES = [
    "frequency",
    "onoise_m1",  # device total -- never ranked
    "onoise_m1_igb",  # identically zero gate leakage
    "onoise_m1_id",  # thermal, 3
    "onoise_m1_1overf",  # flicker, 4
    "onoise_r2",
    "onoise_r2_thermal",  # thermal, 2 (ties r1)
    "onoise_r1",
    "onoise_r1_thermal",  # thermal, 2
    "onoise_d1",
    "onoise_d1_id",  # unclassified diode shot noise, 1
    "onoise_spectrum",
]
ROW = [None, 5.0, 0.0, 3.0, 4.0, 2.0, 2.0, 2.0, 2.0, 1.0, 1.0, 7.0]


def _extract(tmp_path, specs, names=NAMES, row=ROW, analysis=LIN):
    rows = [[f, *row[1:]] for f in (1.0, 2.0, 3.0)]
    _rawfile(tmp_path, names, rows)
    return sim_noise_mechanism.extract_corner(
        sim_noise.lower_measurements(specs, analysis), str(tmp_path), analysis
    )


def _contributors(tmp_path, top_n, **noise):
    outcomes = _extract(tmp_path, [_spec(top_n, **noise)])
    (outcome,) = outcomes.values()
    assert outcome.error is None, outcome.error
    return outcome.contributors


def test_leaves_are_ranked_largest_first_with_names_and_mechanisms(tmp_path):
    assert _contributors(tmp_path, 10) == [
        {"name": "m1_1overf", "value": 4.0, "mechanism": "flicker"},
        {"name": "m1_id", "value": 3.0, "mechanism": "thermal"},
        {"name": "r1_thermal", "value": 2.0, "mechanism": "thermal"},
        {"name": "r2_thermal", "value": 2.0, "mechanism": "thermal"},
        {"name": "d1_id", "value": 1.0, "mechanism": None},
    ]


def test_ties_break_by_name_not_by_rawfile_order(tmp_path):
    ranked = _contributors(tmp_path, 10)
    # r2 precedes r1 in the rawfile; the name decides.
    assert [c["name"] for c in ranked[2:4]] == ["r1_thermal", "r2_thermal"]


def test_device_totals_and_zero_power_leaves_never_appear(tmp_path):
    names = {c["name"] for c in _contributors(tmp_path, 10)}
    assert "m1" not in names and "r1" not in names and "d1" not in names
    assert "m1_igb" not in names


def test_top_n_truncates_and_a_larger_one_is_not_an_error(tmp_path):
    assert [c["name"] for c in _contributors(tmp_path, 1)] == ["m1_1overf"]
    assert [c["name"] for c in _contributors(tmp_path, 2)] == ["m1_1overf", "m1_id"]
    assert len(_contributors(tmp_path, 1000)) == 5


@pytest.mark.parametrize("mechanism", ["thermal", "flicker"])
def test_a_mechanism_on_the_entry_filters_the_ranked_leaves(tmp_path, mechanism):
    ranked = _contributors(tmp_path, 10, mechanism=mechanism)
    assert ranked and {c["mechanism"] for c in ranked} == {mechanism}


def test_without_a_mechanism_an_unclassified_leaf_reports_null(tmp_path):
    ranked = _contributors(tmp_path, 10)
    assert {c["mechanism"] for c in ranked} == {"flicker", "thermal", None}


def test_a_band_value_is_the_leafs_own_rms_over_the_band(tmp_path):
    """Flat 4.0 over [1, 3] Hz: sqrt(4**2 * 2)."""
    (top, *_) = _contributors(tmp_path, 1, band_hz=[1.0, 3.0])
    assert top["value"] == pytest.approx(4.0 * math.sqrt(2.0))


def test_the_contributor_powers_add_up_to_the_total_leaf_power(tmp_path):
    """Amplitudes add in power: the ranked list is the decomposition."""
    ranked = _contributors(tmp_path, 100, band_hz=[1.0, 3.0])
    assert sum(c["value"] ** 2 for c in ranked) == pytest.approx(
        (9 + 16 + 4 + 4 + 1) * 2.0
    )


@pytest.mark.parametrize(
    "total, leaf, expected",
    [
        # ngspice's separator is not fixed: `.` for a sky130 device ...
        (
            "onoise.m.xm1.msky130_fd_pr__nfet_01v8",
            "onoise.m.xm1.msky130_fd_pr__nfet_01v8.1overf",
            "m.xm1.msky130_fd_pr__nfet_01v8.1overf",
        ),
        # ... `_` for a flat device, and a mix inside a subcircuit.
        ("onoise_m1", "onoise_m1_1overf", "m1_1overf"),
        ("onoise_m.x1.mi", "onoise_m.x1.mi_1overf", "m.x1.mi_1overf"),
    ],
)
def test_names_drop_only_the_prefix_and_its_one_separator(
    tmp_path, total, leaf, expected
):
    names = ["frequency", total, leaf, "onoise_spectrum"]
    outcomes = _extract(tmp_path, [_spec(5)], names=names, row=[None, 1, 1, 1])
    (outcome,) = outcomes.values()
    assert [c["name"] for c in outcome.contributors] == [expected]


def test_only_top_n_entries_are_read_from_the_rawfile(tmp_path):
    plain = {"name": "total", "noise": {"quantity": "onoise", "at_hz": 2.0}}
    outcomes = _extract(tmp_path, [plain, _spec(2, name="ranked")])
    assert set(outcomes) == {1}


def test_a_total_outcome_carries_only_its_list(tmp_path):
    (outcome,) = _extract(tmp_path, [_spec(2)]).values()
    assert outcome.value is None and outcome.total is None
    assert outcome.mechanism is None and outcome.code is None


# ------------------------------------------------------- response assembly


def _grade(specs, values, outcomes, *, save_mode="all", analysis=LIN):
    return sim_measurement_results._corner_measurement_results(
        values,
        sim_noise.lower_measurements(specs, analysis),
        analysis=analysis,
        log_text="",
        is_xyce=False,
        save_mode=save_mode,
        noise_mechanisms=outcomes,
    )


def _without_noise(entry):
    return {key: value for key, value in entry.items() if key != "noise"}


def test_a_total_entry_keeps_its_value_and_gains_only_the_list(tmp_path):
    plain = {
        "name": "plain",
        "noise": {"quantity": "onoise", "at_hz": 2.0},
        "unit": "V",
    }
    specs = [plain, _spec(2, name="ranked")]
    outcomes = _extract(tmp_path, specs)
    results, diagnostics, _ = _grade(specs, {"plain": 7.0, "ranked": 7.0}, outcomes)
    assert diagnostics == []
    assert "noise" not in results[0]
    assert _without_noise(results[1]) == {**results[0], "name": "ranked"}
    assert results[1]["noise"] == {
        "contributors": [
            {"name": "m1_1overf", "value": 4.0, "mechanism": "flicker"},
            {"name": "m1_id", "value": 3.0, "mechanism": "thermal"},
        ]
    }
    assert list(results[1]["noise"]) == ["contributors"]


def test_grading_is_identical_with_and_without_top_n(tmp_path):
    """A failing limit fails the same way, with the same margin."""
    specs = [
        {
            "name": "a",
            "noise": {"quantity": "onoise", "at_hz": 2.0},
            "limits": {"max": 5},
        },
        _spec(2, name="b") | {"limits": {"max": 5}},
    ]
    outcomes = _extract(tmp_path, specs)
    results, _, _ = _grade(specs, {"a": 7.0, "b": 7.0}, outcomes)
    assert results[0]["status"] == results[1]["status"] == "fail"
    assert results[0]["margin"] == results[1]["margin"]


def test_a_mechanism_entry_carries_its_list_inside_its_existing_noise_object(
    tmp_path,
):
    specs = [_spec(2, mechanism="thermal")]
    outcomes = _extract(tmp_path, specs)
    (entry,) = _grade(specs, {}, outcomes)[0]
    assert entry["noise"]["mechanism"] == "thermal"
    assert set(entry["noise"]) == {
        "mechanism",
        "total",
        "unclassified",
        "residual_relative",
        "unclassified_suffixes",
        "contributors",
    }
    assert [c["name"] for c in entry["noise"]["contributors"]] == [
        "m1_id",
        "r1_thermal",
    ]
    assert {c["mechanism"] for c in entry["noise"]["contributors"]} == {"thermal"}
    assert entry["value"] == pytest.approx(math.sqrt(9 + 4 + 4))


def test_a_mechanism_entry_without_top_n_is_unchanged(tmp_path):
    """No `contributors` key, and the same `noise` object #5553 shipped."""
    spec = _spec(2, mechanism="thermal")
    del spec["noise"]["top_n"]
    outcomes = _extract(tmp_path, [spec])
    ((_, outcome),) = outcomes.items()
    assert outcome.contributors is None
    assert "contributors" not in outcome.report()
    (entry,) = _grade([spec], {}, outcomes)[0]
    assert set(entry["noise"]) == {
        "mechanism",
        "total",
        "unclassified",
        "residual_relative",
        "unclassified_suffixes",
    }


@pytest.mark.parametrize(
    "rawfile, token",
    [
        (None, "noise_mechanism_rawfile_missing"),
        ("not a rawfile\n", "noise_mechanism_unreadable"),
    ],
)
def test_an_unreadable_rawfile_costs_a_total_entry_only_a_warning(
    tmp_path, rawfile, token
):
    if rawfile is not None:
        (tmp_path / "noise_mechanism.raw").write_text(rawfile)
    specs = [_spec(2)]
    outcomes = sim_noise_mechanism.extract_corner(
        sim_noise.lower_measurements(specs, LIN), str(tmp_path), LIN
    )
    results, diagnostics, _ = _grade(specs, {"vn": 7.0}, outcomes)
    (entry,) = results
    assert entry["value"] == 7.0 and entry["status"] == "pass"
    assert "noise" not in entry
    (diagnostic,) = diagnostics
    assert diagnostic["severity"] == "warning"
    assert diagnostic["message"].startswith(f"[{token}]")
    assert "'vn'" in diagnostic["message"]


def test_an_unreadable_rawfile_is_still_an_error_for_a_mechanism_entry(tmp_path):
    specs = [_spec(2, mechanism="thermal")]
    outcomes = sim_noise_mechanism.extract_corner(
        sim_noise.lower_measurements(specs, LIN), str(tmp_path), LIN
    )
    results, diagnostics, _ = _grade(specs, {}, outcomes)
    assert results[0]["status"] == "error"
    assert diagnostics[0]["severity"] == "error"
    assert diagnostics[0]["message"].startswith("[noise_mechanism_rawfile_missing]")


def test_a_plot_without_contributors_is_a_warning_for_a_total_entry(tmp_path):
    names = ["frequency", "inoise_spectrum", "onoise_spectrum"]
    specs = [_spec(2)]
    outcomes = _extract(tmp_path, specs, names=names, row=[None, 1.0, 1.0])
    results, diagnostics, _ = _grade(specs, {"vn": 1.0}, outcomes)
    assert results[0]["status"] == "pass" and "noise" not in results[0]
    (diagnostic,) = diagnostics
    assert diagnostic["severity"] == "warning"
    assert diagnostic["message"].startswith("[noise_mechanism_no_contributors]")


def test_an_entry_with_no_value_gets_no_second_diagnostic(tmp_path):
    """The engine failed, so the entry already carries its own error."""
    specs = [_spec(2)]
    outcomes = sim_noise_mechanism.extract_corner(
        sim_noise.lower_measurements(specs, LIN), str(tmp_path), LIN
    )
    results, diagnostics, _ = _grade(specs, {}, outcomes)
    assert results[0]["status"] == "error"
    assert [d["severity"] for d in diagnostics] == ["error"]


def test_a_request_without_top_n_has_no_extra_diagnostic_or_member(tmp_path):
    plain = {"name": "n0", "noise": {"quantity": "onoise", "at_hz": 2.0}}
    results, diagnostics, _ = _grade([plain], {"n0": 1.0}, None)
    assert diagnostics == [] and "noise" not in results[0]


def test_top_n_entries_are_selected_by_the_spectra_predicate():
    """`requested` is what `sim_noise_mechanism.reads_spectra` widens on."""
    assert sim_noise_contributors.requested(_spec(2))
    assert not sim_noise_contributors.requested({"name": "x", "expr": "v(out)"})
    assert sim_noise_mechanism.reads_spectra(_spec(2))
    assert sim_noise_mechanism.reads_spectra(_spec(2, mechanism="flicker"))
