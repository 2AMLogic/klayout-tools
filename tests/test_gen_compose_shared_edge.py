"""Landing-pad geometry must stay electrically separate from another net.

A fallback via landing must stay separate from another net's *route*
(#7024); a pad sharing an edge or corner with another net's *block*
geometry must not compose ``routed: true`` either (#7325).
"""

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.gen_compose import compose
from klayout_tools.gen_compose_plane_climb import _climbable
from test_gen_compose import _gen_block
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

MET1, LI1 = (68, 20), (67, 20)


def _compose_cell_strips(tmp_path, specs, connectivity):
    """Hand-drawn ``blocks[].cell`` strips with declared ports, composed and
    extracted -- the fixture shape issue #7325's reproducer established.

    An empty sky130A root keeps every deck spacing rule unresolved, so the
    only rejection these tests can produce is an electrical-contact one.
    ``specs`` maps a block id to ``(layer, box, port_xy, direction_deg,
    origin)``; every net routes against the same metal2/metal3 request
    defaults the reproducer used.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    library = tmp_path / "library.gds"
    blocks = []
    for name, (layer, box, port_xy, direction_deg, _origin) in specs.items():
        layout.create_cell(name).shapes(layout.layer(*layer)).insert(
            kdb.DBox(*box).to_itype(layout.dbu)
        )
        blocks.append(
            {
                "id": name,
                "cell": {
                    "gds_path": str(library),
                    "cell_name": name,
                    "ports": [
                        {
                            "name": "P",
                            "x_um": port_xy[0],
                            "y_um": port_xy[1],
                            "width_um": box[3] - box[1],
                            "direction_deg": direction_deg,
                            "layer": {"layer": layer[0], "datatype": layer[1]},
                        }
                    ],
                },
            }
        )
    layout.write(str(library))
    (tmp_path / "pdk" / "sky130A" / "libs.tech").mkdir(parents=True)
    output = str(tmp_path / "top.gds")
    result = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(tmp_path / "pdk")},
            "blocks": blocks,
            "placement": {
                "strategy": "explicit",
                "order": list(specs),
                "origins_um": {
                    name: {"x": origin[0], "y": origin[1]}
                    for name, (_l, _b, _p, _d, origin) in specs.items()
                },
            },
            "connectivity": connectivity,
            "routing": {"layer_role": "metal2", "width_um": 0.14},
            "options": {"cell_name": "top", "output": output},
        },
        request_dir=str(tmp_path),
    )
    return result, run_extract(output, "sky130", top="top")


def _net_b_on_metal3():
    return {
        "net": "B",
        "pins": [{"block": "c", "port": "P"}, {"block": "d", "port": "P"}],
        "layer_role": "metal3",
        "width_um": 0.3,
    }


def _net_a(*blocks_):
    return {"net": "A", "pins": [{"block": b, "port": "P"} for b in blocks_]}


def test_intermediate_pad_sharing_an_edge_with_another_nets_block_wire(tmp_path):
    result, extracted = _compose_cell_strips(
        tmp_path,
        {
            "a1": (MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 0, (-1.0, -0.07)),
            "a2": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (4.0, -0.07)),
            "c": (LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0, (4.31, -0.365)),
            "d": (LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0, (9.31, -0.365)),
        },
        [_net_a("a1", "a2"), _net_b_on_metal3()],
    )
    assert result["unrouted_nets"] == ["B"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["A"]["routed"]
    reason = statuses["B"]["legs"][0]["reason"]
    assert "block 'a2'" in reason
    assert "(68, 20)" in reason
    assert "net 'B'" in reason
    assert _climbable(reason)  # a higher plane may still lift this leg
    assert not extracted["merged_net_labels"]


def test_intermediate_pad_with_a_positive_gap_to_the_block_still_routes(tmp_path):
    result, extracted = _compose_cell_strips(
        tmp_path,
        {
            "a1": (MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 0, (-1.0, -0.07)),
            "a2": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (4.0, -0.07)),
            "c": (LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0, (4.31, -0.5)),
            "d": (LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0, (9.31, -0.5)),
        },
        [_net_a("a1", "a2"), _net_b_on_metal3()],
    )
    assert not result["unrouted_nets"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["A"]["routed"] and statuses["B"]["routed"]
    assert not extracted["merged_net_labels"]


def test_far_pad_touching_another_nets_block_wire_at_a_corner_only(tmp_path):
    result, extracted = _compose_cell_strips(
        tmp_path,
        {
            "a1": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (6.02, -0.645)),
            "a2": (MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 0, (4.52, -0.645)),
            "c": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (4.31, -0.365)),
            "d": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (9.31, -0.365)),
        },
        [_net_a("a1", "a2"), _net_b_on_metal3()],
    )
    assert result["unrouted_nets"] == ["B"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["A"]["routed"]
    reason = statuses["B"]["legs"][0]["reason"]
    assert "block 'a2'" in reason
    assert "net 'B'" in reason
    assert not extracted["merged_net_labels"]


def test_far_pad_with_a_positive_gap_merges_with_own_port_wire_only(tmp_path):
    result, extracted = _compose_cell_strips(
        tmp_path,
        {
            "a1": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (5.60, -0.70)),
            "a2": (MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 0, (4.10, -0.70)),
            "c": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (4.31, -0.365)),
            "d": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (9.31, -0.365)),
        },
        [_net_a("a1", "a2"), _net_b_on_metal3()],
    )
    assert not result["unrouted_nets"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["A"]["routed"] and statuses["B"]["routed"]
    assert not extracted["merged_net_labels"]


@pytest.mark.parametrize("reverse", [False, True], ids=["SIG-first", "VDD-first"])
def test_fallback_landing_does_not_connect_distinct_nets(tmp_path, pdk_root, reverse):
    report = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "arr",
        w_um=2,
        l_um=0.3,
        rows=2,
        cols=3,
        dummy=0,
        flavor="pfet",
        topology="common_centroid",
        gate_contact=True,
    )
    nets = [
        {"net": name, "pins": [{"block": "arr", "port": port} for port in terminals]}
        for name, terminals in [("SIG", ("U3_D", "U4_G")), ("VDD", ("U1_S", "U3_S"))]
    ]
    output = tmp_path / "top.gds"
    result = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": report}],
            "placement": {
                "strategy": "explicit",
                "order": ["arr"],
                "origins_um": {"arr": {"x": 0, "y": 0}},
            },
            "connectivity": list(reversed(nets)) if reverse else nets,
            "routing": {
                "layer_role": "metal2",
                "width_um": 0.3,
                "cross_block_layer_role": "metal3",
            },
            "options": {"cell_name": "top", "output": str(output)},
        },
        request_dir=str(tmp_path),
    )
    extracted = run_extract(str(output), "sky130", top="top")
    assert not extracted["merged_net_labels"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["SIG"]["routed"]
    if reverse:
        assert statuses["VDD"]["routed"]
    if statuses["VDD"]["routed"]:
        names = {net["name"] for net in extracted["nets"]}
        assert {"SIG", "VDD"} <= names
        assert not result["unrouted_nets"]
    else:
        assert result["unrouted_nets"] == ["VDD"]
        leg = statuses["VDD"]["legs"][0]
        assert leg["blocking_net"] == "SIG"
        reason = leg["reason"]
        assert "cross-block fallback shares a conductive edge" in reason
        assert "already-routed net 'SIG'" in reason


def test_automatic_climb_recovers_from_a_shared_edge_on_the_configured_fallback():
    from test_gen_compose_plane_climb import (
        LI1,
        MET1,
        _climb_bundle,
        _geometry_for_layer,
        _same_facing_row_fixture,
    )

    fixture = _same_facing_row_fixture()
    fixture[0]["b"]["ports"]["A"].update(direction_deg=180, x_um=24.0)
    empty = _geometry_for_layer(fixture[2], ())
    attempted = set()

    def conflict(points, drops, stub, layer, **kwargs):
        attempted.add(layer)
        if layer in (LI1, MET1):
            return "shares a conductive edge with already-routed net 'READ'"
        return None

    result = _climb_bundle(
        fixture,
        cross_block_route_layer=MET1,
        block_geometry_for=empty(LI1),
        cross_block_geometry_for=empty(MET1),
        geometry_for_layer=empty,
        leg_conflict=conflict,
        width_aware_conflict=conflict,
    )
    assert {LI1, MET1, (69, 20)} <= attempted
    assert result["routed"] and result["legs"][0]["route_layer"] == (69, 20)
    assert result["legs"][0].get("blocking_net") is None
