"""``measurements[].noise.mechanism`` -- per-mechanism noise shares (#5553).

Validation, deck emission and the post-run reduction, all without an engine.
The vector namespace these reductions read is covered by
`test_sim_noise_leaves.py`; the end-to-end runs are
`test_sim_noise_mechanism_ngspice.py`.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from klayout_tools import sim, sim_noise, sim_noise_mechanism

#: With the trailing `pts_per_summary` token -- what a mechanism entry needs.
ARGS = "v(out) V1 dec 20 10 1e7 1"
ANALYSIS = {"kind": "noise", "args": ARGS}
#: Without it: ngspice emits only the total spectrum.
ANALYSIS_NO_PTS = {"kind": "noise", "args": "v(out) V1 dec 20 10 1e7"}
#: A linear sweep, for the synthetic rawfiles below.
LIN = {"kind": "noise", "args": "v(out) V1 lin 3 1 3 1"}


def _spec(mechanism="thermal", **noise):
    body = {"quantity": "onoise", "mechanism": mechanism, **noise}
    if "at_hz" not in body:
        body.setdefault("band_hz", [100, 1e6])
    return {"name": "n0", "noise": body}


def _validate(spec, analysis=ANALYSIS):
    sim._validate_measurement_forms([spec])
    sim_noise.validate_noise_context([spec], analysis)


# ---------------------------------------------------------------- validation


def test_a_valid_mechanism_entry_is_accepted():
    _validate(_spec("thermal"))
    _validate(_spec("flicker", at_hz=1e3))


def test_unknown_mechanism_value_is_refused_naming_the_entry():
    with pytest.raises(sim.SimError, match=r"n0.*must be one of") as exc:
        _validate(_spec("shot"))
    assert "'thermal'" in str(exc.value) and "'flicker'" in str(exc.value)


def test_inoise_mechanism_is_refused_with_the_reason():
    spec = {
        "name": "n0",
        "noise": {"quantity": "inoise", "at_hz": 1e3, "mechanism": "flicker"},
    }
    with pytest.raises(sim.SimError, match="per-contributor input-referred") as exc:
        _validate(spec)
    assert "n0" in str(exc.value)


def test_missing_pts_per_summary_is_refused_naming_the_fix():
    with pytest.raises(sim.SimError, match="pts_per_summary") as exc:
        _validate(_spec("thermal"), ANALYSIS_NO_PTS)
    message = str(exc.value)
    assert "n0" in message
    # The corrective message hands back the caller's own args plus the token.
    assert "'v(out) V1 dec 20 10 1e7 1'" in message


def test_the_total_form_still_needs_no_pts_per_summary():
    """Only a `mechanism` entry needs the argument; #2938's form is untouched."""
    _validate(
        {"name": "n0", "noise": {"quantity": "onoise", "at_hz": 1e3}}, ANALYSIS_NO_PTS
    )


@pytest.mark.parametrize(
    "args, expected",
    [
        ("v(out) V1 dec 20 10 1e7 1", True),
        ("v(out) V1 dec 20 10 1e7", False),
        ("v(out) V1 lin 100 10 1e5 4", True),
        ("v(out) V1 oct 10 1k 10meg", False),
        ("", False),
        ("nonsense", False),
    ],
)
def test_pts_per_summary_detection(args, expected):
    assert sim_noise_mechanism.has_pts_per_summary({"args": args}) is expected


# ------------------------------------------------------------------ emission


def _control(specs, analysis=ANALYSIS, corner_dir="/corner"):
    lowered = sim_noise.lower_measurements(specs, analysis)
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=lowered,
        raw_path=None,
        osdi_preload=(),
        corner_dir=corner_dir,
    )


def test_a_mechanism_entry_dumps_the_spectra_plot_and_restores_the_current_one():
    lines = _control([_spec("thermal")])
    assert "set filetype=ascii" in lines
    dump = lines.index("write /corner/noise_mechanism.raw")
    assert lines[dump - 1] == "setplot noise1"
    assert lines[dump - 2] == "set __klt_nzm_plot = $curplot"
    assert lines[dump + 1] == "setplot $__klt_nzm_plot"


def test_a_mechanism_entry_is_not_lowered_to_a_let_print_pair():
    spec = _spec("flicker")
    (lowered,) = sim_noise.lower_measurements([spec], ANALYSIS)
    assert "expr" not in lowered and "expr_prelude" not in lowered
    assert lowered == spec
    assert not any(line.startswith("let n0 =") for line in _control([spec]))


def test_one_dump_group_however_many_mechanism_entries_are_declared():
    specs = [
        {
            "name": "t",
            "noise": {
                "quantity": "onoise",
                "band_hz": [100, 1e6],
                "mechanism": "thermal",
            },
        },
        {
            "name": "f",
            "noise": {
                "quantity": "onoise",
                "band_hz": [100, 1e6],
                "mechanism": "flicker",
            },
        },
    ]
    lines = _control(specs)
    assert lines.count("write /corner/noise_mechanism.raw") == 1


def test_a_request_without_a_mechanism_emits_a_byte_identical_deck():
    """The #2938 total form, and every other request, must be untouched."""
    total = {"name": "n0", "noise": {"quantity": "onoise", "band_hz": [100, 1e6]}}
    lines = _control([total])
    assert not any("noise_mechanism.raw" in line for line in lines)
    assert not any("setplot" in line for line in lines)
    assert "let n0 = " in "\n".join(lines)


def test_the_total_and_mechanism_forms_compose_in_one_request():
    specs = [
        {"name": "total", "noise": {"quantity": "onoise", "band_hz": [100, 1e6]}},
        _spec("thermal") | {"name": "n0"},
    ]
    lines = _control(specs)
    assert any(line.startswith("let total = ") for line in lines)
    assert "write /corner/noise_mechanism.raw" in lines
    # The spectra dump comes after the `expr` lets that alias `noise1` vectors,
    # so moving the current plot cannot change what they read.
    assert lines.index("print total") < lines.index("setplot noise1")


# ----------------------------------------------------------------- reduction


def _rawfile(tmp_path: Path, names: list[str], rows: list[list[float]]) -> Path:
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
    path = tmp_path / "noise_mechanism.raw"
    path.write_text("\n".join(body) + "\n")
    return path


def _reduce(tmp_path, names, rows, mechanism="thermal", **noise):
    _rawfile(tmp_path, names, rows)
    spec = {
        "name": "n0",
        "noise": {"quantity": "onoise", "mechanism": mechanism, **noise},
    }
    spec["noise"].setdefault("band_hz", [1.0, 3.0])
    outcomes = sim_noise_mechanism.extract_corner([spec], str(tmp_path), LIN)
    return outcomes[0]


def test_the_mechanism_share_is_the_power_sum_of_its_leaves(tmp_path):
    names = [
        "frequency",
        "onoise_m1",
        "onoise_m1_id",
        "onoise_m1_1overf",
        "onoise_spectrum",
    ]
    # Device total and onoise_spectrum both carry sqrt(3**2 + 4**2) = 5.
    rows = [[f, 5.0, 3.0, 4.0, 5.0] for f in (1.0, 2.0, 3.0)]
    thermal = _reduce(tmp_path, names, rows, "thermal")
    flicker = _reduce(tmp_path, names, rows, "flicker")
    assert thermal.value == pytest.approx(3.0 * math.sqrt(2.0))
    assert flicker.value == pytest.approx(4.0 * math.sqrt(2.0))
    # The device total (5.0) was excluded -- otherwise this would be 5*sqrt(2).
    assert thermal.total == pytest.approx(5.0 * math.sqrt(2.0))
    assert thermal.residual_relative == pytest.approx(0.0, abs=1e-12)
    assert not thermal.incomplete
    assert thermal.unclassified == pytest.approx(0.0)


def test_a_mechanism_with_no_leaves_at_all_is_zero_not_an_error(tmp_path):
    names = ["frequency", "onoise_r1", "onoise_r1_thermal", "onoise_spectrum"]
    rows = [[f, 2.0, 2.0, 2.0] for f in (1.0, 2.0, 3.0)]
    flicker = _reduce(tmp_path, names, rows, "flicker")
    assert flicker.value == 0.0
    assert flicker.code is None and flicker.error is None
    thermal = _reduce(tmp_path, names, rows, "thermal")
    assert thermal.value == pytest.approx(thermal.total)


def test_an_unclassified_leaf_carrying_power_raises_the_diagnostic(tmp_path):
    names = [
        "frequency",
        "onoise_d1",
        "onoise_d1_rs",
        "onoise_d1_id",
        "onoise_spectrum",
    ]
    rows = [[f, 5.0, 3.0, 4.0, 5.0] for f in (1.0, 2.0, 3.0)]
    outcome = _reduce(tmp_path, names, rows, "thermal")
    assert outcome.incomplete
    assert outcome.unclassified_suffixes == ("id",)
    assert outcome.unclassified == pytest.approx(4.0 * math.sqrt(2.0))
    message = outcome.incomplete_message("n0")
    assert "id" in message and "n0" in message


def test_an_unclassified_leaf_carrying_no_power_stays_quiet(tmp_path):
    """sky130 emits `igb`/`igd`/`igs` for every MOSFET but they are identically
    zero at ordinary biases -- naming them would make the diagnostic useless."""
    names = [
        "frequency",
        "onoise_m1",
        "onoise_m1_id",
        "onoise_m1_igb",
        "onoise_spectrum",
    ]
    rows = [[f, 3.0, 3.0, 0.0, 3.0] for f in (1.0, 2.0, 3.0)]
    outcome = _reduce(tmp_path, names, rows, "thermal")
    assert outcome.unclassified_suffixes == ()
    assert not outcome.incomplete


def test_a_leaf_family_missing_from_the_plot_shows_up_as_a_residual(tmp_path):
    """onoise_spectrum says 5.0 but the only leaf carries 3.0: the decomposition
    is structurally incomplete and must not pass silently."""
    names = ["frequency", "onoise_m1", "onoise_m1_id", "onoise_spectrum"]
    rows = [[f, 3.0, 3.0, 5.0] for f in (1.0, 2.0, 3.0)]
    outcome = _reduce(tmp_path, names, rows, "thermal")
    assert outcome.residual_relative == pytest.approx(0.4)
    assert outcome.incomplete
    assert "4.000e-01" in outcome.incomplete_message("n0")


def test_a_missing_rawfile_is_that_measurements_own_error(tmp_path):
    spec = _spec("thermal")
    (outcome,) = sim_noise_mechanism.extract_corner(
        [spec], str(tmp_path), ANALYSIS
    ).values()
    assert outcome.value is None
    assert outcome.code == "noise_mechanism_rawfile_missing"
    assert "n0" in outcome.error


def test_a_plot_without_contributors_names_pts_per_summary(tmp_path):
    names = ["frequency", "inoise_spectrum", "onoise_spectrum"]
    rows = [[f, 1.0, 1.0] for f in (1.0, 2.0, 3.0)]
    outcome = _reduce(tmp_path, names, rows, "thermal")
    assert outcome.value is None
    assert outcome.code == "noise_mechanism_no_contributors"
    assert "pts_per_summary" in outcome.error


def test_an_unreadable_rawfile_is_that_measurements_own_error(tmp_path):
    (tmp_path / "noise_mechanism.raw").write_text("not a rawfile\n")
    spec = _spec("thermal")
    (outcome,) = sim_noise_mechanism.extract_corner(
        [spec], str(tmp_path), ANALYSIS
    ).values()
    assert outcome.value is None and outcome.code == "noise_mechanism_unreadable"


def test_only_mechanism_entries_are_reduced(tmp_path):
    specs = [
        {"name": "total", "noise": {"quantity": "onoise", "band_hz": [1.0, 3.0]}},
        _spec("thermal"),
        {"name": "other", "expr": "v(out)"},
    ]
    names = ["frequency", "onoise_m1", "onoise_m1_id", "onoise_spectrum"]
    _rawfile(tmp_path, names, [[f, 3.0, 3.0, 3.0] for f in (1.0, 2.0, 3.0)])
    outcomes = sim_noise_mechanism.extract_corner(specs, str(tmp_path), LIN)
    assert set(outcomes) == {1}  # position of the mechanism entry only
