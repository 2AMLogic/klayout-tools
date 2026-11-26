"""Public CLI recovery from a layer-dependent failure on the first assignment."""

import json

import pytest

from klayout_tools.cli import main
from test_gen_compose import _gen_block
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_blocking_net import _three_net_crossing_request
from test_gen_compose_layer_suggest import _walk_connectivity
from test_gen_compose_port_pad_override import _request as _label_access_request


def _run_cli(path, request, capsys, expected_exit):
    path.write_text(json.dumps(request))
    code = main(["gen-compose", str(path), "--format", "json"])
    captured = capsys.readouterr()
    assert code == expected_exit, captured.err or captured.out
    return json.loads(captured.err if code == 1 else captured.out)


@pytest.mark.parametrize(
    ("case", "winning_trial"),
    [("pin_collision", 10), ("width_floor", 4), ("pad_floor", 4)],
)
def test_rejected_baseline_counts_as_a_trial_and_solution_replays(
    tmp_path, pdk_root, monkeypatch, capsys, case, winning_trial
):
    output = tmp_path / "result.gds"
    request, reports = _three_net_crossing_request(tmp_path, pdk_root, output)
    # The baseline produces no report from which to read the resolved variant.
    monkeypatch.setenv("PDK", request["pdk"].pop("variant"))
    if case == "pin_collision":
        stub = _gen_block(tmp_path, pdk_root, "resistor_strip", "stub", num=1)
        request["blocks"].append({"id": "stub", "generator_report": stub})
        request["placement"]["order"].append("stub")
        request["placement"]["origins_um"]["stub"] = {"x": 10, "y": 0}
        request["pins"] = [
            {"net": "PORT", "block": "stub", "port": "P2", "request": {"y_um": 20}}
        ]
    else:
        request["connectivity"] = request["connectivity"][:1]
        request["routing"] = {"layer_role": "metal4", "width_um": 0.5}
        if case == "width_floor":
            request["connectivity"][0]["width_um"] = 0.17
        else:
            request["routing"]["landing_pad_size_um"] = 0.3

    path = tmp_path / "request.json"
    original = _run_cli(path, request, capsys, 1)
    error = original["error"]
    if case == "pin_collision":
        assert error["code"] == "port_request_conflict"
        assert error["conflicts_with"] == {"net": "NET_H"}
    else:
        rule = "met3.width.1" if case == "width_floor" else "met3.area.1"
        assert rule in error["message"]

    request["routing"]["suggest_layers"] = True
    result = _run_cli(path, request, capsys, 0)
    suggestion = result["layer_suggestions"]
    assert suggestion["status"] == "resolved"
    assert suggestion["candidates_evaluated"] == winning_trial
    saved = output.read_bytes()

    # Even after later trials, an incomplete search has no original layout
    # to publish. Keep the same diagnostic and the previously routed GDS.
    request["routing"]["suggest_layers_budget"] = winning_trial - 1
    assert _run_cli(path, request, capsys, 1) == original
    assert output.read_bytes() == saved
    request["routing"]["suggest_layers_budget"] = winning_trial
    assert _run_cli(path, request, capsys, 0)["layer_suggestions"] == suggestion

    del request["routing"]["suggest_layers"]
    del request["routing"]["suggest_layers_budget"]
    for entry in request["connectivity"]:
        if entry["net"] in suggestion["assignments"]:
            entry["layer_role"] = suggestion["assignments"][entry["net"]]
    replay = _run_cli(path, request, capsys, 0)
    assert replay == {k: v for k, v in result.items() if k != "layer_suggestions"}
    joined, shorts = _walk_connectivity(replay, reports, request)
    assert all(joined.values()) and not shorts
    if case == "pin_collision":
        pin = replay["pins"][0]
        assert pin["labelled"] and pin["net"] == "PORT"
        assert (pin["achieved"]["x_um"], pin["achieved"]["y_um"]) == (12.0, 20.0)


@pytest.mark.parametrize("pinned", [False, True])
def test_no_legal_plane_preserves_the_baseline_error(
    tmp_path, pdk_root, capsys, pinned
):
    output = tmp_path / "new.gds"
    request, _ = _three_net_crossing_request(tmp_path, pdk_root, output)
    net = request["connectivity"][0]
    request["connectivity"] = [net]
    net["width_um"] = 0.01
    if pinned:
        net["layer_role"] = "metal"
    path = tmp_path / "request.json"
    original = _run_cli(path, request, capsys, 1)
    assert "width_um" in original["error"]["message"]
    request["routing"]["suggest_layers"] = True
    assert _run_cli(path, request, capsys, 1) == original
    assert not output.exists()


def test_fixed_block_access_error_precedes_routing_validation(
    tmp_path, pdk_root, capsys
):
    path = _label_access_request(tmp_path, pdk_root, 0.1)
    request = json.loads(path.read_text())
    del request["routing"]["layer_role"]
    original = _run_cli(path, request, capsys, 1)
    assert "landing_pad_size_um" in original["error"]["message"]
    request["routing"]["suggest_layers"] = True
    assert _run_cli(path, request, capsys, 1) == original
