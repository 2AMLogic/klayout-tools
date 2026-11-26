"""Replay layer suggestions when the primary or candidate has a non-metal role name."""

import klayout.db as kdb

from test_gen_compose import _make_install
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_blocking_net import _three_net_crossing_request
from test_gen_compose_layer_suggest import _walk_connectivity
from test_gen_compose_layer_suggest_baseline import _run_cli


def _sg13g2_crossing(tmp_path):
    """Three pinned horizontal tracks and one vertical track on eight Metal1 pads."""
    root = tmp_path / "pdk"
    _make_install(root, "sg13g2")
    gds = tmp_path / "pad.gds"
    layout = kdb.Layout()
    pad = layout.create_cell("PAD")
    pad.shapes(layout.layer(8, 0)).insert(kdb.DBox(0, 0, 2, 2))
    layout.write(str(gds))

    blocks = []
    origins = {}

    def endpoint(name, origin, xy, direction):
        origins[name] = {"x": origin[0], "y": origin[1]}
        port = {
            "name": "P",
            "x_um": xy[0],
            "y_um": xy[1],
            "width_um": 1.2,
            "direction_deg": direction,
            "layer": {"layer": 8, "datatype": 0},
        }
        cell = {"gds_path": str(gds), "cell_name": "PAD", "ports": [port]}
        blocks.append({"id": name, "cell": cell})
        return {"block": name, "port": "P"}

    nets = []
    for i, role in enumerate(("metal", "metal2", "metal3")):
        west = endpoint(f"w{i}", (0, 10 * i), (2, 1), 0)
        east = endpoint(f"e{i}", (30, 10 * i), (0, 1), 180)
        nets.append({"net": f"H{i}", "pins": [west, east], "layer_role": role})
    south = endpoint("s", (15, -10), (1, 2), 90)
    north = endpoint("n", (15, 30), (1, 0), 270)
    nets.append(
        {
            "net": "V",
            "pins": [south, north],
            "width_um": 1.8,
            "waypoints_um": [[16, 6], [16, 16]],
        }
    )
    request = {
        "pdk": {"variant": "sg13g2", "root": str(root)},
        "blocks": blocks,
        "placement": {
            "strategy": "explicit",
            "order": list(origins),
            "origins_um": origins,
        },
        "connectivity": nets,
        "routing": {"layer_role": "metal", "width_um": 1.2},
        "options": {"cell_name": "TOP", "output": str(tmp_path / "result.gds")},
    }
    return request, {b["id"]: b["cell"] for b in blocks}


def _assert_replay(path, request, reports, capsys, result, family):
    suggestion = result["layer_suggestions"]
    assert suggestion["status"] == "resolved"
    del request["routing"]["suggest_layers"]
    del request["routing"]["suggest_layers_budget"]
    for entry in request["connectivity"]:
        if entry["net"] in suggestion["assignments"]:
            entry["layer_role"] = suggestion["assignments"][entry["net"]]
    replay = _run_cli(path, request, capsys, 0)
    assert replay == {k: v for k, v in result.items() if k != "layer_suggestions"}
    joined, shorts = _walk_connectivity(replay, reports, request, family)
    assert all(joined.values()) and not shorts


def test_sg13g2_crossing_uses_metal_plane_with_only_a_capacitor_role(tmp_path, capsys):
    request, reports = _sg13g2_crossing(tmp_path)
    path = tmp_path / "request.json"
    request["routing"].update(suggest_layers=True, suggest_layers_budget=3)
    partial = _run_cli(path, request, capsys, 3)["layer_suggestions"]
    assert partial["status"] == "budget_exhausted"
    assert partial["candidates_evaluated"] == 3
    assert partial["roles"] == [
        "metal2",
        "metal3",
        "cap_bottom_plate",
        "cap_top_via_metal",
    ]
    assert partial["candidate_space"] == 5

    request["routing"]["suggest_layers_budget"] = 4
    result = _run_cli(path, request, capsys, 0)
    assert result["layer_suggestions"]["assignments"] == {"V": "cap_bottom_plate"}
    _assert_replay(path, request, reports, capsys, result, "sg13g2")


def test_sky130_poly_primary_searches_the_metal_stack(tmp_path, pdk_root, capsys):
    request, reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "result.gds"
    )
    path = tmp_path / "request.json"
    request["routing"] = {"layer_role": "poly", "width_um": 0.17}
    baseline = _run_cli(path, request, capsys, 3)
    assert baseline["unrouted_nets"] == ["NET_H", "NET_V1", "NET_V2"]
    assert "no via-drop" in baseline["nets"][0]["legs"][0]["reason"]
    request["routing"].update(suggest_layers=True, suggest_layers_budget=1)
    partial = _run_cli(path, request, capsys, 3)["layer_suggestions"]
    assert partial["status"] == "budget_exhausted"
    assert partial["roles"] == [
        "metal",
        "metal2",
        "metal3",
        "metal4",
        "metal5",
        "metal6",
    ]
    assert partial["candidates_evaluated"] == 1
    assert partial["candidate_space"] == 7**3

    request["routing"]["suggest_layers_budget"] = 66
    result = _run_cli(path, request, capsys, 0)
    assert result["layer_suggestions"]["candidates_evaluated"] == 66
    assert result["layer_suggestions"]["assignments"] == {
        "NET_H": "metal",
        "NET_V1": "metal2",
        "NET_V2": "metal3",
    }
    _assert_replay(path, request, reports, capsys, result, "sky130")
