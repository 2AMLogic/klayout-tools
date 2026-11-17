"""Ownership attribution of ``"ongrid"`` markers (issue #6447).

``Region.grid_check`` runs on the *merged* region, so a top-level shape whose
off-grid edge overlaps an on-grid child puts a T-vertex on the child's
boundary. Bbox containment filed that marker under the child -- a block that
owed nothing, and one a ``--waivers`` entry for the child would then silence.
A native physical-layer ``"ongrid"`` marker is now attributed to the placement
whose own source edges supply the offending coordinate(s) at that point, and
to no one when that evidence is absent, conflicting or unprovable.

A separate file rather than more of ``tests/test_drc.py``, per this repo's
extract-rather-than-grow convention for already-large files (the per-file size
ratchet, ``file-size-baseline.json``).
"""

from __future__ import annotations

import json

import klayout.db as kdb

from klayout_tools.drc import run_drc

RULE = "metal5.ongrid.1"  # gf180mcu: Metal5 (81/0), 0.005um grid == 5 dbu at 1nm
M5 = (81, 0)
M4 = (46, 0)


def _layout():
    layout = kdb.Layout()
    layout.dbu = 0.001
    return layout, layout.create_cell("TOP")


def _child(layout, name, *boxes, layer=M5):
    cell = layout.create_cell(name)
    for box in boxes:
        cell.shapes(layout.layer(*layer)).insert(box)
    return cell


def _place(parent, child, trans=None):
    parent.insert(kdb.CellInstArray(child.cell_index(), trans or kdb.Trans(0, 0)))


def _report(tmp_path, layout, waivers=None, **kwargs):
    path = tmp_path / "attr.gds"
    layout.write(str(path))
    return run_drc(str(path), "gf180mcu", waivers_file=waivers, **kwargs)


def _markers(report):
    """``{(x, y): (source_cell, source_path)}`` for every active ongrid marker,
    asserting the two ownership fields always move together."""
    found = {}
    for violation in report["violations"]:
        if violation["rule"] != RULE:
            continue
        cell, path = violation["source_cell"], violation["source_path"]
        assert (cell is None) == (path is None)
        if path is not None:
            assert isinstance(cell, str) and cell == path[-1]
            assert all(isinstance(name, str) for name in path)
        bbox = violation["bbox"]
        assert bbox["left"] == bbox["right"] and bbox["bottom"] == bbox["top"]
        found[(bbox["left"], bbox["bottom"])] = (cell, path)
    return found


def _all_owned_by(markers, owner):
    """Every marker carries exactly ``owner`` (and there is at least one)."""
    return bool(markers) and all(value == owner for value in markers.values())


def _overlap_layout():
    """The issue's reproduction: an on-grid child, and a top-level box whose
    off-grid y edges cross the child's right edge."""
    layout, top = _layout()
    child = _child(layout, "CHILD_CLEAN", kdb.Box(0, 0, 10000, 200000))
    _place(top, child)
    top.shapes(layout.layer(*M5)).insert(kdb.Box(5000, 66333, 15000, 166333))
    return layout


def test_top_level_edge_t_vertices_are_not_filed_under_the_clean_child(tmp_path):
    report = _report(tmp_path, _overlap_layout())

    assert _markers(report) == {
        (10000, 66333): (None, None),
        (10000, 166333): (None, None),
        (15000, 66333): (None, None),
        (15000, 166333): (None, None),
    }
    assert report["rule_counts"][RULE] == 4


def test_clean_child_waiver_leaves_every_marker_active(tmp_path):
    waivers = tmp_path / "waivers.json"
    waivers.write_text(
        json.dumps(
            {"waivers": [{"cell": "CHILD_CLEAN", "rule": RULE, "justification": "x"}]}
        )
    )

    report = _report(tmp_path, _overlap_layout(), waivers=str(waivers))

    assert report["status"] == "violations"
    assert len(_markers(report)) == 4
    assert report["waived_count"] == 0
    assert [w["cell"] for w in report["unused_waivers"]] == ["CHILD_CLEAN"]


def test_offending_rectangle_in_child_keeps_corner_and_t_vertex_attribution(tmp_path):
    """Move the off-grid rectangle into the child: its own corners, and the
    T-vertices where *its* edge crosses an on-grid top-level box, are its."""
    layout, top = _layout()
    _place(top, _child(layout, "CHILD_OFF", kdb.Box(5000, 66333, 15000, 166333)))
    top.shapes(layout.layer(*M5)).insert(kdb.Box(10000, 0, 20000, 200000))

    markers = _markers(_report(tmp_path, layout))

    owned = ("CHILD_OFF", ["CHILD_OFF"])
    assert markers == {
        (5000, 66333): owned,  # the child's own corners
        (5000, 166333): owned,
        (10000, 66333): owned,  # T-vertices on the top box's x=10000 edge
        (10000, 166333): owned,
    }


def test_both_axes_off_grid_corner_is_attributed_when_one_origin_supplies_both(
    tmp_path,
):
    layout, top = _layout()
    _place(top, _child(layout, "CHILD_OFF", kdb.Box(5003, 66333, 15000, 166333)))

    assert _markers(_report(tmp_path, layout))[(5003, 66333)] == (
        "CHILD_OFF",
        ["CHILD_OFF"],
    )


def test_both_axes_off_grid_marker_split_across_origins_is_unattributed(tmp_path):
    """x=7003 comes from a top-level box, y=66333 from the child: neither owns
    the vertex alone, and the child's bbox containing it is not evidence."""
    layout, top = _layout()
    _place(top, _child(layout, "CHILD_Y", kdb.Box(0, 66333, 20000, 166333)))
    top.shapes(layout.layer(*M5)).insert(kdb.Box(7003, 0, 9000, 200000))

    markers = _markers(_report(tmp_path, layout))

    assert markers[(7003, 66333)] == (None, None)
    assert markers[(7003, 166333)] == (None, None)


def test_off_grid_translation_is_evaluated_in_checked_cell_coordinates(tmp_path):
    """The child's own geometry is on-grid; its 3 dbu placement offset is what
    puts every edge off-grid -- in the checked cell, the child supplies them."""
    layout, top = _layout()
    child = _child(layout, "CHILD_ON", kdb.Box(0, 0, 10000, 20000))
    _place(top, child, kdb.Trans(0, 3))

    markers = _markers(_report(tmp_path, layout))

    owned = ("CHILD_ON", ["CHILD_ON"])
    assert markers == {
        (0, 3): owned,
        (10000, 3): owned,
        (0, 20003): owned,
        (10000, 20003): owned,
    }


def test_nested_placement_reports_the_full_instance_path(tmp_path):
    layout, top = _layout()
    leaf = _child(layout, "LEAF", kdb.Box(0, 0, 10000, 20003))
    mid = layout.create_cell("MID")
    _place(mid, leaf, kdb.Trans(0, 5000))
    _place(top, mid, kdb.Trans(20000, 0))

    markers = _markers(_report(tmp_path, layout))

    assert _all_owned_by(markers, ("LEAF", ["MID", "LEAF"]))
    assert (30000, 25003) in markers


def test_edges_from_two_cells_through_one_marker_are_ambiguous(tmp_path):
    """CHILD_A and CHILD_B both draw an edge at y=3 through the shared corner
    (0, 3). Bbox containment picked one; neither is provably the owner."""
    layout, top = _layout()
    _place(top, _child(layout, "CHILD_A", kdb.Box(0, 3, 10000, 20003)))
    _place(top, _child(layout, "CHILD_B", kdb.Box(0, 3, 8000, 30003)))

    markers = _markers(_report(tmp_path, layout))

    assert markers[(0, 3)] == (None, None)
    assert markers[(10000, 3)] == ("CHILD_A", ["CHILD_A"])
    assert markers[(8000, 30003)] == ("CHILD_B", ["CHILD_B"])


def test_one_definition_in_two_overlapping_placements_is_ambiguous(tmp_path):
    """Placements are told apart by transform, not by name: the same square
    placed r0 and r90 lands on identical geometry, so both own every corner."""
    layout, top = _layout()
    square = _child(layout, "SQUARE", kdb.Box(0, 0, 10000, 10000))
    _place(top, square, kdb.Trans(0, 3))
    _place(top, square, kdb.Trans(1, False, 10000, 3))

    markers = _markers(_report(tmp_path, layout))

    assert set(markers) == {(0, 3), (10000, 3), (0, 10003), (10000, 10003)}
    assert _all_owned_by(markers, (None, None))


def test_repeated_identical_placement_is_one_origin(tmp_path):
    layout, top = _layout()
    square = _child(layout, "SQUARE", kdb.Box(0, 0, 10000, 10000))
    _place(top, square, kdb.Trans(0, 3))
    _place(top, square, kdb.Trans(0, 3))

    markers = _markers(_report(tmp_path, layout))

    assert _all_owned_by(markers, ("SQUARE", ["SQUARE"]))


def test_geometry_on_another_layer_is_not_evidence(tmp_path):
    """A Metal4 shape with an edge at the same y through the marker is not a
    Metal5 source edge, so it neither owns nor muddies the Metal5 marker."""
    layout, top = _layout()
    _place(top, _child(layout, "CHILD_OFF", kdb.Box(5000, 66333, 15000, 166333)))
    _place(top, _child(layout, "OTHER", kdb.Box(0, 66333, 12000, 90000), layer=M4))
    top.shapes(layout.layer(*M5)).insert(kdb.Box(10000, 0, 20000, 200000))

    markers = _markers(_report(tmp_path, layout))

    assert markers[(10000, 66333)] == ("CHILD_OFF", ["CHILD_OFF"])


def test_diagonal_edge_through_the_marker_is_unprovable(tmp_path):
    """Diagonal attribution is out of scope: a child whose triangle puts a
    slanted edge through the marker is unattributed, never bbox-attributed."""
    layout, top = _layout()
    tri = kdb.Polygon([kdb.Point(0, 0), kdb.Point(10003, 0), kdb.Point(0, 10000)])
    _place(top, _child(layout, "TRI", tri))

    markers = _markers(_report(tmp_path, layout))

    assert markers[(10003, 0)] == (None, None)


def test_top_level_marker_in_a_gap_of_a_child_bbox_stays_unattributed(tmp_path):
    """The child draws two squares whose bbox spans the gap between them; a
    top-level box in that gap has no source edge from the child, so its markers
    are null rather than the child that merely contains the point."""
    layout, top = _layout()
    far_corners = (kdb.Box(0, 0, 10000, 10000), kdb.Box(90000, 90000, 100000, 100000))
    _place(top, _child(layout, "CHILD_CLEAN", *far_corners))
    top.shapes(layout.layer(*M5)).insert(kdb.Box(40000, 40003, 50000, 50003))

    markers = _markers(_report(tmp_path, layout))

    assert len(markers) == 4
    assert _all_owned_by(markers, (None, None))


def test_tile_run_keeps_attribution_of_the_centre_instance(tmp_path):
    """``--tile`` wraps the cell in an array; its first hop is dropped again,
    so a centre-instance marker reads ``["CHILD_OFF"]`` as in an untiled run."""
    layout, top = _layout()
    pr = layout.layer(235, 4)
    top.shapes(pr).insert(kdb.Box(0, 0, 100000, 100000))
    _place(top, _child(layout, "CHILD_OFF", kdb.Box(20000, 20003, 30000, 40000)))

    tiled = _markers(_report(tmp_path, layout, tile="3x3"))

    assert _all_owned_by(tiled, ("CHILD_OFF", ["CHILD_OFF"]))
    assert set(tiled) == set(_markers(_report(tmp_path, layout)))
