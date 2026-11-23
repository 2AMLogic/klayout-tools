"""Powered ASAP7 RVT routes use the same inputs as the captured explicit request."""

import json
from pathlib import Path

import pytest

from klayout_tools import place_and_route as pnr
from klayout_tools.place_and_route_request import _validate_library
from klayout_tools.place_and_route_sta import _corner_sweep_script_lines
from test_place_and_route_library import _stage_lines

_FIXTURE = Path(__file__).parent / "fixtures/place_and_route_drcfill/request.json"
_NAMES = ("asap7sc7p5t_rvt", "asap7sc7p5t_28_R")


@pytest.mark.parametrize("name", _NAMES)
def test_powered_rvt_defaults_match_captured_tcl_and_audit(name):
    request = json.loads(_FIXTURE.read_text())
    expected = _validate_library(request["library"])
    effective = _validate_library(None, name)
    assert effective == expected
    power = pnr._validate_power(request["power"], name, effective)
    for stage in pnr.STAGE_ORDER:
        assert _stage_lines(
            name, stage, library=effective, power=power
        ) == _stage_lines(name, stage, library=expected, power=power)
    sta = _corner_sweep_script_lines(
        checkpoint_in="route.odb",
        corners=[],
        io_spec=request["io"],
        clock_port="clk",
        clock_period_ns=1,
        library=effective,
    )
    for kind in ("clock", "signal"):
        assert (
            f"set_wire_rc -{kind} -resistance 4.32e-05 -capacitance 0.00016579" in sta
        )
    assert pnr._audit_tapcell_masters(name, effective) == ("TAPCELL_ASAP7_75t_R",) * 2
    assert pnr._audit_filler_masters(name, effective) == tuple(
        request["library"]["filler"]
    )
    route = "\n".join(_stage_lines(name, "route", library=effective, power=power))
    assert "check_antennas" in route and "repair_antennas" not in route
    assert pnr._signal_routing_layers(name, effective) == tuple(
        f"M{i}" for i in range(2, 8)
    )
    with pytest.raises(pnr.PlaceAndRouteError, match="layer_v 'M8'.*M2-M7"):
        pnr._validate_io({"layer_h": "M4", "layer_v": "M8"}, name, effective)


@pytest.mark.parametrize("name", _NAMES)
def test_rvt_overrides_retain_other_defaults(name):
    base = _validate_library(None, name)
    override = _validate_library({"cts_buffer": "OTHER_BUF"}, name)
    assert override == {**base, "cts_buffer": "OTHER_BUF"}
    assert "OTHER_BUF" in "\n".join(_stage_lines(name, "cts", library=override))
    tapless = _validate_library({"tapcell": None}, name)
    assert tapless == {**base, "tapcell": (None, None, None)}
    assert pnr._tapcell_lines(name, None, tapless) == []
    assert pnr._audit_tapcell_masters(name, tapless) == (None, None)


def test_rvt_defaults_are_limited_to_verified_names():
    for name in ("asap7sc7p5t_28", "asap7sc7p5t_lvt", "custom_library"):
        assert _validate_library(None, name) is None
