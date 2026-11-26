"""Per-net layer suggestions on three crossing sky130A nets (#1962).

The independent GDS check traces transformed terminals over conductors
and joins adjacent levels through the actual via cuts.
"""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.decks import get_extraction_deck
from klayout_tools.gen_compose import GenComposeError, compose
from klayout_tools.gen_compose_layer_suggest import _candidate_climbs
from test_gen_compose import _gen_block, _make_install
from test_gen_compose_blocking_net import _three_net_crossing_request


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space (test_pdk.py)."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


def _walk_connectivity(report, gen_reports, request, family="sky130"):
    """The independent GDS connectivity walk: ``{net: terminals joined}``
    plus the set of other nets each net's cluster touches.

    Never reads the composer's own routing bookkeeping -- only the written
    GDS, the deck's own metals/vias stack, and the request's pin list
    (block port geometry + the report's placement offsets).
    """
    deck = get_extraction_deck(family)
    layout = kdb.Layout()
    layout.read(report["gds_path"])
    top = layout.cell(report["cell_name"])
    unit = layout.dbu

    flat = kdb.Layout()
    flat.dbu = unit
    ftop = flat.create_cell(top.name)
    metals: list[kdb.Region] = []
    vias: list[kdb.Region] = []
    for layer in deck.metals:
        metals.append(_flattened_region(layout, top, flat, ftop, layer))
    for layer in deck.vias:
        vias.append(_flattened_region(layout, top, flat, ftop, layer))
    layer_index = {layer: i for i, layer in enumerate(deck.metals)}

    def probe(x_um: float, y_um: float) -> kdb.Region:
        x, y = round(x_um / unit), round(y_um / unit)
        return kdb.Region(kdb.Box(x - 5, y - 5, x + 5, y + 5))

    def flood(seed_index: int, seed: kdb.Region) -> list[kdb.Region]:
        grown = [kdb.Region() for _ in metals]
        grown[seed_index] = metals[seed_index].interacting(seed)
        while True:
            fresh = [kdb.Region() for _ in metals]
            for k, via in enumerate(vias):
                bridging = via.interacting(grown[k] | grown[k + 1])
                if bridging.is_empty():
                    continue
                fresh[k] += metals[k].interacting(bridging)
                fresh[k + 1] += metals[k + 1].interacting(bridging)
            if all(
                ((grown[i] | fresh[i]) - grown[i]).is_empty()
                for i in range(len(metals))
            ):
                return grown
            grown = [grown[i] | fresh[i] for i in range(len(metals))]

    pins_of = _pin_positions(report, gen_reports, request, layer_index)

    joined = _joined_nets(pins_of, probe, flood)
    shorts = _shorted_nets(pins_of, probe, flood)
    return joined, shorts


def _pin_positions(report, gen_reports, request, layer_index):
    """Each net's two terminals, composed frame, as ``(x_um, y_um,
    metals-stack index)`` -- from the blocks' own reported port geometry
    plus the response's placement offsets."""
    offsets = {b["id"]: b["offset_um"] for b in report["blocks"]}
    pins_of = {}
    for entry in request["connectivity"]:
        pins = []
        for pin in entry["pins"]:
            port = next(
                p
                for p in gen_reports[pin["block"]]["ports"]
                if p["name"] == pin["port"]
            )
            offset = offsets[pin["block"]]
            layer = (port["layer"]["layer"], port["layer"]["datatype"])
            pins.append(
                (
                    port["x_um"] + offset["x"],
                    port["y_um"] + offset["y"],
                    layer_index[layer],
                )
            )
        pins_of[entry["net"]] = pins
    return pins_of


def _joined_nets(pins_of, probe, flood):
    """``{net: both terminals in one conductor cluster}``."""
    joined = {}
    for net, pins in pins_of.items():
        (ax, ay, ai), (bx, by, bi) = pins
        cluster = flood(ai, probe(ax, ay))
        joined[net] = not cluster[bi].interacting(probe(bx, by)).is_empty()
    return joined


def _shorted_nets(pins_of, probe, flood):
    """``{(net, other)}`` pairs whose clusters touch each other's terminals."""
    shorts = set()
    for net, pins in pins_of.items():
        (ax, ay, ai), _ = pins
        cluster = flood(ai, probe(ax, ay))
        for other, other_pins in pins_of.items():
            if other == net:
                continue
            for x, y, i in other_pins:
                if not cluster[i].interacting(probe(x, y)).is_empty():
                    shorts.add((net, other))
    return shorts


def _flattened_region(layout, top, flat, ftop, layer):
    src = layout.find_layer(layer[0], layer[1])
    if src is None:
        return kdb.Region()
    dst = flat.layer(layer[0], layer[1])
    ftop.shapes(dst).insert(top.begin_shapes_rec(src))
    return kdb.Region(ftop.shapes(dst)).merged()


def _compose_with_mode(request):
    mode_on = json.loads(json.dumps(request))
    mode_on["routing"]["suggest_layers"] = True
    return compose(mode_on)


def test_mode_off_still_routes_two_of_three_with_blocker(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "mode_off.gds"
    )
    report = compose(request)

    assert report["unrouted_nets"] == ["NET_V2"]
    nets = {net["net"]: net for net in report["nets"]}
    assert nets["NET_H"]["routed"] is True
    assert nets["NET_V1"]["routed"] is True
    blocker = next(leg for leg in nets["NET_V2"]["legs"] if leg["reason"] is not None)
    assert "crosses already-routed net" in blocker["reason"]
    assert blocker["blocking_net"] == "NET_H"
    # The walk is not vacuous: the unrouted net's terminals are NOT joined.
    joined, _ = _walk_connectivity(report, _gen_reports(request), request)
    assert joined == {"NET_H": True, "NET_V1": True, "NET_V2": False}


def test_suggest_layers_routes_all_three_in_declaration_order(tmp_path, pdk_root):
    request, gen_reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "mode_on.gds"
    )
    report = _compose_with_mode(request)

    assert report["unrouted_nets"] == []
    assert [net["net"] for net in report["nets"]] == ["NET_H", "NET_V1", "NET_V2"]
    assert all(net["routed"] for net in report["nets"])
    suggestions = report["layer_suggestions"]
    assert suggestions["status"] == "resolved"
    # The one known solution: only NET_V2 moves, onto metal3.
    assert suggestions["assignments"] == {"NET_V2": "metal3"}
    # The caller's own request document is never mutated.
    assert "layer_role" not in request["connectivity"][2]
    joined, shorts = _walk_connectivity(report, gen_reports, request)
    assert joined == {"NET_H": True, "NET_V1": True, "NET_V2": True}
    assert not shorts
    # Placement, pins, width and waypoints are untouched: the blocks echo
    # exactly the mode-off run's placement (at its own output path, so the
    # mode-on GDS the walk just read stays the one on disk).
    plain = json.loads(json.dumps(request))
    plain["options"]["output"] = str(tmp_path / "mode_off.gds")
    baseline = compose(plain)
    assert report["blocks"] == baseline["blocks"]


def _gen_reports(request):
    return {b["id"]: b["generator_report"] for b in request["blocks"]}


def test_suggested_mapping_replays_through_the_public_cli(tmp_path, pdk_root, capsys):
    request, gen_reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "replay.gds"
    )
    # On the trial that moves NET_H to metal2, NET_V1 crosses the PORT stub.
    stub = _gen_block(tmp_path, pdk_root, "resistor_strip", "stub", num=1)
    request["blocks"].append({"id": "stub", "generator_report": stub})
    request["placement"]["order"].append("stub")
    request["placement"]["origins_um"]["stub"] = {"x": 0.0, "y": 20.0}
    request["pins"] = [
        {"net": "PORT", "block": "stub", "port": "P2", "request": {"x_um": 22.0}}
    ]
    mode_on = json.loads(json.dumps(request))
    mode_on["routing"]["suggest_layers"] = True
    path = tmp_path / "mode_on.json"
    path.write_text(json.dumps(mode_on))

    assert main(["gen-compose", str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assignments = payload["layer_suggestions"]["assignments"]
    assert assignments == {"NET_V2": "metal3"}
    assert payload["layer_suggestions"]["candidates_evaluated"] == 5

    # The default text output also reports the mapping.
    assert main(["gen-compose", str(path)]) == 0
    text = capsys.readouterr().out
    assert "layer_suggestions:" in text
    assert "suggested: NET_V2 -> metal3" in text

    # The replay: the ordinary command with the suggested roles copied in.
    replay = json.loads(json.dumps(request))
    replay["connectivity"][2]["layer_role"] = assignments["NET_V2"]
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(json.dumps(replay))

    assert main(["gen-compose", str(replay_path), "--format", "json"]) == 0
    replayed = json.loads(capsys.readouterr().out)
    pin = replayed["pins"][0]
    assert pin["net"] == "PORT" and pin["labelled"]
    assert (pin["achieved"]["x_um"], pin["achieved"]["y_um"]) == (22.0, 20.21)
    assert replayed["unrouted_nets"] == []
    assert all(net["routed"] for net in replayed["nets"])
    # The replay reproduces every ordinary compose field.
    assert {k: v for k, v in payload.items() if k != "layer_suggestions"} == replayed

    joined, shorts = _walk_connectivity(replayed, gen_reports, request)
    assert joined == {"NET_H": True, "NET_V1": True, "NET_V2": True}
    assert not shorts


def test_suggestions_are_deterministic(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "determinism.gds"
    )
    first = _compose_with_mode(request)
    second = _compose_with_mode(request)
    assert first == second
    assert first["layer_suggestions"]["assignments"] == {"NET_V2": "metal3"}
    assert (
        first["layer_suggestions"]["candidates_evaluated"]
        == second["layer_suggestions"]["candidates_evaluated"]
    )


def test_suggest_layers_honours_a_pinned_net(tmp_path, pdk_root):
    request, gen_reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "pinned.gds"
    )
    request["connectivity"][1]["layer_role"] = "metal2"
    report = _compose_with_mode(request)

    # NET_V1's own pin is honoured verbatim: it never appears in the
    # suggested mapping and it routes on its named plane.
    assert report["unrouted_nets"] == []
    assert report["layer_suggestions"]["assignments"] == {"NET_V2": "metal3"}
    nets = {net["net"]: net for net in report["nets"]}
    assert nets["NET_V1"]["routed"] is True
    joined, shorts = _walk_connectivity(report, gen_reports, request)
    assert joined == {"NET_H": True, "NET_V1": True, "NET_V2": True}
    assert not shorts


def test_conflict_when_pinned_nets_prevent_a_solution(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "conflict.gds"
    )
    # Pin NET_V1 and NET_V2 to the same plane: their caller-owned waypoint
    # lanes are both the x=16 vertical, NET_V2's a superset of NET_V1's, so
    # NET_V2's metal2 backbone can never be drawn without overlapping
    # NET_V1's accepted one -- and a pinned net gets no fallback, climb or
    # track retry. Only NET_H is searchable, and no assignment of it can
    # move either pinned lane: the whole space fails, a verified conflict
    # with the caller's own pin named as the unresolved net.
    request["connectivity"][1]["layer_role"] = "metal2"
    request["connectivity"][2]["layer_role"] = "metal2"
    report = _compose_with_mode(request)

    suggestions = report["layer_suggestions"]
    assert suggestions["status"] == "conflict"
    assert suggestions["assignments"] == {}
    assert suggestions["unresolved_nets"] == ["NET_V2"]
    # One searchable net x (inherit + 5 roles) = 6 assignments: the baseline
    # plus five candidates, all inside the default budget, so the conflict
    # is verified, not budget-truncated.
    assert suggestions["candidate_space"] == 6
    assert suggestions["candidates_evaluated"] == 6
    assert "none routed every net" in suggestions["note"]
    # The response is the request's own (mode-off) outcome plus the report.
    plain = compose(json.loads(json.dumps(request)))
    assert {k: v for k, v in report.items() if k != "layer_suggestions"} == plain


def test_conflict_reports_the_closest_trial(tmp_path, pdk_root):
    request, reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "two_choices.gds"
    )
    horizontal, v1, v2 = request["connectivity"]
    horizontal["layer_role"] = "metal"
    v2["layer_role"] = "metal2"
    pins = []
    for pin in horizontal["pins"]:
        block = pin["block"]
        duplicate = block + "_high"
        request["blocks"].append({"id": duplicate, "generator_report": reports[block]})
        request["placement"]["order"].append(duplicate)
        origin = request["placement"]["origins_um"][block]
        request["placement"]["origins_um"][duplicate] = {**origin, "y": 10.0}
        pins.append({**pin, "block": duplicate})
    request["connectivity"].insert(
        1, {"net": "H3", "pins": pins, "layer_role": "metal3"}
    )

    suggestions = _compose_with_mode(request)["layer_suggestions"]
    assert suggestions["status"] == "conflict"
    assert suggestions["unresolved_nets"] == ["NET_V2"]
    assert "closest trial" in suggestions["note"]
    v1["layer_role"] = "metal3"
    assert compose(request)["unrouted_nets"] == ["NET_V1"]


def test_candidate_read_error_propagates(tmp_path, pdk_root, monkeypatch):
    from klayout_tools import gen_compose

    request, reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "source.gds"
    )
    original = gen_compose.compose

    def remove_after_baseline(request, request_dir=None):
        baseline = original(request, request_dir)
        Path(reports["bw"]["gds_path"]).unlink()
        return baseline

    monkeypatch.setattr(gen_compose, "compose", remove_after_baseline)
    with pytest.raises(GenComposeError, match="bw"):
        _compose_with_mode(request)


def test_candidate_order_is_bounded_on_large_requests():
    expected = sorted(product(range(3), repeat=4), key=lambda c: (sum(c), c))
    assert list(_candidate_climbs(4, 2)) == expected[1:]
    assert next(_candidate_climbs(1100, 5)) == (0,) * 1099 + (1,)


def test_budget_exhaustion_is_reported_and_bounds_the_search(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "budget.gds"
    )
    request["connectivity"][1]["layer_role"] = "metal2"
    request["connectivity"][2]["layer_role"] = "metal2"
    mode_on = json.loads(json.dumps(request))
    mode_on["routing"]["suggest_layers"] = True
    mode_on["routing"]["suggest_layers_budget"] = 2
    report = compose(mode_on)

    suggestions = report["layer_suggestions"]
    assert suggestions["status"] == "budget_exhausted"
    assert suggestions["candidates_evaluated"] == 2  # baseline + one candidate
    assert suggestions["candidates_evaluated"] <= 2
    assert suggestions["unresolved_nets"]
    assert "suggest_layers_budget" in suggestions["note"]


def test_error_when_every_net_is_already_pinned(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "all_pinned.gds"
    )
    for entry in request["connectivity"]:
        entry["layer_role"] = "metal2"
    with pytest.raises(GenComposeError, match="nothing to search"):
        _compose_with_mode(request)


@pytest.mark.parametrize("mode", ["yes", "", 0, None])
def test_error_on_non_boolean_flag(tmp_path, pdk_root, mode):
    request, _ = _three_net_crossing_request(tmp_path, pdk_root, tmp_path / "flag.gds")
    request["routing"]["suggest_layers"] = mode
    with pytest.raises(GenComposeError, match="must be a boolean"):
        compose(request)


@pytest.mark.parametrize("budget", [0, None])
def test_error_on_non_positive_budget(tmp_path, pdk_root, budget):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "budget0.gds"
    )
    request["routing"]["suggest_layers"] = True
    request["routing"]["suggest_layers_budget"] = budget
    with pytest.raises(GenComposeError, match="positive integer"):
        _compose_with_mode(request)


def test_error_when_routing_carries_only_the_suggestion_keys(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(tmp_path, pdk_root, tmp_path / "bare.gds")
    request["routing"] = {"suggest_layers": True}
    with pytest.raises(GenComposeError, match="routing.layer_role"):
        _compose_with_mode(request)


def test_error_when_no_connectivity_to_search(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(tmp_path, pdk_root, tmp_path / "empty.gds")
    del request["connectivity"]
    request["routing"]["suggest_layers"] = True
    with pytest.raises(GenComposeError, match="non-empty connectivity"):
        _compose_with_mode(request)


def test_already_routing_request_returns_the_empty_mapping(tmp_path, pdk_root):
    request, gen_reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "solved.gds"
    )
    for entry in request["connectivity"]:
        entry["layer_role"] = "metal2"
    request["connectivity"][0]["layer_role"] = "metal"
    request["connectivity"][2]["layer_role"] = "metal3"
    report = _compose_with_mode(request)

    assert report["unrouted_nets"] == []
    suggestions = report["layer_suggestions"]
    assert suggestions["status"] == "resolved"
    assert suggestions["assignments"] == {}
    assert suggestions["candidates_evaluated"] == 1
    assert "routes every net as written" in suggestions["note"]


def test_failed_suggestion_preserves_existing_layout(tmp_path, pdk_root, capsys):
    output = tmp_path / "last_good.gds"
    request, reports = _three_net_crossing_request(tmp_path, pdk_root, output)
    h, v1, v2 = request["connectivity"]
    h["layer_role"], v1["layer_role"], v2["layer_role"] = "metal", "metal2", "metal3"
    good = compose(request)
    joined, shorts = _walk_connectivity(good, reports, request)
    assert all(joined.values()) and not shorts
    before = output.read_bytes()
    v2["layer_role"] = "metal2"
    request["routing"]["suggest_layers"] = True
    path = tmp_path / "pinned.json"
    path.write_text(json.dumps(request))
    assert main(["gen-compose", str(path), "--format", "json"]) == 1
    capture = capsys.readouterr()
    assert "nothing to search" in capture.out + capture.err
    assert output.read_bytes() == before


def test_candidate_landing_pad_floor_does_not_abort_search(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(tmp_path, pdk_root, tmp_path / "pad.gds")
    request["routing"].update(width_um=0.5, landing_pad_size_um=0.3)
    for net in request["connectivity"][1:]:
        net["layer_role"] = "metal2"
    assert compose(request)["unrouted_nets"] == ["NET_V2"]
    result = _compose_with_mode(request)["layer_suggestions"]
    assert result["status"] == "conflict"
    assert result["candidates_evaluated"] == result["candidate_space"]


def test_search_can_resolve_on_a_lower_plane(tmp_path, pdk_root):
    request, reports = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "down.gds"
    )
    request["connectivity"].pop()
    request["routing"]["layer_role"] = "metal3"
    request["routing"].pop("cross_block_layer_role")
    request["connectivity"][0]["layer_role"] = "metal3"
    result = _compose_with_mode(request)
    suggestion = result["layer_suggestions"]
    assert suggestion["status"] == "resolved"
    assert suggestion["assignments"] == {"NET_V1": "metal2"}
    joined, shorts = _walk_connectivity(result, reports, request)
    assert joined == {"NET_H": True, "NET_V1": True} and not shorts
