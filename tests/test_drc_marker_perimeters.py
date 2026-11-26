"""Public DRC marker outlines for edge-pair checks and GF180 tap geometry."""

import klayout.db as kdb
import pytest

import klayout_tools.drc as drc
from helpers.drc_marker_ring import assert_simple_quad, first_meeting_sides
from klayout_tools.decks import DrcRule

_ENCLOSURE_CORNERS = [[0, 54], [0, 1946], [20, 100], [20, 1900]]

#: One nondegenerate marker per edge-pair check kind: (rule kwargs, shapes as
#: (layer, box), the marker's bbox, its four corners). ``enclosed`` and
#: ``enclosing`` are the two same-direction kinds; the others are antiparallel
#: and must come through unchanged in shape.
_KIND_CASES = {
    "enclosing": (
        {"layer": (1, 0), "other_layer": (2, 0)},
        [(1, (0, 0, 1000, 2000)), (2, (20, 100, 980, 1900))],
        (0, 54, 20, 1946),
        _ENCLOSURE_CORNERS,
    ),
    "enclosed": (
        {"layer": (1, 0), "other_layer": (2, 0)},
        [(2, (0, 0, 1000, 2000)), (1, (20, 100, 980, 1900))],
        (0, 54, 20, 1946),
        _ENCLOSURE_CORNERS,
    ),
    "separation": (
        {"layer": (1, 0), "other_layer": (2, 0)},
        [(1, (0, 0, 500, 1000)), (2, (540, 100, 1000, 900))],
        (500, 70, 540, 930),
        [[500, 70], [500, 930], [540, 100], [540, 900]],
    ),
    "space": (
        {"layer": (1, 0)},
        [(1, (0, 0, 500, 1000)), (1, (540, 200, 1000, 1200))],
        (500, 170, 540, 1030),
        [[500, 170], [500, 1000], [540, 200], [540, 1030]],
    ),
    "width": (
        {"layer": (1, 0)},
        [(1, (0, 0, 30, 1000))],
        (0, 0, 30, 1000),
        [[0, 0], [0, 1000], [30, 0], [30, 1000]],
    ),
}


@pytest.mark.parametrize("check", sorted(_KIND_CASES))
def test_every_check_kind_polygon_is_a_simple_ring(check, tmp_path, monkeypatch):
    layer_kwargs, shapes, bbox, corners = _KIND_CASES[check]
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for layer, box in shapes:
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(*box))
    path = tmp_path / f"{check}.gds"
    layout.write(str(path))
    rule = DrcRule(
        id=f"test.{check}",
        description=f"synthetic 50 dbu {check}",
        check=check,
        threshold_dbu=50,
        **layer_kwargs,
    )

    monkeypatch.setattr(drc, "get_deck", lambda _: [rule])
    report = drc.run_drc(str(path), "sky130")

    markers = [
        v
        for v in report["violations"]
        if (v["bbox"]["left"], v["bbox"]["bottom"]) == bbox[:2]
        and (v["bbox"]["right"], v["bbox"]["top"]) == bbox[2:]
    ]
    assert len(markers) == 1, report["violations"]
    assert_simple_quad(markers[0]["polygon"], corners)


def test_gf180_nplus_enclosing_comp_markers_are_simple_rings(tmp_path, monkeypatch):
    """The issue #7979 reproducer, through the real GF180MCU deck: four
    ``nplus.enclosing.comp.1`` violations, each a simple ring over the same
    four integer corners as the bowtie it replaces."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(22, 0)).insert(kdb.Box(0, 0, 2000, 1000))  # Comp
    top.shapes(layout.layer(32, 0)).insert(kdb.Box(-110, -110, 2110, 1110))  # Nplus
    path = tmp_path / "tap.gds"
    layout.write(str(path))

    # Capture the deck's actual edge pairs before the report serializes them.
    # The enclosure metric chooses their extent. Serialization must preserve
    # both the bounds and the corners while putting the corners in ring order.
    expected = {}
    serialize = drc._edge_pair_hull_points

    def capture(pair):
        box = pair.bbox()
        expected[box.left, box.bottom, box.right, box.top] = [
            (point.x, point.y)
            for edge in (pair.first, pair.second)
            for point in (edge.p1, edge.p2)
        ]
        return serialize(pair)

    monkeypatch.setattr(drc, "_edge_pair_hull_points", capture)
    report = drc.run_drc(str(path), "gf180mcu", rules=["nplus.enclosing.comp.1"])

    assert len(expected) == 4
    assert report["rule_counts"] == {"nplus.enclosing.comp.1": 4}
    by_bbox = {
        (
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        ): v
        for v in report["violations"]
    }
    assert set(by_bbox) == set(expected)
    for key, corners in expected.items():
        assert_simple_quad(by_bbox[key]["polygon"], corners)


def test_chamfer_reports_triangular_marker_outlines(tmp_path, monkeypatch):
    """Pin the Euclidean measurement that produces two sloped edge pairs."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(22, 0)).insert(kdb.Box(0, 0, 2000, 1000))
    outline = [(-110, -110), (2000, -110), (2110, 0), (2110, 1110), (-110, 1110)]
    top.shapes(layout.layer(32, 0)).insert(
        kdb.Polygon([kdb.Point(*p) for p in outline])
    )
    path = tmp_path / "chamfer.gds"
    layout.write(str(path))

    rule = DrcRule(
        id="test.chamfer",
        description="160 dbu Euclidean enclosure on the chamfered implant",
        check="enclosed",
        layer=(22, 0),
        other_layer=(32, 0),
        threshold_dbu=160,
    )
    monkeypatch.setattr(drc, "get_deck", lambda _: [rule])
    report = drc.run_drc(str(path), "gf180mcu", rules=[rule.id])

    assert report["rule_counts"] == {rule.id: 6}
    triangles = [v for v in report["violations"] if len(v["polygon"]) == 3]
    assert len(triangles) == 2
    actual = {
        tuple(v["bbox"][k] for k in ("left", "bottom", "right", "top")): {
            tuple(p) for p in v["polygon"]
        }
        for v in triangles
    }
    assert actual == {
        (2000, -110, 2110, 116): {(2000, -110), (2000, 116), (2110, 0)},
        (1884, -110, 2110, 0): {(2000, -110), (1884, 0), (2110, 0)},
    }


def test_sloped_implant_markers_have_simple_perimeters(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(22, 0)).insert(kdb.Box(0, 0, 2000, 1000))
    outline = [(-100, -100), (1900, -75), (2100, 1100), (-100, 1100)]
    top.shapes(layout.layer(32, 0)).insert(
        kdb.Polygon([kdb.Point(*point) for point in outline])
    )
    path = tmp_path / "sloped-implant.gds"
    layout.write(str(path))

    report = drc.run_drc(str(path), "gf180mcu", rules=["nplus.enclosing.comp.1"])
    assert report["violations"]
    for marker in report["violations"]:
        points = marker["polygon"]
        assert points and first_meeting_sides(points) is None, marker
