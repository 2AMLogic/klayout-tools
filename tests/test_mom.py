"""Tests for `klt mom` and the `run_mom` library function (issue #718).

Requires the `klt_mom_native` Rust extension to be built and importable
(`maturin develop --release` in `native/mom/` -- see
`docs/cli/mom.md#building-the-native-extension`); every test in this module
is skipped with a clear reason (never silently passed) when it is not, so
CI environments that have not yet wired up the Rust toolchain degrade
gracefully instead of failing.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.mom import MomError, run_mom

pytest.importorskip(
    "klt_mom_native",
    reason=(
        "klt_mom_native is not built -- run `maturin develop --release` in "
        "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
    ),
)


def _um(v: float) -> int:
    return int(round(v / 0.001))


def _parallel_plate_fixture(path) -> None:
    """Two identical 2x2 um flat plates, 1 um apart -- layer 1/0 = "top" at
    z=1.0um, layer 2/0 = "bottom" at z=0.0um."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(2)))
    top.shapes(layout.layer(2, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(2)))
    layout.write(str(path))


def _plate_spec(path) -> None:
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 3.9,
                "panel_size_um": 0.5,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "top",
                        "z0_um": 1.0,
                        "z1_um": 1.0,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "bottom",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )


def _coax_fixture(path) -> None:
    """A square inner conductor surrounded by a four-wall square "shield" --
    the MVP's rectangle-only approximation of a coax cross-section (real
    annular geometry needs polygon-with-hole support, a follow-up -- see
    docs/cli/mom.md's "Worked example: coax"). The inner box sits at
    x/y in [4,5]; the four wall segments (on separate layers, all named the
    same conductor) trace a square ring at x/y in [0,9] with 1um-thick walls.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(5, 0)).insert(kdb.Box.new(_um(4), _um(4), _um(5), _um(5)))
    walls = {
        10: (0, 8, 9, 9),  # north
        11: (0, 0, 9, 1),  # south
        12: (8, 1, 9, 8),  # east
        13: (0, 1, 1, 8),  # west
    }
    for layer_num, (x0, y0, x1, y1) in walls.items():
        top.shapes(layout.layer(layer_num, 0)).insert(
            kdb.Box.new(_um(x0), _um(y0), _um(x1), _um(y1))
        )
    layout.write(str(path))


def _coax_spec(path) -> None:
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 0.5,
                "stackup": [
                    {
                        "layer": "5/0",
                        "conductor": "inner",
                        "z0_um": 0.0,
                        "z1_um": 0.5,
                    },
                    *(
                        {
                            "layer": f"{layer_num}/0",
                            "conductor": "outer",
                            "z0_um": 0.0,
                            "z1_um": 0.5,
                        }
                        for layer_num in (10, 11, 12, 13)
                    ),
                ],
            }
        )
    )


# --- library: parallel-plate geometry ---------------------------------------


def test_run_mom_parallel_plate(tmp_path):
    gds = tmp_path / "plates.gds"
    spec = tmp_path / "plates.mom.json"
    _parallel_plate_fixture(gds)
    _plate_spec(spec)

    report = run_mom(str(gds), str(spec))

    assert report["schema_version"] == 2
    assert report["file"] == str(gds)
    assert report["spec"] == str(spec)
    assert report["background_permittivity"] == 3.9
    assert report["panel_size_um"] == 0.5
    assert report["conductors"] == ["top", "bottom"]
    assert report["panel_count"] > 0

    c = report["capacitance_matrix_ff"]
    assert len(c) == 2 and all(len(row) == 2 for row in c)
    # Diagonal (self) terms are positive.
    assert c[0][0] > 0
    assert c[1][1] > 0
    # Symmetric Maxwell capacitance matrix.
    assert c[0][1] == pytest.approx(c[1][0], rel=1e-9)
    # Mutual term is negative (standard sign convention -- see
    # docs/cli/mom.md's "Reading the matrix").
    assert c[0][1] < 0
    # A well-resolved solve reports no physicality warnings.
    assert report["warnings"] == []


def test_run_mom_closer_plates_increase_capacitance(tmp_path):
    """Sanity check standing in for full closed-form validation (owned by
    #719): halving the plate separation should not *decrease* the magnitude
    of the self-capacitance."""
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)

    near_spec = tmp_path / "near.json"
    near_spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 0.5,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "top",
                        "z0_um": 0.5,
                        "z1_um": 0.5,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "bottom",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )
    far_spec = tmp_path / "far.json"
    far_spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 0.5,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "top",
                        "z0_um": 5.0,
                        "z1_um": 5.0,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "bottom",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )

    near = run_mom(str(gds), str(near_spec))
    far = run_mom(str(gds), str(far_spec))
    assert near["capacitance_matrix_ff"][0][0] > far["capacitance_matrix_ff"][0][0]


# --- library: coax-approximation geometry -----------------------------------


def test_run_mom_coax(tmp_path):
    gds = tmp_path / "coax.gds"
    spec = tmp_path / "coax.mom.json"
    _coax_fixture(gds)
    _coax_spec(spec)

    report = run_mom(str(gds), str(spec))

    assert report["conductors"] == ["inner", "outer"]
    c = report["capacitance_matrix_ff"]
    assert c[0][0] > 0
    assert c[1][1] > 0
    assert c[0][1] == pytest.approx(c[1][0], rel=1e-9)


# --- edge cases (test plan: single-conductor, mismatched scale) ------------


def test_run_mom_single_conductor_degenerate_case(tmp_path):
    gds = tmp_path / "solo.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(2)))
    layout.write(str(gds))

    spec = tmp_path / "solo.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "only",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )

    report = run_mom(str(gds), str(spec))
    assert report["conductors"] == ["only"]
    assert report["capacitance_matrix_ff"] == [
        [pytest.approx(report["capacitance_matrix_ff"][0][0])]
    ]
    assert report["capacitance_matrix_ff"][0][0] > 0


def test_run_mom_under_resolved_solve_reports_a_warning(tmp_path):
    """Panels far wider than the gap they face break the point-collocation
    fill down badly enough to flip the mutual term's sign. The command still
    returns numbers (its bar is "produces a numeric result"), but must say
    plainly that they are not physical -- see docs/cli/mom.md's "Warnings"."""
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)

    spec = tmp_path / "coarse.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                # 2 um panels across a 0.01 um plate gap.
                "panel_size_um": 2.0,
                "stackup": [
                    {"layer": "1/0", "conductor": "top", "z0_um": 0.01, "z1_um": 0.01},
                    {
                        "layer": "2/0",
                        "conductor": "bottom",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )

    report = run_mom(str(gds), str(spec))
    assert report["warnings"], report
    assert all("panel_size_um" in w for w in report["warnings"])
    joined = " ".join(report["warnings"])
    assert "top" in joined and "bottom" in joined


def test_run_mom_mismatched_scale_rejected_cleanly(tmp_path):
    """A tiny conductor's panel_size_um reused against a much larger
    conductor in the same request implies an astronomical panel count --
    must fail fast with MomError, not hang or OOM (test plan edge case)."""
    gds = tmp_path / "mismatch.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(
        kdb.Box.new(_um(0), _um(0), _um(0.002), _um(0.002))
    )
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(_um(0), _um(0), _um(1000), _um(1000))
    )
    layout.write(str(gds))

    spec = tmp_path / "mismatch.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 0.001,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "tiny",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "big",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )

    with pytest.raises(MomError, match="panels"):
        run_mom(str(gds), str(spec))


# --- error paths -------------------------------------------------------------


def test_run_mom_missing_spec_file(tmp_path):
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)

    with pytest.raises(MomError, match="spec file not found"):
        run_mom(str(gds), str(tmp_path / "nope.json"))


def test_run_mom_spec_missing_permittivity(tmp_path):
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)
    spec = tmp_path / "bad.json"
    spec.write_text(
        json.dumps(
            {"stackup": [{"layer": "1/0", "conductor": "a", "z0_um": 0, "z1_um": 0}]}
        )
    )

    with pytest.raises(MomError, match="background_permittivity"):
        run_mom(str(gds), str(spec))


def test_run_mom_conductor_matches_no_shapes(tmp_path):
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)
    spec = tmp_path / "bad.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "stackup": [
                    {
                        "layer": "99/0",
                        "conductor": "ghost",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )

    with pytest.raises(MomError, match="matched no shapes"):
        run_mom(str(gds), str(spec))


def test_run_mom_ambiguous_top_cell_requires_flag(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    a = layout.create_cell("A")
    a.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(2)))
    a.shapes(layout.layer(2, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(2), _um(2)))
    # A second, unrelated top cell -- just enough to make the stream
    # multi-top, so run_mom() must be told which one to discretise.
    layout.create_cell("B")
    gds = tmp_path / "multi_top.gds"
    layout.write(str(gds))

    spec = tmp_path / "spec.json"
    _plate_spec(spec)

    with pytest.raises(MomError, match="exactly one top cell"):
        run_mom(str(gds), str(spec))

    # Passing --top disambiguates.
    report = run_mom(str(gds), str(spec), top="A")
    assert report["conductors"] == ["top", "bottom"]


# --- CLI ----------------------------------------------------------------


def test_cli_json_contract(tmp_path, capsys):
    gds = tmp_path / "plates.gds"
    spec = tmp_path / "plates.mom.json"
    _parallel_plate_fixture(gds)
    _plate_spec(spec)

    assert main(["mom", str(gds), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "spec",
        "background_permittivity",
        "panel_size_um",
        "conductors",
        "capacitance_matrix_ff",
        "panel_count",
        "warnings",
    }
    assert data["schema_version"] == 2


def test_cli_text_output(tmp_path, capsys):
    gds = tmp_path / "plates.gds"
    spec = tmp_path / "plates.mom.json"
    _parallel_plate_fixture(gds)
    _plate_spec(spec)

    assert main(["mom", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "top" in out
    assert "bottom" in out
    assert "femtofarads" in out


def test_cli_text_output_renders_warnings(tmp_path, capsys):
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)
    spec = tmp_path / "coarse.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 2.0,
                "stackup": [
                    {"layer": "1/0", "conductor": "top", "z0_um": 0.01, "z1_um": 0.01},
                    {
                        "layer": "2/0",
                        "conductor": "bottom",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
            }
        )
    )

    # A physicality warning is a diagnostic, not a failure -- exit stays 0.
    assert main(["mom", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "warnings:" in out
    assert "panel_size_um" in out


def test_cli_error_exits_one_with_clean_message(tmp_path, capsys):
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)

    assert main(["mom", str(gds), str(tmp_path / "nope.json")]) == 1
    err = capsys.readouterr().err
    assert "spec file not found" in err
    assert "Traceback" not in err


def test_cli_json_error_shape(tmp_path, capsys):
    gds = tmp_path / "plates.gds"
    _parallel_plate_fixture(gds)

    assert main(["mom", str(gds), str(tmp_path / "nope.json"), "--format", "json"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "mom"
    assert "spec file not found" in err["error"]["message"]


# --- PEEC inductance/resistance (issue #797) --------------------------------


def _wire_fixture(path) -> None:
    """A single 100x2 um bar on layer 1/0 -- becomes a 100x2x2 um bar once
    the spec's z0_um/z1_um give it thickness."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(100), _um(2)))
    layout.write(str(path))


def _wire_spec(path, *, conductivity: float | None = 5.96e7) -> None:
    entry = {"layer": "1/0", "conductor": "wire", "z0_um": 0.0, "z1_um": 2.0}
    if conductivity is not None:
        entry["conductivity_S_per_m"] = conductivity
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                # Coarse on purpose -- these tests are about the PEEC
                # inductance/resistance path, not capacitance accuracy; a
                # long, thick (real 3-D, non-flat) bar at the default
                # panel_size_um would trip the capacitance solver's own
                # panel-count guard (docs/cli/mom.md's "Panel-count guard").
                "panel_size_um": 5.0,
                "compute_inductance": True,
                "filament_size_um": 0.5,
                "stackup": [entry],
            }
        )
    )


def test_run_mom_peec_straight_wire(tmp_path):
    gds = tmp_path / "wire.gds"
    spec = tmp_path / "wire.mom.json"
    _wire_fixture(gds)
    _wire_spec(spec)

    report = run_mom(str(gds), str(spec))

    assert report["schema_version"] == 2
    assert report["conductors"] == ["wire"]
    assert report["filament_size_um"] == 0.5
    assert report["filament_count"] > 0
    l_nh = report["inductance_matrix_nh"]
    assert len(l_nh) == 1 and len(l_nh[0]) == 1
    assert l_nh[0][0] > 0
    r = report["resistance_ohm"]
    assert len(r) == 1
    length_m = 100e-6
    area_m2 = 2e-6 * 2e-6
    expected_r = length_m / (5.96e7 * area_m2)
    assert r[0] == pytest.approx(expected_r, rel=1e-9)


def test_run_mom_without_compute_inductance_omits_peec_fields(tmp_path):
    """The default (compute_inductance unset) must keep the original
    capacitance-only response shape exactly -- no new keys at all."""
    gds = tmp_path / "wire.gds"
    spec = tmp_path / "wire-cap-only.mom.json"
    _wire_fixture(gds)
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "stackup": [
                    {"layer": "1/0", "conductor": "wire", "z0_um": 0.0, "z1_um": 2.0}
                ],
            }
        )
    )

    report = run_mom(str(gds), str(spec))
    assert "inductance_matrix_nh" not in report
    assert "resistance_ohm" not in report
    assert "filament_count" not in report
    assert "filament_size_um" not in report


def test_run_mom_peec_missing_conductivity_is_a_clear_error(tmp_path):
    gds = tmp_path / "wire.gds"
    spec = tmp_path / "wire.mom.json"
    _wire_fixture(gds)
    _wire_spec(spec, conductivity=None)

    with pytest.raises(MomError, match="conductivity_S_per_m|conductivity_s_per_m"):
        run_mom(str(gds), str(spec))


def test_run_mom_peec_rejects_non_bar_shaped_conductor(tmp_path):
    """A square pad (aspect ratio ~1:1) has no well-defined current-flow
    direction under this MVP's model."""
    gds = tmp_path / "pad.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(5), _um(5)))
    layout.write(str(gds))

    spec = tmp_path / "pad.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "compute_inductance": True,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "pad",
                        "z0_um": 0.0,
                        "z1_um": 5.0,
                        "conductivity_S_per_m": 5.96e7,
                    }
                ],
            }
        )
    )

    with pytest.raises(MomError, match="bar-shaped"):
        run_mom(str(gds), str(spec))


def test_run_mom_peec_loop(tmp_path):
    """Two long parallel bars (a 'loop' -- out on one, back on the other)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(500), _um(2)))
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(_um(0), _um(20), _um(500), _um(22))
    )
    gds = tmp_path / "loop.gds"
    layout.write(str(gds))

    spec = tmp_path / "loop.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                # Coarse on purpose -- see _wire_spec's comment.
                "panel_size_um": 5.0,
                "compute_inductance": True,
                "filament_size_um": 0.5,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "go",
                        "z0_um": 0.0,
                        "z1_um": 2.0,
                        "conductivity_S_per_m": 5.96e7,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "return",
                        "z0_um": 0.0,
                        "z1_um": 2.0,
                        "conductivity_S_per_m": 5.96e7,
                    },
                ],
            }
        )
    )

    report = run_mom(str(gds), str(spec))
    l_nh = report["inductance_matrix_nh"]
    assert l_nh[0][1] == pytest.approx(l_nh[1][0], rel=1e-9)
    assert l_nh[0][0] > l_nh[0][1] > 0


def test_cli_text_output_renders_peec_fields(tmp_path, capsys):
    gds = tmp_path / "wire.gds"
    spec = tmp_path / "wire.mom.json"
    _wire_fixture(gds)
    _wire_spec(spec)

    assert main(["mom", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "nanohenries" in out
    assert "resistance (ohms):" in out


# --- full-wave frequency sweep (issue #893) ---------------------------------


def _loop_fixture(
    path, *, length_um: float = 500.0, separation_um: float = 40.0
) -> None:
    """Two long, parallel bars -- the same "loop" shape as `_wire_spec`'s
    inductance sibling, reused as the full-wave solve's canonical
    two-conductor transmission-line structure."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(
        kdb.Box.new(_um(0), _um(0), _um(length_um), _um(2))
    )
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(_um(0), _um(separation_um), _um(length_um), _um(separation_um + 2))
    )
    layout.write(str(path))


def _loop_spec(
    path, *, frequencies_hz: list[float], ports: list[dict] | None = None
) -> None:
    entry = {"z0_um": 0.0, "z1_um": 2.0}
    spec: dict = {
        "background_permittivity": 1.0,
        "panel_size_um": 2.0,
        "frequencies_hz": frequencies_hz,
        "segment_size_um": 5.0,
        "stackup": [
            {"layer": "1/0", "conductor": "go", **entry},
            {"layer": "2/0", "conductor": "return", **entry},
        ],
    }
    if ports is not None:
        spec["ports"] = ports
    path.write_text(json.dumps(spec))


def test_run_mom_full_wave_sweep(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    _loop_spec(spec, frequencies_hz=[1.0e6, 1.0e9])

    report = run_mom(str(gds), str(spec))

    assert report["schema_version"] == 2
    assert report["segment_size_um"] == 5.0
    assert report["full_wave_segment_count"] > 0
    sweep = report["full_wave_sweep"]
    assert len(sweep) == 2
    assert [p["frequency_hz"] for p in sweep] == [1.0e6, 1.0e9]

    for point in sweep:
        z = point["impedance_matrix_real_ohm"]
        assert len(z) == 2 and len(z[0]) == 2
        # Symmetric partial-impedance matrix (same reciprocity argument as
        # the static inductance/capacitance matrices).
        assert point["impedance_matrix_real_ohm"][0][1] == pytest.approx(
            point["impedance_matrix_real_ohm"][1][0], rel=1e-9
        )
        assert point["impedance_matrix_imag_ohm"][0][1] == pytest.approx(
            point["impedance_matrix_imag_ohm"][1][0], rel=1e-9
        )
        # Two conductors -> the derived characteristic impedance/propagation
        # constant fields must be present.
        assert point["characteristic_impedance_real_ohm"] is not None
        assert point["characteristic_impedance_real_ohm"] > 0
        assert point["phase_rad_per_m"] > 0


def test_run_mom_without_frequencies_hz_omits_full_wave_fields(tmp_path):
    """The default (frequencies_hz unset) must keep the original response
    shape exactly -- no new keys at all, matching
    test_run_mom_without_compute_inductance_omits_peec_fields's precedent."""
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    _loop_spec(spec, frequencies_hz=[])

    report = run_mom(str(gds), str(spec))
    assert "full_wave_sweep" not in report
    assert "full_wave_segment_count" not in report
    assert "segment_size_um" not in report


def test_run_mom_full_wave_rejects_non_bar_shaped_conductor(tmp_path):
    """Same bar-shape restriction as PEEC's compute_inductance (#797),
    reused here -- a square pad has no well-defined current-flow axis."""
    gds = tmp_path / "pad.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(5), _um(5)))
    layout.write(str(gds))

    spec = tmp_path / "pad.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "frequencies_hz": [1.0e9],
                "stackup": [
                    {"layer": "1/0", "conductor": "pad", "z0_um": 0.0, "z1_um": 5.0}
                ],
            }
        )
    )

    with pytest.raises(MomError, match="bar-shaped"):
        run_mom(str(gds), str(spec))


def test_run_mom_full_wave_rejects_non_positive_frequency(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    _loop_spec(spec, frequencies_hz=[0.0])

    with pytest.raises(MomError, match="positive"):
        run_mom(str(gds), str(spec))


def test_cli_text_output_renders_full_wave_sweep(tmp_path, capsys):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    _loop_spec(spec, frequencies_hz=[1.0e6])

    assert main(["mom", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "full_wave_segment_count" in out
    assert "full-wave sweep:" in out
    assert "Z0=" in out


# --- ports and de-embedding (issue #894, Phase 2b) --------------------------


def test_run_mom_ports_adds_s_parameters(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    ports = [
        {"position_um": 0.0, "reference_impedance_ohm": 50.0},
        {"position_um": 500.0, "reference_impedance_ohm": 50.0},
    ]
    _loop_spec(spec, frequencies_hz=[1.0e9], ports=ports)

    report = run_mom(str(gds), str(spec))

    assert report["ports"] == ports
    point = report["full_wave_sweep"][0]
    s = point["s_parameters"]
    assert set(s) == {
        "s11_real",
        "s11_imag",
        "s12_real",
        "s12_imag",
        "s21_real",
        "s21_imag",
        "s22_real",
        "s22_imag",
    }
    # Reciprocal network: S12 == S21.
    assert s["s12_real"] == pytest.approx(s["s21_real"], abs=1e-9)
    assert s["s12_imag"] == pytest.approx(s["s21_imag"], abs=1e-9)


def test_run_mom_ports_default_reference_impedance_is_50_ohm(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    # Omit reference_impedance_ohm entirely -- must default to 50 ohm.
    ports = [{"position_um": 0.0}, {"position_um": 500.0}]
    _loop_spec(spec, frequencies_hz=[1.0e9], ports=ports)

    report = run_mom(str(gds), str(spec))
    assert report["ports"] == [
        {"position_um": 0.0, "reference_impedance_ohm": 50.0},
        {"position_um": 500.0, "reference_impedance_ohm": 50.0},
    ]


def test_run_mom_without_ports_omits_ports_and_s_parameters_fields(tmp_path):
    """The default (ports unset) must keep the original response shape
    exactly -- no new keys at all, matching
    test_run_mom_without_frequencies_hz_omits_full_wave_fields's precedent."""
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    _loop_spec(spec, frequencies_hz=[1.0e9])

    report = run_mom(str(gds), str(spec))
    assert "ports" not in report
    assert "s_parameters" not in report["full_wave_sweep"][0]


def test_run_mom_wrong_port_count_is_a_clear_error(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    _loop_spec(spec, frequencies_hz=[1.0e9], ports=[{"position_um": 0.0}])

    with pytest.raises(MomError, match="exactly two ports"):
        run_mom(str(gds), str(spec))


def test_run_mom_port_position_outside_modeled_span_is_a_clear_error(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)  # 500um long
    ports = [{"position_um": 0.0}, {"position_um": 600.0}]
    _loop_spec(spec, frequencies_hz=[1.0e9], ports=ports)

    with pytest.raises(MomError, match="outside the modeled bar span"):
        run_mom(str(gds), str(spec))


def test_run_mom_ports_without_frequencies_hz_is_a_clear_error(tmp_path):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    ports = [{"position_um": 0.0}, {"position_um": 500.0}]
    # frequencies_hz deliberately empty -- S-parameters are undefined
    # without a swept frequency.
    _loop_spec(spec, frequencies_hz=[], ports=ports)

    with pytest.raises(MomError, match="frequencies_hz"):
        run_mom(str(gds), str(spec))


def test_cli_text_output_renders_ports_and_s_parameters(tmp_path, capsys):
    gds = tmp_path / "loop.gds"
    spec = tmp_path / "loop.mom.json"
    _loop_fixture(gds)
    ports = [
        {"position_um": 0.0, "reference_impedance_ohm": 50.0},
        {"position_um": 500.0, "reference_impedance_ohm": 50.0},
    ]
    _loop_spec(spec, frequencies_hz=[1.0e9], ports=ports)

    assert main(["mom", str(gds), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "ports:" in out
    assert "reference_impedance_ohm=50" in out
    assert "S11=" in out
    assert "S21=" in out
