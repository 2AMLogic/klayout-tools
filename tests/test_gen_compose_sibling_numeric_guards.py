"""Report pad widths must not escape the optional stub-widening contract."""

import json
import math

import pytest

from klayout_tools import gen_compose
from klayout_tools.cli import main
from klayout_tools.gen_compose_routing import _endpoint_stub_widen_um
from test_gen_compose import _gen_block, _mos_gate_bus_request
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

INVALID_WIDTHS = [
    pytest.param(10**400, id="huge"),
    pytest.param(-(10**400), id="negative-huge"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="negative-inf"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param("1", id="string"),
    pytest.param(None, id="null"),
]


@pytest.mark.parametrize("width", INVALID_WIDTHS)
def test_endpoint_stub_ignores_invalid_width(width):
    port = {"width_um": width, "layer": {"layer": 66, "datatype": 20}}
    assert _endpoint_stub_widen_um(port, (1.0, 2.0), 90, 0.5, 0.17, (66, 20)) is None


@pytest.mark.parametrize("direction", [90, 270])
@pytest.mark.parametrize("width", [1, 0.42, 0.17, 0.1, 0, -1, None])
def test_endpoint_stub_finite_and_missing_width_controls(direction, width):
    port = {"layer": {"layer": 66, "datatype": 20}}
    if width is not None:
        port["width_um"] = width
    result = _endpoint_stub_widen_um(port, (1.0, 2.0), direction, 0.5, 0.17, (66, 20))
    if width is not None and width > 0.17:
        assert result == {
            "x_um": 1.0,
            "y_um": 2.0,
            "direction_deg": direction,
            "length_um": 0.5,
            "width_um": float(width),
        }
    else:
        assert result is None


@pytest.mark.parametrize("width", INVALID_WIDTHS)
def test_cli_invalid_endpoint_width_skips_widening(
    tmp_path, pdk_root, capsys, monkeypatch, width
):
    # Generate real layouts/reports, then poison only one endpoint width.
    a, b = [
        _gen_block(
            tmp_path, pdk_root, "mos_array", name, rows=1, cols=1, fingers=1, dummy=0
        )
        for name in ("a", "b")
    ]
    port = next(p for p in a["ports"] if p["name"] == "U0_G")
    port["width_um"] = width
    output = tmp_path / "gate_bus.gds"
    request = _mos_gate_bus_request(pdk_root, "sky130A", a, b, 0.17, output)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    observed = []

    def observe(port, *args):
        result = _endpoint_stub_widen_um(port, *args)
        # Call the real helper and reject invalid geometry before native GDS
        # code sees it. The other, unmodified endpoint must still widen.
        if result is not None:
            assert all(math.isfinite(value) for value in result.values())
        observed.append(result)
        return result

    monkeypatch.setattr(gen_compose, "_endpoint_stub_widen_um", observe)
    assert main(["gen-compose", str(request_path), "--format", "json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["unrouted_nets"] == []
    assert response["nets"][0]["status"] == "routed"
    assert response["nets"][0]["routed"] is True
    assert response["nets"][0]["landed_on_block"] is True
    assert len(observed) == 2
    assert observed[0] is None
    assert observed[1]["width_um"] == pytest.approx(0.42)
    assert output.is_file()
