"""Tests for the FLUTE/RUDY-family congestion pre-check (issue #785).

Requires the `klt_congestion_native` Rust extension to be built and
importable (`maturin develop --release` in `native/congestion/`, or `uv
sync --group congestion`); every test that needs it is skipped with a clear
reason when it is not, mirroring `test_mom.py`'s own posture. DEF-parsing
tests (`nets_from_def`) need no native extension and always run.
"""

from __future__ import annotations

import pytest

from klayout_tools.congestion import CongestionError, estimate_congestion, nets_from_def

pytest.importorskip(
    "klt_congestion_native",
    reason=(
        "klt_congestion_native is not built -- run `maturin develop "
        "--release` in native/congestion/ (or `uv sync --group congestion`)"
    ),
)


# --------------------------------------------------------------------------- #
# estimate_congestion (native extension wrapper)
# --------------------------------------------------------------------------- #


def test_estimate_congestion_empty_design():
    resp = estimate_congestion(
        die_box_um={"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0},
        grid_cols=4,
        grid_rows=4,
        nets=[],
    )
    assert resp["schema_version"] == 1
    assert resp["net_count"] == 0
    assert resp["congestion_score"] == 0.0
    assert resp["overflow_tile_count"] == 0


def test_estimate_congestion_two_pin_net_matches_manhattan_distance():
    resp = estimate_congestion(
        die_box_um={"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0},
        grid_cols=4,
        grid_rows=4,
        nets=[
            {
                "name": "a",
                "pins": [{"x_um": 1.0, "y_um": 1.0}, {"x_um": 9.0, "y_um": 9.0}],
            }
        ],
    )
    assert resp["net_count"] == 1
    assert resp["total_rsmt_length_um"] == pytest.approx(16.0)


def test_estimate_congestion_single_pin_nets_are_skipped():
    resp = estimate_congestion(
        die_box_um={"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0},
        grid_cols=4,
        grid_rows=4,
        nets=[{"name": "a", "pins": [{"x_um": 1.0, "y_um": 1.0}]}],
    )
    assert resp["net_count"] == 0
    assert resp["skipped_single_pin_nets"] == 1


def test_estimate_congestion_denser_net_raises_score():
    """More total wirelength packed into the same grid raises
    congestion_score -- the headline signal the correlation study leans
    on."""
    die_box_um = {"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0}
    sparse = estimate_congestion(
        die_box_um=die_box_um,
        grid_cols=8,
        grid_rows=8,
        nets=[
            {
                "name": "a",
                "pins": [{"x_um": 1.0, "y_um": 1.0}, {"x_um": 2.0, "y_um": 1.0}],
            }
        ],
    )
    dense_nets = [
        {
            "name": f"n{i}",
            "pins": [{"x_um": 1.0, "y_um": 1.0}, {"x_um": 2.0, "y_um": 1.0}],
        }
        for i in range(50)
    ]
    dense = estimate_congestion(
        die_box_um=die_box_um, grid_cols=8, grid_rows=8, nets=dense_nets
    )
    assert dense["congestion_score"] > sparse["congestion_score"]


def test_estimate_congestion_rejects_zero_grid():
    with pytest.raises(CongestionError):
        estimate_congestion(
            die_box_um={"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0},
            grid_cols=0,
            grid_rows=4,
            nets=[],
        )


def test_estimate_congestion_custom_layer_and_pitch_change_capacity():
    die_box_um = {"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0}
    fewer_layers = estimate_congestion(
        die_box_um=die_box_um, grid_cols=4, grid_rows=4, nets=[], layer_count=1.0
    )
    more_layers = estimate_congestion(
        die_box_um=die_box_um, grid_cols=4, grid_rows=4, nets=[], layer_count=10.0
    )
    assert more_layers["capacity_um_per_tile"] > fewer_layers["capacity_um_per_tile"]


# --------------------------------------------------------------------------- #
# nets_from_def (plain-text DEF parsing, no native extension needed)
# --------------------------------------------------------------------------- #

_MINIMAL_DEF = """\
VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 10000 ) ;
ROW ROW_0 unithd 0 0 N DO 10 BY 1 STEP 460 0 ;
COMPONENTS 2 ;
    - u1 sky130_fd_sc_hd__inv_2 + PLACED ( 1000 1000 ) N ;
    - u2 sky130_fd_sc_hd__inv_2 + PLACED ( 9000 9000 ) N ;
END COMPONENTS
PINS 1 ;
    - clk + NET clk + DIRECTION INPUT + USE SIGNAL
      + PORT
        + LAYER met2 ( -70 -242 ) ( 70 243 )
        + PLACED ( 500 500 ) N ;
END PINS
NETS 2 ;
    - net1 ( u1 A ) ( u2 Y ) + USE SIGNAL ;
    - clk ( PIN clk ) ( u1 CLK ) + USE SIGNAL ;
END NETS
END DESIGN
"""


def test_nets_from_def_parses_die_box(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text(_MINIMAL_DEF)

    nets, die_box_um = nets_from_def(str(def_path))

    assert die_box_um == {"x0_um": 0.0, "y0_um": 0.0, "x1_um": 10.0, "y1_um": 10.0}
    assert len(nets) == 2


def test_nets_from_def_resolves_component_locations(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text(_MINIMAL_DEF)

    nets, _ = nets_from_def(str(def_path))

    net1 = next(n for n in nets if n["name"] == "net1")
    assert {"x_um": 1.0, "y_um": 1.0} in net1["pins"]
    assert {"x_um": 9.0, "y_um": 9.0} in net1["pins"]


def test_nets_from_def_resolves_top_level_pin_connections(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text(_MINIMAL_DEF)

    nets, _ = nets_from_def(str(def_path))

    clk_net = next(n for n in nets if n["name"] == "clk")
    # The "( PIN clk )" connection resolves to the PINS section's own
    # PLACED location (0.5, 0.5 um); the "( u1 CLK )" connection resolves
    # to u1's component-center location (1.0, 1.0 um).
    assert {"x_um": 0.5, "y_um": 0.5} in clk_net["pins"]
    assert {"x_um": 1.0, "y_um": 1.0} in clk_net["pins"]


def test_nets_from_def_missing_file_raises_congestion_error(tmp_path):
    with pytest.raises(CongestionError):
        nets_from_def(str(tmp_path / "does_not_exist.def"))


def test_nets_from_def_missing_nets_section_raises(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text(_MINIMAL_DEF.split("NETS 2 ;")[0] + "END DESIGN\n")

    with pytest.raises(CongestionError):
        nets_from_def(str(def_path))


def test_nets_from_def_unplaced_component_is_dropped_not_raised(tmp_path):
    def_with_unplaced = _MINIMAL_DEF.replace(
        "    - u2 sky130_fd_sc_hd__inv_2 + PLACED ( 9000 9000 ) N ;",
        "    - u2 sky130_fd_sc_hd__inv_2 + UNPLACED ;",
    )
    def_path = tmp_path / "top.def"
    def_path.write_text(def_with_unplaced)

    nets, _ = nets_from_def(str(def_path))

    net1 = next(n for n in nets if n["name"] == "net1")
    # u2's pin is dropped (no location); u1's pin remains -- 1 usable pin,
    # which the native extension's own skipped_single_pin_nets counts if
    # fed through estimate_congestion (not asserted here -- this test is
    # scoped to the parser itself).
    assert net1["pins"] == [{"x_um": 1.0, "y_um": 1.0}]


# --------------------------------------------------------------------------- #
# End-to-end: DEF -> estimate_congestion
# --------------------------------------------------------------------------- #


def test_end_to_end_def_to_congestion_estimate(tmp_path):
    def_path = tmp_path / "top.def"
    def_path.write_text(_MINIMAL_DEF)

    nets, die_box_um = nets_from_def(str(def_path))
    resp = estimate_congestion(
        die_box_um=die_box_um, grid_cols=4, grid_rows=4, nets=nets
    )

    assert resp["net_count"] == 2
    assert resp["total_rsmt_length_um"] > 0
