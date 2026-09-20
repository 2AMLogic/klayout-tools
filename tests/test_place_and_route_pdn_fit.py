"""Tests for `klayout_tools.place_and_route_pdn_fit` -- the request-level
cross-check between `request.power`'s PDN strap geometry and
`request.floorplan`'s core size (issue #2170).

The arithmetic tests below are pinned to the **reported** failure's own
numbers rather than to values this module produces: issue #2170 recorded a
real `pdngen` run rejecting gf180's own `Metal5` stripe with

    [ERROR PDN-0185] Insufficient width (55.44 um) to add straps on layer
    Metal5 in grid "grid" with total strap width 49.3 um and offset 44.8 um

so `strap_group_width_um` must produce 49.28 and `strap_min_core_um`
44.8 + 49.28 = 94.08 for that strap, or this module is not modelling
`pdngen`'s check.
"""

from __future__ import annotations

import math

import pytest

from klayout_tools import place_and_route_pdn_fit as pdn_fit
from klayout_tools.place_and_route import _validate_power

# gf180's own `pdn_grid_strategy_9t_6M.cfg` standard-cell grid, normalized
# through `_validate_power` exactly as a real request is -- the geometry
# this issue's reproduction actually ran.
_GF180_STRAPS = _validate_power({"preset": "gf180mcu_9t_6M"}, "gf180mcu_fd_sc_mcu9t5v0")

#: The core `initialize_floorplan -utilization 40` derived in the reported
#: run, and the standard-cell area that produces it at 40% / aspect 1.0.
_REPORTED_CORE_UM = 55.44
_REPORTED_CELL_AREA_UM2 = _REPORTED_CORE_UM * _REPORTED_CORE_UM * 0.40


def _strap(layer: str) -> dict:
    return next(s for s in _GF180_STRAPS["straps"] if s["layer"] == layer)


# --------------------------------------------------------------------------- #
# Strap geometry: `pdngen`'s own `getStrapGroupWidth()` / offset check
# --------------------------------------------------------------------------- #


def test_group_width_uses_pdngen_default_spacing_when_none_given():
    """`Metal5` states no `-spacing`, so `pdngen` defaults it to
    `pitch / net_count - width` -- 89.6/2 - 4.48 = 40.32, giving the
    reported "total strap width 49.3 um"."""
    assert pdn_fit.resolved_spacing_um(_strap("Metal5")) == pytest.approx(40.32)
    assert pdn_fit.strap_group_width_um(_strap("Metal5")) == pytest.approx(49.28)


def test_min_core_matches_the_reported_pdn_0185_numbers():
    """offset (44.8) + total strap width (49.28) = the 94.1 um this issue's
    own report computed by hand."""
    assert pdn_fit.strap_min_core_um(_strap("Metal5")) == pytest.approx(94.08)


def test_group_width_uses_explicit_spacing_when_given():
    """`Metal4` states `-spacing {0.56}`, which is used verbatim rather than
    defaulted: 2 x 4.48 + 0.56 = 9.52, and 22.4 + 9.52 = 31.92 um of core
    -- comfortably inside the reported 55.44 um core, which is why the
    reported run failed on `Metal5` and not here."""
    assert pdn_fit.resolved_spacing_um(_strap("Metal4")) == pytest.approx(0.56)
    assert pdn_fit.strap_group_width_um(_strap("Metal4")) == pytest.approx(9.52)
    assert pdn_fit.strap_min_core_um(_strap("Metal4")) == pytest.approx(31.92)


def test_default_spacing_snaps_down_to_the_manufacturing_grid():
    """`Straps::Straps` snaps a defaulted spacing **down** to the layer's
    manufacturing grid ("rounding up will cause pitch check to fail") --
    verified against ``TechLayer::snapToManufacturingGrid`` (``The-OpenROAD-
    Project/OpenROAD`` ``src/pdn/src/techlayer.cpp``, ``master``): it only
    changes a value that is not already an exact multiple of the grid, so
    ``pitch_um``/``width_um`` are chosen here to give an unsnapped spacing
    (12.05 um) that is deliberately *not* a multiple of the 0.25 um grid,
    unlike a plain round-number fixture where snapping would be a no-op."""
    strap = {"layer": "met5", "width_um": 1.6, "pitch_um": 27.3, "offset_um": 13.65}
    assert pdn_fit.resolved_spacing_um(strap) == pytest.approx(12.05)
    assert pdn_fit.resolved_spacing_um(
        strap, manufacturing_grid_um=0.25
    ) == pytest.approx(12.0)
    assert pdn_fit.strap_group_width_um(
        strap, manufacturing_grid_um=0.25
    ) == pytest.approx(15.2)


# --------------------------------------------------------------------------- #
# Core extent per floorplan method
# --------------------------------------------------------------------------- #


def test_core_extent_explicit_is_exact():
    core = pdn_fit.core_extent_um(
        {"method": "explicit", "core_area_um": [2.0, 3.0, 102.0, 53.0]}
    )
    assert (core.width_um, core.height_um, core.exact) == (100.0, 50.0, True)
    assert "explicit" in core.derivation


def test_core_extent_utilization_reproduces_initialize_floorplan():
    """`InitFloorplan::makeDieUtilization`: core_area = design_area /
    utilization, core_width = sqrt(core_area / aspect_ratio). At 40% over
    the reported design's cell area that is the reported 55.44 um core."""
    core = pdn_fit.core_extent_um(
        {"method": "utilization", "utilization_pct": 40, "aspect_ratio": 1.0},
        cell_area_um2=_REPORTED_CELL_AREA_UM2,
    )
    assert core.width_um == pytest.approx(_REPORTED_CORE_UM)
    assert core.height_um == pytest.approx(_REPORTED_CORE_UM)
    assert core.exact is False


def test_core_extent_utilization_honors_aspect_ratio():
    core = pdn_fit.core_extent_um(
        {"method": "utilization", "utilization_pct": 50, "aspect_ratio": 4.0},
        cell_area_um2=800.0,
    )
    assert core.width_um == pytest.approx(math.sqrt(1600.0 / 4.0))
    assert core.height_um == pytest.approx(4.0 * core.width_um)


def test_core_extent_utilization_defaults_aspect_ratio_to_one():
    core = pdn_fit.core_extent_um(
        {"method": "utilization", "utilization_pct": 50}, cell_area_um2=800.0
    )
    assert core.width_um == pytest.approx(core.height_um)


def test_core_extent_utilization_without_cell_area_is_unknown():
    assert (
        pdn_fit.core_extent_um({"method": "utilization", "utilization_pct": 40}) is None
    )


def test_core_extent_def_method_is_never_derived():
    """A `"def"` floorplan's core comes from an existing DEF's own rows,
    which this module deliberately does not read -- the check is skipped
    rather than guessed at."""
    assert pdn_fit.core_extent_um({"method": "def", "def_path": "x.def"}) is None


# --------------------------------------------------------------------------- #
# Standard-cell area from the netlist + cell LEF
# --------------------------------------------------------------------------- #

_CELL_LEF = """\
VERSION 5.7 ;
MACRO cell_and2
  CLASS CORE ;
  SIZE 5.6 BY 5.04 ;
  PIN A
    DIRECTION INPUT ;
  END A
END cell_and2
MACRO cell_inv
  CLASS CORE ;
  SIZE 2.8 BY 5.04 ;
END cell_inv
END LIBRARY
"""

_NETLIST = """\
module counter (clk, q);
  input clk;
  output q;
  wire n1;
  cell_and2 u0 ( .A(clk), .B(clk), .Z(n1) );
  cell_inv  u1 ( .A(n1), .Z(q) );
endmodule
"""


def test_parse_macro_sizes_reads_every_macro_size():
    assert pdn_fit.parse_macro_sizes(_CELL_LEF) == {
        "cell_and2": (5.6, 5.04),
        "cell_inv": (2.8, 5.04),
    }


def test_standard_cell_area_sums_every_instantiated_master(tmp_path):
    netlist = tmp_path / "counter.v"
    netlist.write_text(_NETLIST, encoding="utf-8")
    sizes = pdn_fit.parse_macro_sizes(_CELL_LEF)
    area = pdn_fit.standard_cell_area_um2(str(netlist), "counter", sizes)
    assert area == pytest.approx(5.6 * 5.04 + 2.8 * 5.04)


def test_standard_cell_area_is_unknown_when_a_master_has_no_size(tmp_path):
    """A partial sum would understate the design area, which would
    understate the derived core, which could reject a request OpenROAD
    would have accepted -- so an unsized master disables the check."""
    netlist = tmp_path / "counter.v"
    netlist.write_text(_NETLIST, encoding="utf-8")
    sizes = {"cell_and2": (5.6, 5.04)}
    assert pdn_fit.standard_cell_area_um2(str(netlist), "counter", sizes) is None


def test_standard_cell_area_is_unknown_for_an_unparseable_netlist(tmp_path):
    netlist = tmp_path / "counter.v"
    netlist.write_text("// fake mapped netlist\n", encoding="utf-8")
    assert pdn_fit.standard_cell_area_um2(str(netlist), "counter", {}) is None


def test_standard_cell_area_is_unknown_when_the_toplevel_is_absent(tmp_path):
    netlist = tmp_path / "counter.v"
    netlist.write_text(_NETLIST, encoding="utf-8")
    sizes = pdn_fit.parse_macro_sizes(_CELL_LEF)
    assert pdn_fit.standard_cell_area_um2(str(netlist), "other_top", sizes) is None


# --------------------------------------------------------------------------- #
# Tech-LEF context: routing direction + manufacturing grid
# --------------------------------------------------------------------------- #

_TECH_LEF = """\
VERSION 5.7 ;
MANUFACTURINGGRID 0.005 ;
LAYER Metal4
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
END Metal4
LAYER Metal5
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
END Metal5
END LIBRARY
"""


def test_read_tech_context_reads_directions_and_grid(tmp_path):
    tech_lef = tmp_path / "tech.lef"
    tech_lef.write_text(_TECH_LEF, encoding="utf-8")
    tech = pdn_fit.read_tech_context(str(tech_lef))
    assert tech.layer_directions["Metal5"] == "HORIZONTAL"
    assert tech.layer_directions["Metal4"] == "VERTICAL"
    assert tech.manufacturing_grid_um == pytest.approx(0.005)


def test_read_tech_context_degrades_to_empty_without_a_tech_lef(tmp_path):
    assert pdn_fit.read_tech_context(None) == pdn_fit.TechContext({}, None)
    missing = pdn_fit.read_tech_context(str(tmp_path / "nope.lef"))
    assert missing == pdn_fit.TechContext({}, None)


# --------------------------------------------------------------------------- #
# The joint check itself
# --------------------------------------------------------------------------- #


def _square_core(extent_um: float) -> pdn_fit.CoreExtent:
    return pdn_fit.CoreExtent(extent_um, extent_um, "a test core", True)


def test_fit_error_names_both_request_fields():
    """The whole point of this check: a message a caller can act on, naming
    `request.power` and `request.floorplan` -- neither of which appears in
    `pdngen`'s own PDN-0185 text."""
    message = pdn_fit.pdn_fit_error(
        _GF180_STRAPS,
        {},
        core=_square_core(_REPORTED_CORE_UM),
        tech=pdn_fit.TechContext({"Metal5": "HORIZONTAL"}, 0.005),
    )
    assert message is not None
    assert "request.power.straps[2]" in message
    assert "request.floorplan" in message
    assert "Metal5" in message
    assert "94.08" in message
    assert "55.44" in message
    assert "PDN-0185" in message


def test_no_error_when_every_strap_fits():
    assert (
        pdn_fit.pdn_fit_error(
            _GF180_STRAPS,
            {},
            core=_square_core(200.0),
            tech=pdn_fit.TechContext({"Metal5": "HORIZONTAL"}, 0.005),
        )
        is None
    )


def test_followpins_row_rail_is_exempt():
    """`FollowPins::checkLayerSpecifications` overrides the base-class
    offset/width check, so a row rail is never measured against the core
    edge however large its declared offset is."""
    power = {
        "straps": [
            {
                "layer": "Metal1",
                "width_um": 0.9,
                "pitch_um": 5.04,
                "offset_um": 5000.0,
                "spacing_um": None,
                "followpins": True,
            }
        ]
    }
    assert (
        pdn_fit.pdn_fit_error(
            power, {}, core=_square_core(20.0), tech=pdn_fit.TechContext({}, None)
        )
        is None
    )


def test_layer_direction_selects_which_core_dimension_is_measured():
    """A horizontal strap steps along y (`pdngen` compares it against the
    core's `dy()`); a vertical one along x. A tall, narrow core therefore
    fits one and not the other."""
    core = pdn_fit.CoreExtent(40.0, 200.0, "a test core", True)
    strap = {
        "layer": "metX",
        "width_um": 1.0,
        "pitch_um": 100.0,
        "offset_um": 0.0,
        "spacing_um": None,
        "followpins": False,
    }
    power = {"straps": [strap]}
    horizontal = pdn_fit.TechContext({"metX": "HORIZONTAL"}, None)
    vertical = pdn_fit.TechContext({"metX": "VERTICAL"}, None)
    assert pdn_fit.pdn_fit_error(power, {}, core=core, tech=horizontal) is None
    message = pdn_fit.pdn_fit_error(power, {}, core=core, tech=vertical)
    assert message is not None and "core width" in message


def test_unknown_layer_direction_uses_the_larger_core_dimension():
    """With no `DIRECTION` in the tech LEF the check cannot tell which
    dimension `pdngen` will measure, so it uses the larger one -- it can
    then only under-report a problem, never invent one."""
    core = pdn_fit.CoreExtent(40.0, 200.0, "a test core", True)
    power = {
        "straps": [
            {
                "layer": "metX",
                "width_um": 1.0,
                "pitch_um": 100.0,
                "offset_um": 0.0,
                "spacing_um": None,
                "followpins": False,
            }
        ]
    }
    assert (
        pdn_fit.pdn_fit_error(power, {}, core=core, tech=pdn_fit.TechContext({}, None))
        is None
    )


# --------------------------------------------------------------------------- #
# `check_request`: the entry point `run_place_and_route` calls
# --------------------------------------------------------------------------- #


def test_check_request_is_a_no_op_without_request_power():
    assert (
        pdn_fit.check_request(
            floorplan={"method": "explicit", "core_area_um": [0, 0, 10, 10]},
            power=None,
        )
        is None
    )


# A single large master (SIZE 40 BY 36, area 1440 um^2) so a single
# instance's `utilization_pct: 40`/`aspect_ratio: 1.0` core lands at
# sqrt(1440 / 0.4) = 60 um -- deliberately between Metal4's 31.92 um
# minimum (fits) and Metal5's 94.08 um minimum (does not), the same shape
# as this issue's own reported repro: nothing individually wrong, Metal4
# passes, Metal5 is the strap that actually fails.
_BIG_CELL_LEF = """\
VERSION 5.7 ;
MACRO cell_big
  CLASS CORE ;
  SIZE 40.0 BY 36.0 ;
END cell_big
END LIBRARY
"""

_BIG_CELL_NETLIST = """\
module counter (clk, q);
  input clk;
  output q;
  cell_big u0 ( .A(clk), .Z(q) );
endmodule
"""


def test_check_request_rejects_the_reported_utilization_reproduction(tmp_path):
    """Issue #2170's own reproduction end to end: gf180's verbatim strap
    geometry against a `method: "utilization"` floorplan whose derived
    60 um core fits Metal4's 31.92 um minimum but not Metal5's 94.08 um
    one -- the exact layer the real report named."""
    netlist = tmp_path / "counter.v"
    netlist.write_text(_BIG_CELL_NETLIST, encoding="utf-8")
    cell_lef = tmp_path / "cells.lef"
    cell_lef.write_text(_BIG_CELL_LEF, encoding="utf-8")
    tech_lef = tmp_path / "tech.lef"
    tech_lef.write_text(_TECH_LEF, encoding="utf-8")

    message = pdn_fit.check_request(
        floorplan={
            "method": "utilization",
            "utilization_pct": 40,
            "aspect_ratio": 1.0,
            "core_margin_um": 4.0,
            "site": "GF018hv5v_mcu_sc9",
        },
        power=_GF180_STRAPS,
        tech_lef=str(tech_lef),
        cell_lefs=[str(cell_lef)],
        netlist_path=str(netlist),
        hdl_toplevel="counter",
    )
    assert message is not None
    assert "request.power.straps[2]" in message
    assert "Metal5" in message
    assert "request.floorplan" in message
    assert 'floorplan.method "utilization"' in message


def test_check_request_accepts_a_floorplan_the_straps_fit(tmp_path):
    """The no-false-positive half: the same straps against a core that is
    genuinely large enough must produce no message at all."""
    netlist = tmp_path / "counter.v"
    netlist.write_text(_NETLIST, encoding="utf-8")
    cell_lef = tmp_path / "cells.lef"
    cell_lef.write_text(_CELL_LEF, encoding="utf-8")
    tech_lef = tmp_path / "tech.lef"
    tech_lef.write_text(_TECH_LEF, encoding="utf-8")

    assert (
        pdn_fit.check_request(
            floorplan={
                "method": "explicit",
                "die_area_um": [0, 0, 300, 300],
                "core_area_um": [10, 10, 290, 290],
                "site": "GF018hv5v_mcu_sc9",
            },
            power=_GF180_STRAPS,
            tech_lef=str(tech_lef),
            cell_lefs=[str(cell_lef)],
            netlist_path=str(netlist),
            hdl_toplevel="counter",
        )
        is None
    )


def test_check_request_skips_when_the_netlist_cannot_be_sized(tmp_path):
    """An unparseable netlist (or an unsized master) leaves a
    utilization-method run exactly as it was before this check existed --
    `pdngen`'s own PDN-0185 -- rather than being rejected on a guess."""
    netlist = tmp_path / "counter.v"
    netlist.write_text("// fake mapped netlist\n", encoding="utf-8")
    assert (
        pdn_fit.check_request(
            floorplan={"method": "utilization", "utilization_pct": 40, "site": "s"},
            power=_GF180_STRAPS,
            netlist_path=str(netlist),
            hdl_toplevel="counter",
        )
        is None
    )
