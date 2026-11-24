"""Leaf consumers must stay usable without loading the placement orchestrator."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("base", ""),
        ("cell_libraries", ""),
        ("layers", "m._signal_layer_range('sky130_fd_sc_hd', None)"),
        ("unconstrained", "m.unconstrained_warnings('place', None, None)"),
        ("pins", "m.check_pin_constraints([{'pins': ['x']}], ['a'])"),
        ("request", "m._validate_target_stage('route')"),
        ("script", "m._antenna_check_lines()"),
        ("gds_merge", "m._output_dbu_factor(0.001, 0.002)"),
        ("route_overflow", ""),
        (
            "sta",
            "m._spef_sta_script_lines(checkpoint_in='c.odb', liberty_path='c.lib', "
            "clock_port=None, clock_period_ns=None, spef_path='c.spef', net_names=[])",
        ),
    ],
)
def test_consumer_keeps_orchestrator_unloaded(name, call):
    action = call or "pass"
    if name in {"pins", "gds_merge"}:
        action = (
            f"try:\n    {call}\n"
            "except m.PlaceAndRouteError:\n    pass\n"
            "else:\n    raise AssertionError('expected invalid input to be refused')"
        )
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        f"import klayout_tools.place_and_route_{name} as m\n"
        f"{action}\n"
        "assert 'klayout_tools.place_and_route' not in sys.modules\n"
    )
    if name in {"script", "pins", "route_overflow"}:
        script += "assert 'klayout_tools.place_and_route_request' not in sys.modules"
    if name in {"base", "cell_libraries"}:
        script += (
            "loaded = {k for k in sys.modules if k.startswith('klayout_tools.')}\n"
            "assert loaded == {m.__name__}, loaded"
        )
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_reexports_share_identity():
    from klayout_tools import place_and_route as flow
    from klayout_tools import place_and_route_base as base
    from klayout_tools import place_and_route_cell_libraries as libraries
    from klayout_tools import place_and_route_layers as layers
    from klayout_tools import place_and_route_request as request

    assert (
        flow.PlaceAndRouteError is request.PlaceAndRouteError is base.PlaceAndRouteError
    )
    for name in ("STAGE_ORDER", "_SETUP_VIOLATIONS_BEGIN", "_SPEF_NET_CHECK_RE"):
        assert getattr(flow, name) is getattr(base, name)
    assert flow._ROUTING_LAYER_RANGE is layers._ROUTING_LAYER_RANGE
    assert flow._CTS_BUFFER_CELLS is libraries._CTS_BUFFER_CELLS
    assert flow._NO_TAPCELL_LIBRARIES is libraries._NO_TAPCELL_LIBRARIES
