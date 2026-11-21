"""Synthetic-graph tests for the shortest series-path resistance query
(``klayout_tools.resistance_path``, issue #2765). Pure Python -- no KLayout,
no simulator, no PDK."""

from __future__ import annotations

import json
import math

import pytest

from klayout_tools.resistance_path import (
    QUANTITY,
    STATUSES,
    GraphResistor,
    ResistanceGraphError,
    query_parasitic_net,
    shortest_series_resistance,
)

R = GraphResistor


def _assert_failure_payload(result: dict) -> None:
    """Every non-success result: no resistance, no (partial) path."""
    assert result["status"] != "ok"
    assert result["resistance_ohm"] is None
    assert result["path_nodes"] == []
    assert result["path_resistors"] == []
    assert isinstance(result["reason"], str) and result["reason"]


def _path_sum(resistors: list[GraphResistor], ids: list[str]) -> float:
    by_id = {r.id: r.resistance_ohm for r in resistors}
    return sum(by_id[i] for i in ids)


def _assert_reconstructable(result: dict, resistors: list[GraphResistor]) -> None:
    """The ordered path is a real walk: each resistor joins its neighbours."""
    by_id = {r.id: r for r in resistors}
    nodes = result["path_nodes"]
    assert len(nodes) == len(result["path_resistors"]) + 1
    for i, rid in enumerate(result["path_resistors"]):
        r = by_id[rid]
        assert {r.node_a, r.node_b} == {nodes[i], nodes[i + 1]}
    assert result["resistance_ohm"] == pytest.approx(
        _path_sum(resistors, result["path_resistors"])
    )


# --------------------------------------------------------------------------- #
# ok
# --------------------------------------------------------------------------- #


def test_series_chain_sums_every_resistor():
    nodes = ["a", "b", "c", "d"]
    resistors = [
        R("r0", "a", "b", 1.5),
        R("r1", "b", "c", 2.0),
        R("r2", "c", "d", 3.25),
    ]
    result = shortest_series_resistance(nodes, resistors, "a", "d")
    assert result["status"] == "ok"
    assert result["reason"] is None
    assert result["resistance_ohm"] == pytest.approx(6.75)
    assert result["path_nodes"] == ["a", "b", "c", "d"]
    assert result["path_resistors"] == ["r0", "r1", "r2"]
    assert result["node_count"] == 4
    assert result["resistor_count"] == 3
    _assert_reconstructable(result, resistors)


def test_direction_is_irrelevant_reverse_query_mirrors_path():
    nodes = ["a", "b", "c"]
    resistors = [R("r0", "a", "b", 1.0), R("r1", "b", "c", 2.0)]
    result = shortest_series_resistance(nodes, resistors, "c", "a")
    assert result["resistance_ohm"] == pytest.approx(3.0)
    assert result["path_nodes"] == ["c", "b", "a"]
    assert result["path_resistors"] == ["r1", "r0"]


def test_cheaper_alternate_route_wins_over_fewer_hops():
    nodes = ["a", "b", "c", "d"]
    resistors = [
        R("direct", "a", "d", 10.0),
        R("r0", "a", "b", 1.0),
        R("r1", "b", "c", 1.0),
        R("r2", "c", "d", 1.0),
    ]
    result = shortest_series_resistance(nodes, resistors, "a", "d")
    assert result["resistance_ohm"] == pytest.approx(3.0)
    assert result["path_resistors"] == ["r0", "r1", "r2"]
    _assert_reconstructable(result, resistors)


def test_parallel_edges_are_not_combined():
    """2 ohm || 3 ohm is 1.2 ohm effective -- this query reports the
    cheaper *series* path, 2 ohm, by design."""
    nodes = ["a", "b"]
    resistors = [R("r3", "a", "b", 3.0), R("r2", "a", "b", 2.0)]
    result = shortest_series_resistance(nodes, resistors, "a", "b")
    assert result["status"] == "ok"
    assert result["resistance_ohm"] == 2.0
    assert result["resistance_ohm"] != pytest.approx(1.2)
    assert result["path_resistors"] == ["r2"]


def test_equal_cost_parallel_edges_pick_the_first_in_input_order():
    nodes = ["a", "b"]
    resistors = [R("first", "a", "b", 2.0), R("second", "a", "b", 2.0)]
    assert shortest_series_resistance(nodes, resistors, "a", "b")["path_resistors"] == [
        "first"
    ]
    assert shortest_series_resistance(nodes, resistors[::-1], "a", "b")[
        "path_resistors"
    ] == ["second"]


def test_equal_cost_routes_are_deterministic_and_documented_by_input_order():
    """Two 2-ohm routes a-b-d and a-c-d: the route through the node earlier
    in ``nodes`` is settled first and kept; repeated runs agree; reordering
    the input may change the route but never the resistance."""
    resistors = [
        R("ab", "a", "b", 1.0),
        R("bd", "b", "d", 1.0),
        R("ac", "a", "c", 1.0),
        R("cd", "c", "d", 1.0),
    ]
    first = shortest_series_resistance(["a", "b", "c", "d"], resistors, "a", "d")
    assert first["path_nodes"] == ["a", "b", "d"]
    for _ in range(5):
        assert (
            shortest_series_resistance(["a", "b", "c", "d"], resistors, "a", "d")
            == first
        )
    swapped = shortest_series_resistance(["a", "c", "b", "d"], resistors, "a", "d")
    assert swapped["path_nodes"] == ["a", "c", "d"]
    assert swapped["resistance_ohm"] == first["resistance_ohm"]


def test_identical_existing_endpoints_are_zero_with_one_node_path():
    nodes = ["a", "b"]
    resistors = [R("r0", "a", "b", 5.0)]
    result = shortest_series_resistance(nodes, resistors, "a", "a")
    assert result["status"] == "ok"
    assert result["resistance_ohm"] == 0.0
    assert result["path_nodes"] == ["a"]
    assert result["path_resistors"] == []


def test_zero_ohm_cycle_terminates_and_is_used():
    nodes = ["a", "b", "c", "d"]
    resistors = [
        R("z0", "a", "b", 0.0),
        R("z1", "b", "c", 0.0),
        R("z2", "c", "a", 0.0),
        R("out", "c", "d", 4.0),
        R("loop", "b", "b", 0.0),
    ]
    result = shortest_series_resistance(nodes, resistors, "a", "d")
    assert result["status"] == "ok"
    assert result["resistance_ohm"] == 4.0
    _assert_reconstructable(result, resistors)
    assert len(result["path_nodes"]) == len(set(result["path_nodes"]))


def test_isolated_extra_nodes_do_not_affect_ok():
    result = shortest_series_resistance(
        ["a", "island", "b"], [R("r", "a", "b", 1.0)], "a", "b"
    )
    assert result["resistance_ohm"] == 1.0


# --------------------------------------------------------------------------- #
# no_path / node_not_found
# --------------------------------------------------------------------------- #


def test_disconnected_existing_endpoints_are_no_path():
    nodes = ["a", "b", "c", "d"]
    resistors = [R("r0", "a", "b", 1.0), R("r1", "c", "d", 1.0)]
    result = shortest_series_resistance(nodes, resistors, "a", "d")
    assert result["status"] == "no_path"
    _assert_failure_payload(result)
    assert result["node_count"] == 4


@pytest.mark.parametrize(
    ("source", "target"), [("a", "zz"), ("zz", "a"), ("zz", "zz"), ("x", "y")]
)
def test_absent_endpoints_are_node_not_found(source, target):
    result = shortest_series_resistance(
        ["a", "b"], [R("r0", "a", "b", 1.0)], source, target
    )
    assert result["status"] == "node_not_found"
    _assert_failure_payload(result)


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #


def _chain(n: int) -> tuple[list[str], list[GraphResistor]]:
    nodes = [f"n{i}" for i in range(n)]
    return nodes, [R(f"r{i}", nodes[i], nodes[i + 1], 1.0) for i in range(n - 1)]


def test_exactly_at_both_caps_is_searched():
    nodes, resistors = _chain(5)
    result = shortest_series_resistance(
        nodes, resistors, "n0", "n4", max_nodes=5, max_resistors=4
    )
    assert result["status"] == "ok"
    assert result["resistance_ohm"] == 4.0


def test_below_caps_is_searched():
    nodes, resistors = _chain(5)
    result = shortest_series_resistance(
        nodes, resistors, "n0", "n4", max_nodes=6, max_resistors=5
    )
    assert result["status"] == "ok"


@pytest.mark.parametrize(("max_nodes", "max_resistors"), [(4, 4), (5, 3), (1, 1)])
def test_above_either_cap_is_limit_exceeded_without_partial_result(
    max_nodes, max_resistors
):
    nodes, resistors = _chain(5)
    result = shortest_series_resistance(
        nodes, resistors, "n0", "n4", max_nodes=max_nodes, max_resistors=max_resistors
    )
    assert result["status"] == "limit_exceeded"
    _assert_failure_payload(result)
    assert result["node_count"] == 5
    assert result["resistor_count"] == 4


def test_limit_is_checked_before_validation_and_endpoints():
    """An oversized graph is refused before its contents are even looked
    at -- malformed entries and absent endpoints included."""
    nodes = ["a", "b", "c"]
    resistors = [R("bad", "a", "nowhere", -1.0)]
    result = shortest_series_resistance(nodes, resistors, "zz", "a", max_nodes=2)
    assert result["status"] == "limit_exceeded"


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, None, "10"])
def test_invalid_caps_are_value_errors(bad):
    nodes, resistors = _chain(2)
    with pytest.raises(ValueError, match="max_nodes"):
        shortest_series_resistance(nodes, resistors, "n0", "n1", max_nodes=bad)
    with pytest.raises(ValueError, match="max_resistors"):
        shortest_series_resistance(nodes, resistors, "n0", "n1", max_resistors=bad)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [-0.5, math.nan, math.inf, -math.inf, "1", True])
def test_bad_resistance_values_are_rejected_before_search(value):
    with pytest.raises(ResistanceGraphError, match="resistance_ohm"):
        shortest_series_resistance(
            ["a", "b", "c"],
            [R("ok", "a", "b", 1.0), R("bad", "b", "c", value)],
            "a",
            "b",  # even a query the bad edge would not be on
        )


def test_dangling_endpoint_is_rejected_not_dropped():
    with pytest.raises(ResistanceGraphError, match="names no node"):
        shortest_series_resistance(
            ["a", "b"], [R("r0", "a", "b", 1.0), R("r1", "b", "ghost", 1.0)], "a", "b"
        )


def test_duplicate_node_and_resistor_ids_are_rejected():
    with pytest.raises(ResistanceGraphError, match="duplicate node"):
        shortest_series_resistance(["a", "a"], [], "a", "a")
    with pytest.raises(ResistanceGraphError, match="duplicate resistor"):
        shortest_series_resistance(
            ["a", "b"], [R("r", "a", "b", 1.0), R("r", "a", "b", 2.0)], "a", "b"
        )


def test_resistance_graph_error_is_a_value_error():
    assert issubclass(ResistanceGraphError, ValueError)


# --------------------------------------------------------------------------- #
# ladder adapter over `parasitics.nets[]`
# --------------------------------------------------------------------------- #


def _ladder_entry(net: str, net_id: int, legs: list[str], rs: list[float]) -> dict:
    return {
        "net": net,
        "net_id": net_id,
        "rc_model": "distributed",
        "terminals": [
            {"device": f"M{i}", "terminal": "G", "leg_net": leg, "order": i}
            for i, leg in enumerate(legs)
        ],
        "segments": [
            {"net_a": legs[i], "net_b": legs[i + 1], "resistance_ohm": r}
            for i, r in enumerate(rs)
        ],
    }


def test_adapter_ok_payload_echoes_query_and_model():
    nets = [_ladder_entry("OUT", 7, ["OUT__t0", "OUT", "OUT__t2"], [1.25, 2.5])]
    result = query_parasitic_net(nets, 7, "OUT__t0", "OUT__t2")
    assert result == {
        "net_id": 7,
        "net": "OUT",
        "rc_model": "distributed",
        "from": "OUT__t0",
        "to": "OUT__t2",
        "quantity": QUANTITY,
        "status": "ok",
        "reason": None,
        "resistance_ohm": 3.75,
        "path_nodes": ["OUT__t0", "OUT", "OUT__t2"],
        "path_resistors": ["seg0", "seg1"],
        "node_count": 3,
        "resistor_count": 2,
        "max_nodes": 10_000,
        "max_resistors": 10_000,
    }
    assert QUANTITY == "shortest_series_path_resistance"
    json.dumps(result, allow_nan=False)


def test_adapter_rounds_float_noise_to_report_precision():
    nets = [_ladder_entry("N", 1, ["a", "b", "c", "d"], [0.1, 0.2, 0.3])]
    assert query_parasitic_net(nets, 1, "a", "d")["resistance_ohm"] == 0.6


def test_adapter_same_display_name_different_net_ids_stay_distinct():
    """Two distinct `VGND` islands share a name -- and their ladders' middle
    legs even share a node spelling -- but a query selects by `net_id` and
    can never cross into the other island."""
    nets = [
        _ladder_entry("VGND", 3, ["VGND__t0", "VGND", "VGND__t2"], [1.0, 1.0]),
        _ladder_entry("VGND", 9, ["VGND__t0b", "VGND", "VGND__t2b"], [5.0, 5.0]),
    ]
    a = query_parasitic_net(nets, 3, "VGND__t0", "VGND__t2")
    b = query_parasitic_net(nets, 9, "VGND__t0b", "VGND__t2b")
    assert (a["resistance_ohm"], b["resistance_ohm"]) == (2.0, 10.0)
    crossing = query_parasitic_net(nets, 3, "VGND__t0", "VGND__t2b")
    assert crossing["status"] == "node_not_found"
    _assert_failure_payload(crossing)


def test_adapter_unknown_net_id_is_node_not_found():
    result = query_parasitic_net(
        [_ladder_entry("N", 1, ["a", "b"], [1.0])], 2, "a", "b"
    )
    assert result["status"] == "node_not_found"
    assert "net_id 2" in result["reason"]
    assert result["net"] is None
    assert result["node_count"] is None
    _assert_failure_payload(result)


def test_adapter_lumped_net_is_node_not_found():
    entry = {
        "net": "N",
        "net_id": 4,
        "rc_model": "lumped",
        "terminals": [{"leg_net": "N__t0"}, {"leg_net": "N__t1"}],
        "segments": [],
    }
    result = query_parasitic_net([entry], 4, "N__t0", "N__t1")
    assert result["status"] == "node_not_found"
    assert "lumped" in result["reason"]
    assert result["rc_model"] == "lumped"
    _assert_failure_payload(result)


def test_adapter_limit_exceeded_and_identical_endpoints():
    nets = [_ladder_entry("N", 1, ["a", "b", "c"], [1.0, 1.0])]
    over = query_parasitic_net(nets, 1, "a", "c", max_nodes=2)
    assert over["status"] == "limit_exceeded"
    assert over["max_nodes"] == 2
    _assert_failure_payload(over)
    same = query_parasitic_net(nets, 1, "b", "b")
    assert (same["status"], same["resistance_ohm"], same["path_nodes"]) == (
        "ok",
        0.0,
        ["b"],
    )


def test_adapter_malformed_segment_raises():
    entry = _ladder_entry("N", 1, ["a", "b"], [1.0])
    entry["segments"].append({"net_a": "b", "net_b": "ghost", "resistance_ohm": 1.0})
    with pytest.raises(ResistanceGraphError):
        query_parasitic_net([entry], 1, "a", "b")


def test_every_status_value_is_covered():
    assert set(STATUSES) == {"ok", "limit_exceeded", "node_not_found", "no_path"}
