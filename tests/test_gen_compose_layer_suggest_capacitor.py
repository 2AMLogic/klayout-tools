"""Check capacitor terminal connectivity in the GDS produced by layer search."""

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_layer_suggest_baseline import _run_cli


def _plate_request(tmp_path, pdk_root, top, bottom, port_layer, primary):
    layout = kdb.Layout()
    west = layout.create_cell("WEST")
    west.shapes(layout.layer(*bottom)).insert(kdb.DBox(0, 0, 2, 2))
    west.shapes(layout.layer(*top)).insert(kdb.DBox(0.2, 0.2, 1.8, 1.8))
    east = layout.create_cell("EAST")
    east.shapes(layout.layer(67, 20)).insert(kdb.DBox(0, 0, 2, 2))
    east.shapes(layout.layer(67, 5)).insert(kdb.DText("N", 1, 1))
    source = tmp_path / "pads.gds"
    layout.write(str(source))

    def block(name, layer, x, direction):
        port = {
            "name": "P",
            "x_um": x,
            "y_um": 1,
            "width_um": 1.2,
            "direction_deg": direction,
            "layer": {"layer": layer[0], "datatype": layer[1]},
        }
        cell = {"gds_path": str(source), "cell_name": name, "ports": [port]}
        return {"id": name, "cell": cell}

    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [block("WEST", port_layer, 1.7, 0), block("EAST", (67, 20), 0, 180)],
        "placement": {
            "strategy": "explicit",
            "order": ["WEST", "EAST"],
            "origins_um": {"WEST": {"x": 0, "y": 0}, "EAST": {"x": 10, "y": 0}},
        },
        "connectivity": [
            {
                "net": "N",
                "pins": [{"block": name, "port": "P"} for name in ("WEST", "EAST")],
            }
        ],
        "routing": {"layer_role": primary, "width_um": 1.2, "suggest_layers": True},
        "options": {"cell_name": "TOP", "output": str(tmp_path / "result.gds")},
    }


def _assert_plate_connectivity(report, tmp_path, top_port):
    extracted = run_extract(
        report["gds_path"],
        "sky130",
        output=str(tmp_path / "plates.spice"),
        probes=[{"name": "east", "layer": "metal0", "x": 10.1, "y": 1}],
    )
    (east,) = extracted["probes"]
    (capacitor,) = extracted["devices"]
    assert east["found"] and east["net"] == "N"
    # Count devices on the probed cluster even if a disconnected route
    # carries the same text label as the east pad.
    east_net = next(n for n in extracted["nets"] if n["net_id"] == east["cluster_id"])
    assert east_net["device_count"] == (0 if top_port else 1)
    assert capacitor["nets"]["b"] != east["net"]
    assert (capacitor["nets"]["a"] == east["net"]) == (not top_port)


@pytest.mark.parametrize(
    ("plate_role", "top", "bottom"),
    [("cap_top_plate", (89, 44), (70, 20)), ("cap2_top_plate", (97, 44), (71, 20))],
)
@pytest.mark.parametrize("start_on_metal", [True, False], ids=["baseline", "candidate"])
@pytest.mark.parametrize(
    "top_port", [True, False], ids=["unsupported-top", "routable-bottom"]
)
def test_layer_search_matches_extracted_plate_connectivity(
    tmp_path, pdk_root, capsys, plate_role, top, bottom, start_on_metal, top_port
):
    primary = "metal" if start_on_metal else plate_role
    request = _plate_request(
        tmp_path, pdk_root, top, bottom, top if top_port else bottom, primary
    )
    path = tmp_path / "request.json"
    code = 3 if top_port else 0
    result = _run_cli(path, request, capsys, code)
    suggestion = result["layer_suggestions"]
    assert suggestion["status"] == ("conflict" if top_port else "resolved")
    assignments = {} if top_port or start_on_metal else {"N": "metal"}
    assert suggestion["assignments"] == assignments
    _assert_plate_connectivity(result, tmp_path, top_port)

    del request["routing"]["suggest_layers"]
    if assignments:
        request["connectivity"][0]["layer_role"] = assignments["N"]
    replay = _run_cli(path, request, capsys, code)
    assert replay["unrouted_nets"] == (["N"] if top_port else [])
    if top_port:
        assert "via-drop" in replay["nets"][0]["legs"][0]["reason"]
    _assert_plate_connectivity(replay, tmp_path, top_port)
