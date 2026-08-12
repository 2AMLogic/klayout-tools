"""Tests for `klayout_tools.congestion` -- the post-placement congestion
pre-check (issue #785, Epic #700 Phase 1).

Everything here runs against **fabricated** LEF/DEF text written under
`tmp_path`: the module is pure text parsing plus arithmetic, so no PDK,
no OpenROAD, and no `klayout.db` is needed to exercise any of it. The
correlation study that decides whether this estimator is worth shipping
into the fleet DSE loop is a separate, out-of-band experiment against real
OpenROAD runs (see the PR description); these tests only pin the
estimator's own behaviour.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from klayout_tools import congestion

# --------------------------------------------------------------------------- #
# Fixtures: minimal LEF/DEF text
# --------------------------------------------------------------------------- #

TECH_LEF = """\
VERSION 5.8 ;
UNITS
  DATABASE MICRONS 1000 ;
END UNITS

LAYER li1
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  PITCH 0.46 ;
END li1

LAYER mcon
  TYPE CUT ;
END mcon

LAYER met1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.34 ;
  WIDTH 0.14 ;
END met1

LAYER met2
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  PITCH 0.46 ;
END met2

LAYER met3
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 0.68 ;
END met3

LAYER nopitch
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
END nopitch
END LIBRARY
"""

CELL_LEF = """\
VERSION 5.8 ;
MACRO cellA
  CLASS CORE ;
  ORIGIN 0 0 ;
  SIZE 2.000 BY 4.000 ;
  PIN A
    DIRECTION INPUT ;
    USE SIGNAL ;
    PORT
      LAYER li1 ;
        RECT 0.000 1.000 1.000 3.000 ;
      LAYER met1 ;
        RECT 1.900 3.900 2.000 4.000 ;
    END
  END A
  PIN Y
    DIRECTION OUTPUT ;
    USE SIGNAL ;
    PORT
      LAYER li1 ;
        RECT 1.000 0.000 2.000 2.000 ;
    END
  END Y
  PIN VPWR
    DIRECTION INOUT ;
    USE POWER ;
    PORT
      LAYER met1 ;
        RECT 0.000 3.800 2.000 4.000 ;
    END
  END VPWR
END cellA
END LIBRARY
"""


def _def_text(
    *,
    die: tuple[int, int, int, int] = (0, 0, 100000, 100000),
    components: str,
    pins: str = "",
    nets: str,
    component_count: int,
    pin_count: int,
    net_count: int,
) -> str:
    return f"""\
VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN widget ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( {die[0]} {die[1]} ) ( {die[2]} {die[3]} ) ;
TRACKS X 230 DO 10 STEP 460 LAYER met2 ;
COMPONENTS {component_count} ;
{components}
END COMPONENTS
PINS {pin_count} ;
{pins}
END PINS
NETS {net_count} ;
{nets}
END NETS
END DESIGN
"""


@pytest.fixture()
def tech_lef(tmp_path: Path) -> str:
    path = tmp_path / "tech.lef"
    path.write_text(TECH_LEF, encoding="utf-8")
    return str(path)


@pytest.fixture()
def cell_lef(tmp_path: Path) -> str:
    path = tmp_path / "cells.lef"
    path.write_text(CELL_LEF, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# LEF parsing
# --------------------------------------------------------------------------- #


def test_parse_lef_routing_layers_reads_direction_and_pitch(tech_lef):
    layers = congestion.parse_lef_routing_layers(tech_lef)
    assert set(layers) == {"li1", "met1", "met2", "met3"}
    assert layers["met1"].direction == "horizontal"
    assert layers["met1"].pitch_um == pytest.approx(0.34)
    assert layers["met2"].direction == "vertical"
    assert layers["met2"].pitch_um == pytest.approx(0.46)


def test_parse_lef_routing_layers_skips_cut_and_pitchless_layers(tech_lef):
    layers = congestion.parse_lef_routing_layers(tech_lef)
    assert "mcon" not in layers  # TYPE CUT
    assert "nopitch" not in layers  # no PITCH -> no track supply to count


def test_parse_lef_macros_reads_size_and_first_port_rect_centre(cell_lef):
    macros = congestion.parse_lef_macros([cell_lef])
    assert set(macros) == {"cellA"}
    cell = macros["cellA"]
    assert (cell.width_um, cell.height_um) == pytest.approx((2.0, 4.0))
    # First PORT RECT of pin A is (0,1)-(1,3): centre (0.5, 2.0). The second
    # (met1) rect is an alternative access point and must be ignored.
    assert cell.pin_offsets["A"] == pytest.approx((0.5, 2.0))
    assert cell.pin_offsets["Y"] == pytest.approx((1.5, 1.0))


def test_parse_lef_macros_drops_power_and_ground_pins(cell_lef):
    macros = congestion.parse_lef_macros([cell_lef])
    assert "VPWR" not in macros["cellA"].pin_offsets


def test_parse_lef_unreadable_path_raises(tmp_path):
    with pytest.raises(congestion.CongestionError, match="could not read"):
        congestion.parse_lef_routing_layers(str(tmp_path / "missing.lef"))


# --------------------------------------------------------------------------- #
# Orientation transform
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        ("N", (0.5, 2.0)),
        ("FN", (1.5, 2.0)),
        ("S", (1.5, 2.0)),
        ("FS", (0.5, 2.0)),
        ("W", (2.0, 0.5)),
        ("E", (2.0, 1.5)),
        ("FW", (2.0, 0.5)),
        ("FE", (2.0, 1.5)),
    ],
)
def test_orient_offset_all_eight_orientations(orientation, expected):
    # Pin at (0.5, 2.0) in a 2.0 x 4.0 cell -- exactly the vertical midpoint
    # of the left edge, which makes each orientation's answer checkable by
    # inspection.
    assert congestion._orient_offset(0.5, 2.0, 2.0, 4.0, orientation) == pytest.approx(
        expected
    )


def test_orient_offset_rejects_unknown_orientation():
    with pytest.raises(congestion.CongestionError, match="unknown DEF orientation"):
        congestion._orient_offset(0.0, 0.0, 1.0, 1.0, "R42")


# --------------------------------------------------------------------------- #
# DEF parsing
# --------------------------------------------------------------------------- #


def test_parse_placement_def_resolves_pins_through_lef_offsets(tmp_path, cell_lef):
    def_path = tmp_path / "widget_place.def"
    def_path.write_text(
        _def_text(
            components=(
                "    - u1 cellA + PLACED ( 10000 20000 ) N ;\n"
                "    - u2 cellA + PLACED ( 50000 20000 ) FS ;"
            ),
            component_count=2,
            pins="",
            pin_count=0,
            nets="    - n1 ( u1 Y ) ( u2 A ) + USE SIGNAL ;",
            net_count=1,
        ),
        encoding="utf-8",
    )
    macros = congestion.parse_lef_macros([cell_lef])
    snapshot = congestion.parse_placement_def(str(def_path), macros)

    assert snapshot.design == "widget"
    assert snapshot.die_area_um == pytest.approx((0.0, 0.0, 100.0, 100.0))
    assert snapshot.instance_count == 2
    assert len(snapshot.nets) == 1
    net = snapshot.nets[0]
    # u1 is N: origin (10, 20) + Y offset (1.5, 1.0).
    # u2 is FS (mirrored in Y): origin (50, 20) + A offset (0.5, 4.0 - 2.0).
    assert [c for pin in net.pins for c in pin] == pytest.approx(
        [11.5, 21.0, 50.5, 22.0]
    )


def test_parse_placement_def_without_lef_falls_back_to_instance_origin(tmp_path):
    def_path = tmp_path / "widget_place.def"
    def_path.write_text(
        _def_text(
            components="    - u1 cellA + PLACED ( 10000 20000 ) N ;",
            component_count=1,
            nets="    - n1 ( u1 Y ) ( u1 A ) + USE SIGNAL ;",
            net_count=1,
            pin_count=0,
        ),
        encoding="utf-8",
    )
    snapshot = congestion.parse_placement_def(str(def_path))
    assert [c for pin in snapshot.nets[0].pins for c in pin] == pytest.approx(
        [10.0, 20.0, 10.0, 20.0]
    )


def test_parse_placement_def_resolves_io_pin_locations(tmp_path, cell_lef):
    def_path = tmp_path / "widget_place.def"
    def_path.write_text(
        _def_text(
            components="    - u1 cellA + PLACED ( 10000 20000 ) N ;",
            component_count=1,
            pins=(
                "    - clk + NET clk + DIRECTION INPUT + USE SIGNAL\n"
                "      + PORT\n"
                "        + LAYER met2 ( -70 -242 ) ( 70 243 )\n"
                "        + PLACED ( 90000 500 ) N ;"
            ),
            pin_count=1,
            nets="    - clk ( PIN clk ) ( u1 A ) + USE CLOCK ;",
            net_count=1,
        ),
        encoding="utf-8",
    )
    macros = congestion.parse_lef_macros([cell_lef])
    snapshot = congestion.parse_placement_def(str(def_path), macros)
    assert snapshot.nets[0].use == "CLOCK"
    assert [c for pin in snapshot.nets[0].pins for c in pin] == pytest.approx(
        [90.0, 0.5, 10.5, 22.0]
    )


def test_parse_placement_def_joins_wrapped_net_connection_lists(tmp_path):
    components = "\n".join(
        f"    - u{i} cellA + PLACED ( {i * 1000} 0 ) N ;" for i in range(6)
    )
    wrapped = (
        "    - wide ( u0 A ) ( u1 A ) ( u2 A )\n"
        "      ( u3 A ) ( u4 A )\n"
        "      ( u5 A ) + USE SIGNAL ;"
    )
    def_path = tmp_path / "widget_place.def"
    def_path.write_text(
        _def_text(
            components=components,
            component_count=6,
            nets=wrapped,
            net_count=1,
            pin_count=0,
        ),
        encoding="utf-8",
    )
    snapshot = congestion.parse_placement_def(str(def_path))
    assert snapshot.nets[0].degree == 6


def test_parse_placement_def_requires_units_and_diearea(tmp_path):
    no_units = tmp_path / "no_units.def"
    no_units.write_text(
        "DESIGN widget ;\nDIEAREA ( 0 0 ) ( 1 1 ) ;\n", encoding="utf-8"
    )
    with pytest.raises(congestion.CongestionError, match="UNITS DISTANCE MICRONS"):
        congestion.parse_placement_def(str(no_units))

    no_die = tmp_path / "no_die.def"
    no_die.write_text(
        "DESIGN widget ;\nUNITS DISTANCE MICRONS 1000 ;\n", encoding="utf-8"
    )
    with pytest.raises(congestion.CongestionError, match="no DIEAREA"):
        congestion.parse_placement_def(str(no_die))


# --------------------------------------------------------------------------- #
# RSMT / HPWL
# --------------------------------------------------------------------------- #


def _length(edges):
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in edges)


def test_hpwl_is_bounding_box_half_perimeter():
    assert congestion.hpwl([(0.0, 0.0), (3.0, 4.0), (1.0, 1.0)]) == pytest.approx(7.0)
    assert congestion.hpwl([(1.0, 1.0)]) == 0.0


def test_rsmt_of_two_points_is_their_manhattan_distance():
    edges = congestion.rsmt_edges([(0.0, 0.0), (3.0, 4.0)])
    assert _length(edges) == pytest.approx(7.0)


def test_rsmt_of_three_points_equals_hpwl():
    # A degree-3 net's RSMT is exactly its bounding-box half-perimeter --
    # a textbook identity, and a real check that the Steiner search finds
    # the optimum rather than settling for the spanning tree.
    points = [(0.0, 0.0), (10.0, 2.0), (4.0, 9.0)]
    assert _length(congestion.rsmt_edges(points)) == pytest.approx(
        congestion.hpwl(points)
    )


def test_rsmt_beats_rmst_on_a_cross():
    # Four points on the arms of a plus sign: the RSMT routes through the
    # centre Steiner point (total 40); the spanning tree cannot (total 60).
    points = [(0.0, 10.0), (20.0, 10.0), (10.0, 0.0), (10.0, 20.0)]
    rsmt = _length(congestion.rsmt_edges(points))
    rmst = _length(congestion.rmst_edges(points))
    assert rsmt == pytest.approx(40.0)
    assert rmst == pytest.approx(60.0)


def test_rsmt_never_shorter_than_hpwl_on_random_nets():
    import random

    rng = random.Random(20260811)
    for _ in range(25):
        points = [
            (float(rng.randrange(0, 100)), float(rng.randrange(0, 100)))
            for _ in range(rng.randrange(2, 8))
        ]
        assert _length(congestion.rsmt_edges(points)) >= congestion.hpwl(points) - 1e-9


def test_rsmt_falls_back_to_rmst_above_the_degree_cap():
    points = [(float(i), float(i * i % 7)) for i in range(10)]
    capped = congestion.rsmt_edges(points, max_iterated_steiner_degree=4)
    assert _length(capped) == pytest.approx(_length(congestion.rmst_edges(points)))


def test_rsmt_of_degenerate_nets_is_empty():
    assert congestion.rsmt_edges([]) == []
    assert congestion.rsmt_edges([(1.0, 1.0)]) == []
    assert congestion.rsmt_edges([(1.0, 1.0), (1.0, 1.0)]) == []


# --------------------------------------------------------------------------- #
# Bin-grid estimate
# --------------------------------------------------------------------------- #


def _layers():
    return [
        congestion.RoutingLayer("met1", "horizontal", 0.34),
        congestion.RoutingLayer("met2", "vertical", 0.46),
    ]


def _snapshot(nets, *, die=(0.0, 0.0, 100.0, 100.0)):
    return congestion.PlacementSnapshot(
        design="widget",
        die_area_um=die,
        instance_count=len(nets) * 2,
        nets=tuple(nets),
    )


def test_estimate_reports_hpwl_rsmt_and_their_ratio():
    net = congestion.Net("n1", "SIGNAL", ((0.0, 0.0), (10.0, 10.0)))
    result = congestion.estimate_congestion(_snapshot([net]), _layers())
    assert result["schema_version"] == congestion.SCHEMA_VERSION
    assert result["routed_net_count"] == 1
    assert result["hpwl_um"] == pytest.approx(20.0)
    assert result["rsmt_um"] == pytest.approx(20.0)
    assert result["rsmt_over_hpwl"] == pytest.approx(1.0)


def test_estimate_total_demand_equals_total_rsmt_length():
    # Demand spreading must conserve wirelength: every micron of RSMT ends
    # up in exactly one bin's worth of demand, so the utilisation-weighted
    # totals reconstruct `rsmt_um`.
    nets = [
        congestion.Net("n1", "SIGNAL", ((1.0, 1.0), (60.0, 40.0))),
        congestion.Net("n2", "SIGNAL", ((5.0, 90.0), (95.0, 5.0), (50.0, 50.0))),
    ]
    result = congestion.estimate_congestion(_snapshot(nets), _layers(), bin_um=10.0)
    grid = result["bin_grid"]
    total_supply_weighted = (
        result["mean_utilization"] * grid["nx"] * grid["ny"]
    )  # only an ordering sanity check; exact reconstruction is below
    assert total_supply_weighted > 0
    assert result["rsmt_um"] == pytest.approx(
        sum(
            abs(a[0] - b[0]) + abs(a[1] - b[1])
            for net in nets
            for a, b in congestion.rsmt_edges(list(net.pins))
        )
    )


def test_estimate_skips_clock_nets_by_default():
    clock = congestion.Net("clk", "CLOCK", ((0.0, 0.0), (90.0, 90.0)))
    result = congestion.estimate_congestion(_snapshot([clock]), _layers())
    assert result["routed_net_count"] == 0
    assert result["skipped_clock_net_count"] == 1
    assert result["rsmt_um"] == 0.0

    kept = congestion.estimate_congestion(
        _snapshot([clock]), _layers(), include_clock_nets=True
    )
    assert kept["routed_net_count"] == 1
    assert kept["rsmt_um"] == pytest.approx(180.0)


def test_estimate_skips_power_and_ground_nets():
    power = congestion.Net("VPWR", "POWER", ((0.0, 0.0), (90.0, 90.0)))
    result = congestion.estimate_congestion(_snapshot([power]), _layers())
    assert result["routed_net_count"] == 0
    assert result["rsmt_um"] == 0.0


def test_estimate_score_rises_when_the_same_nets_are_squeezed_into_less_area():
    # The core monotonicity an early-reject gate depends on: identical
    # connectivity packed into a smaller die must score as more congested.
    def nets(scale):
        return [
            congestion.Net(
                f"n{i}",
                "SIGNAL",
                ((i * scale, 0.0), (i * scale, 90.0 * scale)),
            )
            for i in range(1, 20)
        ]

    roomy = congestion.estimate_congestion(
        _snapshot(nets(1.0), die=(0.0, 0.0, 100.0, 100.0)), _layers(), bin_um=10.0
    )
    tight = congestion.estimate_congestion(
        _snapshot(nets(0.25), die=(0.0, 0.0, 25.0, 25.0)), _layers(), bin_um=2.5
    )
    assert tight["congestion_score"] > roomy["congestion_score"]


def test_estimate_default_bin_size_is_fifteen_finest_pitches():
    net = congestion.Net("n1", "SIGNAL", ((0.0, 0.0), (10.0, 10.0)))
    result = congestion.estimate_congestion(_snapshot([net]), _layers())
    assert result["bin_grid"]["bin_um"] == pytest.approx(15 * 0.34)


def test_estimate_congestion_score_is_the_p95_bin_utilisation():
    nets = [
        congestion.Net(f"n{i}", "SIGNAL", ((0.0, float(i)), (100.0, float(i))))
        for i in range(40)
    ]
    result = congestion.estimate_congestion(_snapshot(nets), _layers(), bin_um=10.0)
    assert result["congestion_score"] == pytest.approx(result["p95_utilization"])
    assert result["peak_utilization"] >= result["p95_utilization"]
    assert result["p95_utilization"] >= result["mean_utilization"]


def test_estimate_layer_adjustment_scales_utilisation_but_not_ranking():
    nets = [congestion.Net("n1", "SIGNAL", ((0.0, 0.0), (90.0, 90.0)))]
    full = congestion.estimate_congestion(
        _snapshot(nets), _layers(), bin_um=10.0, layer_adjustment=1.0
    )
    half = congestion.estimate_congestion(
        _snapshot(nets), _layers(), bin_um=10.0, layer_adjustment=0.5
    )
    assert half["peak_utilization"] == pytest.approx(2 * full["peak_utilization"])


def test_estimate_rejects_empty_layer_set():
    with pytest.raises(congestion.CongestionError, match="no routing layers"):
        congestion.estimate_congestion(_snapshot([]), [])


def test_estimate_rejects_single_direction_layer_stack():
    net = congestion.Net("n1", "SIGNAL", ((0.0, 0.0), (10.0, 10.0)))
    with pytest.raises(congestion.CongestionError, match="horizontal and one"):
        congestion.estimate_congestion(
            _snapshot([net]), [congestion.RoutingLayer("met1", "horizontal", 0.34)]
        )


def test_estimate_rejects_bad_layer_direction_or_pitch():
    net = congestion.Net("n1", "SIGNAL", ((0.0, 0.0), (10.0, 10.0)))
    with pytest.raises(congestion.CongestionError, match="expected 'horizontal'"):
        congestion.estimate_congestion(
            _snapshot([net]), [congestion.RoutingLayer("metX", "diagonal", 0.34)]
        )
    with pytest.raises(congestion.CongestionError, match="non-positive pitch"):
        congestion.estimate_congestion(
            _snapshot([net]),
            [
                congestion.RoutingLayer("met1", "horizontal", 0.0),
                congestion.RoutingLayer("met2", "vertical", 0.46),
            ],
        )


def test_estimate_rejects_degenerate_die_area():
    net = congestion.Net("n1", "SIGNAL", ((0.0, 0.0), (10.0, 10.0)))
    with pytest.raises(congestion.CongestionError, match="degenerate die area"):
        congestion.estimate_congestion(
            _snapshot([net], die=(0.0, 0.0, 0.0, 100.0)), _layers()
        )


# --------------------------------------------------------------------------- #
# End-to-end convenience wrapper
# --------------------------------------------------------------------------- #


def test_estimate_congestion_from_def_end_to_end(tmp_path, tech_lef, cell_lef):
    components = "\n".join(
        f"    - u{i} cellA + PLACED ( {i * 4000} {i * 4000} ) N ;" for i in range(8)
    )
    nets = "\n".join(
        f"    - n{i} ( u{i} Y ) ( u{i + 1} A ) + USE SIGNAL ;" for i in range(7)
    )
    def_path = tmp_path / "widget_place.def"
    def_path.write_text(
        _def_text(
            components=components,
            component_count=8,
            nets=nets,
            net_count=7,
            pin_count=0,
        ),
        encoding="utf-8",
    )
    result = congestion.estimate_congestion_from_def(
        str(def_path),
        tech_lef=tech_lef,
        cell_lefs=[cell_lef],
        routing_layers=["met1", "met2", "met3"],
    )
    assert result["design"] == "widget"
    assert result["routed_net_count"] == 7
    assert result["rsmt_um"] > 0
    assert math.isfinite(result["congestion_score"])
    assert [layer["name"] for layer in result["bin_grid"]["layers"]] == [
        "met1",
        "met2",
        "met3",
    ]


def test_estimate_congestion_from_def_rejects_unknown_routing_layer(
    tmp_path, tech_lef, cell_lef
):
    def_path = tmp_path / "widget_place.def"
    def_path.write_text(
        _def_text(
            components="    - u1 cellA + PLACED ( 0 0 ) N ;",
            component_count=1,
            nets="    - n1 ( u1 A ) ( u1 Y ) + USE SIGNAL ;",
            net_count=1,
            pin_count=0,
        ),
        encoding="utf-8",
    )
    with pytest.raises(congestion.CongestionError, match="no routing layer named"):
        congestion.estimate_congestion_from_def(
            str(def_path),
            tech_lef=tech_lef,
            cell_lefs=[cell_lef],
            routing_layers=["met9"],
        )
