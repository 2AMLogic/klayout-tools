"""Independent root clocks in implementation and post-route timing."""

from pathlib import Path

import pytest

from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from klayout_tools.place_and_route_request import _validate_constraints
from test_place_and_route import (
    _REAL_GF180MCU_7T_PNR_VARIANT,
    _STAGE_METRICS,
    HAVE_OPENROAD,
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success_with_post_route_spef,
    _stub_run_extract_for_post_route_spef,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

CLOCKS = [{"port": "clk_a", "period_ns": 40}, {"port": "clk_b", "period_ns": 80}]
GROUPS = [["clk_a"], ["clk_b"]]
NETLIST = """module pair(input clk_a, clk_b, da, db, output qa, qb);
  gf180mcu_fd_sc_mcu7t5v0__dffq_1 a(.CLK(clk_a), .D(da), .Q(qa));
  gf180mcu_fd_sc_mcu7t5v0__dffq_1 b(.CLK(clk_b), .D(db), .Q(qb));
endmodule
"""


@pytest.mark.parametrize(
    ("constraints", "field"),
    [
        (
            {"clocks": CLOCKS, "clock_port": "clk_a", "clock_period_ns": 40},
            "clock_port",
        ),
        ({"clocks": CLOCKS, "clock_period_ns": 40}, "clock_period_ns"),
        ({"clocks": CLOCKS, "clock_port": None}, "clock_port"),
        ({"clocks": []}, "clocks"),
        ({"clocks": None}, "clocks"),
        ({"clocks": [CLOCKS[0], CLOCKS[0]]}, "clk_a"),
        ({"clocks": ["clk_a"]}, "clocks"),
        ({"clocks": [{"period_ns": 40}]}, "port"),
        ({"clocks": [{"port": "clk_b"}]}, "period_ns"),
        ({"clocks": [{**CLOCKS[0], "perod": 40}]}, "perod"),
        ({"clocks": CLOCKS, "clock_groups": [["clk_a"], ["typo"]]}, "typo"),
        ({"clocks": CLOCKS, "clock_groups": [["clk_a"]]}, "clock_groups"),
        ({"cloks": CLOCKS}, "cloks"),
        ({"clock_groups": GROUPS}, "clock_groups"),
        ({"clocks": CLOCKS, "clock_groups": [[], ["clk_b"]]}, "clock_groups"),
    ],
)
def test_invalid_clocks(constraints, field):
    with pytest.raises(PlaceAndRouteError, match=field):
        _validate_constraints(constraints)


@pytest.mark.parametrize(
    "period", [0, -1, False, float("inf"), float("nan"), 10**400, "80", {"tt": 80}]
)
def test_invalid_period(period):
    with pytest.raises(PlaceAndRouteError, match="period_ns"):
        _validate_constraints({"clocks": [{"port": "clk_b", "period_ns": period}]})


@pytest.mark.parametrize("groups", [None, GROUPS])
def test_every_session_declares_both_clocks(tmp_path, monkeypatch, groups):
    constraints = {"clocks": CLOCKS, "input_delay_ns": 1, "output_delay_ns": 2}
    if groups:
        constraints["clock_groups"] = groups
    request = _setup_success_env(
        tmp_path, monkeypatch, constraints=constraints, post_route_spef=True
    )
    # Inventory keys captured from the two-flop GF180D OpenROAD run.
    measured = {
        "clock__period_ns__636c6b5f61": 40,
        "clock__registers__636c6b5f61": 1,
        "clock__period_ns__636c6b5f62": 80,
        "clock__registers__636c6b5f62": 1,
    }
    _stub_openroad_success_with_post_route_spef(
        monkeypatch,
        stage_metrics={s: {**m, **measured} for s, m in _STAGE_METRICS.items()},
    )
    _stub_run_extract_for_post_route_spef(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    run_place_and_route(request)
    scripts = list((tmp_path / ".klt/place-and-route").glob("pnr_*.tcl"))
    assert {p.stem for p in scripts} >= {
        f"pnr_gcd_{stage}"
        for stage in (
            "floorplan",
            "place",
            "cts",
            "route",
            "route_corners",
            "route_spef",
        )
    }
    for script in scripts:
        tcl = script.read_text()
        for clock in CLOCKS:
            period = float(clock["period_ns"])
            assert f"create_clock -name {clock['port']} -period {period} " in tcl
        if groups:
            assert (
                "set_clock_groups -asynchronous -group [get_clocks [list clk_a]] "
                "-group [get_clocks [list clk_b]]" in tcl
            )
        else:
            assert "set_clock_groups" not in tcl
        assert "$klt_non_clock_inputs [get_ports clk_b]" in tcl
        assert "set_input_delay 1.0 -clock clk_a" in tcl
        assert "set_output_delay 2.0 -clock clk_a" in tcl


def test_missing_engine_inventory_names_the_dropped_clock():
    clocks, *_ = _validate_constraints({"clocks": CLOCKS})
    with pytest.raises(PlaceAndRouteError, match="clk_b"):
        clocks.inventory(
            {"clock__period_ns__636c6b5f61": 40, "clock__registers__636c6b5f61": 1}
        )


def _pair_request(tmp_path, constraints, stage="route"):
    return _write_request(
        tmp_path / "pair.json",
        {
            "netlist": "pair.v",
            "hdl_toplevel": "pair",
            "pdk": {
                "cell_library": "gf180mcu_fd_sc_mcu7t5v0",
                "corner": "tt_025C_3v30",
                "sweep_corners": ["tt_025C_3v30"],
            },
            "floorplan": {
                "method": "explicit",
                "die_area_um": [0, 0, 100, 100],
                "core_area_um": [8, 8, 92, 92],
                "site": "GF018hv5v_mcu_sc7",
            },
            "io": {"layer_h": "Metal3", "layer_v": "Metal2"},
            "constraints": constraints,
            "seed": 1,
            "target_stage": stage,
            "outputs": {"dir": "out"},
        },
    )


@pytest.fixture
def pair(tmp_path, monkeypatch):
    if not HAVE_OPENROAD or _REAL_GF180MCU_7T_PNR_VARIANT is None:
        pytest.skip("requires OpenROAD and the GF180 7-track PDK")
    root, variant = _REAL_GF180MCU_7T_PNR_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv(
        "PDK", "gf180mcuD" if (Path(root) / "gf180mcuD").exists() else variant
    )
    (tmp_path / "pair.v").write_text(NETLIST)
    return tmp_path


@pytest.mark.parametrize("groups", [None, GROUPS])
def test_live_pair_routes_both_domains(pair, groups):
    constraints = {"clocks": CLOCKS}
    if groups:
        constraints["clock_groups"] = groups
    report = run_place_and_route(_pair_request(pair, constraints))
    assert report["stage_reached"] == "route"
    expected = [{**clock, "clocked_registers": 1} for clock in CLOCKS]
    assert report["clock_roots"] == expected
    assert report["stages"][2]["clock_roots"] == expected


def test_live_pair_missing_port(pair):
    clocks = [CLOCKS[0], {**CLOCKS[1], "port": "missing_clk"}]
    with pytest.raises(PlaceAndRouteError, match="missing_clk"):
        run_place_and_route(_pair_request(pair, {"clocks": clocks}, "floorplan"))


def test_live_first_clock_without_sinks(pair):
    (pair / "pair.v").write_text(NETLIST.replace(".CLK(clk_a)", ".CLK(clk_b)"))
    report = run_place_and_route(_pair_request(pair, {"clocks": CLOCKS}, "cts"))
    assert [row["clocked_registers"] for row in report["clock_roots"]] == [0, 2]
    cts_def = (pair / "out/pair.cts.def").read_text()
    assert "clkbuf_0_clk_b" in cts_def
    assert "clkbuf_0_clk_a" not in cts_def
