"""Geometric counterexamples for the runset–curated PEX correspondence."""

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.pex_net_correspondence import (
    _interior_point,
    build_probes,
    correspondence,
)
from test_extract_body_nets_sg13cmos5l import _make_sg13cmos5l_inverter_layout


def _join(tmp_path, layout, *, join_first_metal=False, anonymous=False):
    """Serialize a runset DB, then probe the independent sg13cmos5l extraction."""
    cell = layout.top_cell()
    gds = str(tmp_path / "routing.gds")
    db = str(tmp_path / "routing.lvsdb")
    layout.write(gds)
    lvs = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(layout, cell, []))
    metal_layers = (8,) if join_first_metal else (8, 10)
    for layer in metal_layers:
        region = lvs.make_layer(layout.layer(layer, 0), f"conductor{layer}")
        if anonymous:
            region = region & region
            lvs.register(region, f"derived{layer}")
        lvs.connect(region)
        if join_first_metal:
            lvs.connect_global(region, "JOINED")
    lvs.extract_netlist()
    lvs.write(db)
    nets = [
        net.expanded_name().replace("$", "\\$")
        for net in lvs.netlist().circuit_by_name(cell.name).each_net()
    ]
    probes, coverage = build_probes(
        db, gds, top=cell.name, metals=[(8, 0), (10, 0)], dbu_um=layout.dbu
    )
    curated = run_extract(
        gds,
        "sg13cmos5l",
        top=cell.name,
        parasitics=True,
        probes=probes,
        no_netlist=True,
    )
    return correspondence(
        nets, curated["probes"] or [], curated["parasitics"]["nets"], coverage
    )


def _draw(layout, layer, shape, datatype=0):
    layout.top_cell().shapes(layout.layer(layer, datatype)).insert(shape)


@pytest.mark.parametrize("pad_count", [16, 17, 65])
def test_last_pad_exposes_a_split_at_and_above_old_caps(tmp_path, pad_count):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    for i in range(pad_count):
        x = i * 2000
        _draw(layout, 8, kdb.Box(x, 0, x + 500, 500))
        if i < pad_count - 1:
            _draw(layout, 19, kdb.Box(x + 100, 100, x + 300, 300))
    _draw(layout, 10, kdb.Box(0, 0, (pad_count - 2) * 2000 + 500, 500))
    _draw(layout, 8, kdb.Text("BUS", kdb.Trans(200, 200)), 2)
    _draw(layout, 8, kdb.Text("OTHER", kdb.Trans((pad_count - 1) * 2000 + 200, 200)), 2)
    corr = _join(tmp_path, layout, join_first_metal=True)
    assert not any(m["runset"] == "JOINED" for m in corr["matched"])
    (split,) = [d for d in corr["disagreements"] if d["kind"] == "split"]
    assert split["runset_net"] == "JOINED"
    assert split["curated_nets"] == ["BUS", "OTHER"]
    assert corr["probe_count"] == pad_count


def test_stacked_metals_without_a_via_keep_their_own_roles(tmp_path):
    layout = _make_sg13cmos5l_inverter_layout()
    _draw(layout, 10, kdb.Box(75, 350, 325, 650))
    _draw(layout, 10, kdb.Text("UPPER", kdb.Trans(200, 500)), 2)
    corr = _join(tmp_path, layout)
    assert {"VGND", "UPPER"} <= {m["curated"] for m in corr["matched"]}
    assert corr["disagreements"] == []

    # After stripping provenance, the upper pad fits either mask. It cannot
    # claim UPPER or establish VGND's exclusivity. Other pads still match.
    corr = _join(tmp_path, layout, anonymous=True)
    assert {m["curated"] for m in corr["matched"]} == {"A", "VPWR", "Y"}
    assert corr["disagreements"] == []
    assert any(
        e["candidate_roles"] == ["metal0", "metal1"]
        for e in corr["incomplete_geometry"]
    )


def test_anonymous_identical_metal_masks_are_disclosed_as_ambiguous(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    for layer, label in [(8, "LOWER"), (10, "UPPER")]:
        _draw(layout, layer, kdb.Box(0, 0, 500, 500))
        _draw(layout, layer, kdb.Text(label, kdb.Trans(200, 200)), 2)
    corr = _join(tmp_path, layout, anonymous=True)
    assert corr["matched"] == []
    assert corr["disagreements"] == []
    ambiguous = [
        e for e in corr["incomplete_geometry"] if e["reason"] == "ambiguous_layer"
    ]
    assert {e["runset_net"] for e in ambiguous} == set(corr["unmatched_runset_nets"])
    assert all(e["candidate_roles"] == ["metal0", "metal1"] for e in ambiguous)


def test_interior_probe_avoids_hull_and_hole_edges():
    elbow = kdb.Polygon(
        [
            kdb.Point(x, y)
            for x, y in [(0, 0), (0, 100), (50, 100), (50, 50), (100, 50), (100, 0)]
        ]
    )
    holed = kdb.Polygon(kdb.Box(0, 0, 100, 100))
    holed.insert_hole(kdb.Box(50, 25, 75, 75))
    for polygon in [elbow, holed]:
        point = _interior_point(polygon)
        assert point is not None and polygon.inside(point)
        assert not any(edge.contains(point) for edge in polygon.each_edge())
    assert _interior_point(kdb.Polygon(kdb.Box(0, 0, 1, 10))) is None


def test_polygon_without_an_interior_grid_point_withholds_the_partial_match(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    _draw(layout, 8, kdb.Box(0, 0, 500, 500))
    _draw(layout, 8, kdb.Text("BUS", kdb.Trans(200, 200)), 2)
    _draw(layout, 8, kdb.Box(2000, 0, 2001, 500))
    corr = _join(tmp_path, layout, join_first_metal=True)
    assert corr["probe_count"] == 1
    assert corr["matched"] == []
    assert corr["unmatched_runset_nets"] == ["JOINED"]
    (gap,) = corr["incomplete_geometry"]
    assert (gap["runset_net"], gap["reason"]) == ("JOINED", "no_interior_probe")


def test_probe_misses_and_uncertain_layer_hits_prevent_exclusive_matches():
    def hit(prefix, net, cluster):
        return {
            "name": f"{prefix}|metal0|{net}|1-0",
            "found": cluster is not None,
            "net": "BUS",
            "cluster_id": cluster,
        }

    corr = correspondence(
        ["partial", "confirmed", "ambiguous"],
        [
            hit("kltcorr", "partial", 1),
            hit("kltcorr", "partial", None),
            hit("kltcorr", "confirmed", 2),
            hit("kltmaybe", "ambiguous", 2),
        ],
        [],
        [{"runset_net": "ambiguous", "reason": "ambiguous_layer"}],
    )
    assert corr["matched"] == []
    assert corr["disagreements"] == []
    assert {"runset_net": "partial", "reason": "unresolved_probe"} in corr[
        "incomplete_geometry"
    ]
