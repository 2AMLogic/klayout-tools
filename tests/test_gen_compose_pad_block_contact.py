"""A landing pad must stay electrically separate from another net's block metal."""

from collections import Counter

import klayout.db as kdb

from klayout_tools import gen_compose
from klayout_tools.extract import run_extract
from klayout_tools.gen_compose import compose
from klayout_tools.gen_compose_plane_climb import _climbable
from test_gen_compose import _isolate as _isolate

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


def test_contact_check_reads_each_bystander_block_layer_once_per_request(
    tmp_path, monkeypatch
):
    """More nets mean more ``_leg_conflict`` calls, never more GDS reads of a
    block hosting no pin of any net.

    The contact check consults every block hosting no pin of the net being
    routed, for each pad of each candidate leg. It must go through the
    request's cached ``_own_block_layer_geometry`` so a bystander's drawn
    geometry on a layer is read once per ``(layer, scale)`` -- not once per
    pad, leg or net (issue #7325). Pin-hosting blocks are left out of the
    assertion: two older, separate readers also consult those.
    """
    reads: Counter = Counter()
    real = gen_compose.read_block_layer_geometry

    def counting(block_id, block, offset, layer, dbu=None):
        reads[(block_id, tuple(layer), dbu)] += 1
        return real(block_id, block, offset, layer, dbu)

    monkeypatch.setattr(gen_compose, "read_block_layer_geometry", counting)
    specs = {
        "a1": (MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 0, (-1.0, -0.07)),
        "a2": (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (4.0, -0.07)),
        "c": (LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0, (4.31, -0.5)),
        "d": (LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0, (9.31, -0.5)),
    }
    connectivity = [_net_a("a1", "a2"), _net_b_on_metal3()]
    for k in range(3):  # three more routed nets, each a fresh set of legs
        row = 10.0 * (k + 1)
        specs[f"p{k}a"] = (MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 0, (-1.0, row))
        specs[f"p{k}b"] = (MET1, (0, 0, 1.0, 0.14), (0.0, 0.07), 180, (4.0, row))
        connectivity.append(_net_a(f"p{k}a", f"p{k}b") | {"net": f"P{k}"})
    for i in range(6):  # bystanders: hosting no pin of any net
        specs[f"z{i}"] = (
            MET1,
            (0, 0, 1.0, 0.14),
            (0.0, 0.07),
            180,
            (20.0 + 3 * i, 30.0),
        )

    result, extracted = _compose_cell_strips(tmp_path, specs, connectivity)

    assert not result["unrouted_nets"]
    assert not extracted["merged_net_labels"]
    bystander_reads = {key: n for key, n in reads.items() if key[0].startswith("z")}
    assert bystander_reads, "the check never consulted a bystander block"
    assert {key: n for key, n in bystander_reads.items() if n > 1} == {}
