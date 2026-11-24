"""Tests for per-gap drawn coverage in ``klt economy --attribute``
(issue #6941): decomposition of each gap component's contents into
connected components of top-cell-only drawn geometry.

Split out of ``test_economy.py`` for the file-size ratchet; fixtures are
generated programmatically with ``klayout.db`` (same convention), with
hand-computed expectations in each fixture's docstring.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.economy import economy_report

# --- fixtures --------------------------------------------------------------


def _make_ring_and_strap_layout() -> kdb.Layout:
    """The issue #6941 reproduction (dbu = 0.001 um).

    On layer 1/0 the top cell draws a 20 x 20 um square ring with 2 um
    walls from four edge-joined rectangles (144 um^2) and a strap from
    (6, 9) to (14, 11) (16 um^2); a 4 x 4 um child sits at (8, 8), so the
    strap's middle 8 um^2 lies over the child bbox.

    - top bbox (0, 0) - (20, 20) = 400 um^2; child bbox 16 + one 384 um^2
      margin gap partition it exactly.
    - drawn inside the gap: ring 144 + strap ends 4 + 4 = 152 um^2,
      leaving 232 um^2 empty; the ring component has 4 pieces, each strap
      end 1 (the one strap shape counts once per split component).
    - top_level_drawn_area_um2 = 160 um^2: the strap's over-child middle
      counts there but not inside the gap.
    """
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    top = layout.create_cell("TOP")
    top.shapes(li).insert(kdb.Box(0, 0, 20000, 2000))  # bottom wall
    top.shapes(li).insert(kdb.Box(0, 18000, 20000, 20000))  # top wall
    top.shapes(li).insert(kdb.Box(0, 2000, 2000, 18000))  # left wall
    top.shapes(li).insert(kdb.Box(18000, 2000, 20000, 18000))  # right wall
    top.shapes(li).insert(kdb.Box(6000, 9000, 14000, 11000))  # strap
    child = layout.create_cell("CHILD")
    child.shapes(li).insert(kdb.Box(0, 0, 4000, 4000))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(8000, 8000))))
    return layout


def _make_empty_gap_layout() -> kdb.Layout:
    """Two 1 x 1 um children filling the full height of a 4 x 1 um bbox:
    the single 2 um^2 gap between them carries no top-cell drawing."""
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    child.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(3000, 0))))
    return layout


def _make_overlap_layers_layout() -> kdb.Layout:
    """No children: the whole 3 x 1 um bbox is one margin gap. Two
    overlapping top-cell rectangles on different layers -- (0, 0)-(2, 1)
    on 1/0 and (1, 0)-(3, 1) on 2/0 -- merge to one physical footprint of
    3 um^2 (the 1 um^2 overlap counts once) with 2 contributing pieces."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 2000, 1000))
    top.shapes(layout.layer(2, 0)).insert(kdb.Box(1000, 0, 3000, 1000))
    return layout


def _make_annotation_layout() -> kdb.Layout:
    """A 6 x 1 um bbox: real drawing (0, 0)-(3, 1) on 1/0, a child at
    (4, 0)-(5, 1), and a reserved annotation shape (994/0) at
    (5, 0)-(6, 1) inside the gap. The annotation shape contributes neither
    area nor pieces, so gap coverage is exactly the 3 um^2 rectangle."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 3000, 1000))
    top.shapes(layout.layer(994, 0)).insert(kdb.Box(5000, 0, 6000, 1000))
    child = layout.create_cell("CHILD")
    child.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 1000))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(4000, 0))))
    return layout


def _make_wrapper_drawing_layout() -> kdb.Layout:
    """A wrapper COMP placing two 1 x 1 um leaves at (0, 0) and (2, 0) and
    drawing its own (1.1, 0)-(1.2, 0.1) strap between them. With
    ``--flatten-wrappers`` that strap lands inside the 1 um^2 gap between
    the leaves, but drawing is sourced from the top cell only, so the gap
    reports zero coverage and no components."""
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    a = layout.create_cell("A")
    a.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    b = layout.create_cell("B")
    b.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    comp = layout.create_cell("COMP")
    comp.insert(kdb.CellInstArray(a.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    comp.insert(kdb.CellInstArray(b.cell_index(), kdb.Trans(kdb.Vector(2000, 0))))
    comp.shapes(li).insert(kdb.Box(1100, 0, 1200, 100))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(comp.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    return layout


# --- library-level ---------------------------------------------------------


def test_ring_and_strap_components(tmp_path):
    path = tmp_path / "ring.gds"
    _make_ring_and_strap_layout().write(str(path))
    attr = economy_report(str(path), attribute=True)["attribution"]

    assert attr["sums_to_total"] is True
    assert attr["unattributed_um2"] == 0
    assert attr["top_level_drawn_area_um2"] == pytest.approx(160.0)

    assert len(attr["children"]) == 1
    assert len(attr["gap_components"]) == 1
    gap = attr["gap_components"][0]
    assert gap["area_um2"] == pytest.approx(384.0)
    assert gap["position"] == "margin:bottom+left+right+top"
    # Child bbox + gap partition the 400 um^2 bbox exactly.
    assert attr["children"][0]["bbox_area_um2"] + gap["area_um2"] == pytest.approx(
        400.0
    )

    assert gap["drawn_area_um2"] == pytest.approx(152.0)
    assert gap["area_um2"] - gap["drawn_area_um2"] == pytest.approx(232.0)

    comps = gap["drawn_components"]
    assert len(comps) == 3
    ring, left_end, right_end = comps
    assert ring["drawn_area_um2"] == pytest.approx(144.0)
    assert ring["piece_count"] == 4
    assert (ring["bbox_um"]["left"], ring["bbox_um"]["bottom"]) == (0.0, 0.0)
    assert (ring["bbox_um"]["right"], ring["bbox_um"]["top"]) == (20.0, 20.0)
    for end, left in ((left_end, 6.0), (right_end, 12.0)):
        assert end["drawn_area_um2"] == pytest.approx(4.0)
        assert end["piece_count"] == 1
        assert (end["bbox_um"]["left"], end["bbox_um"]["bottom"]) == (left, 9.0)
        assert (end["bbox_um"]["right"], end["bbox_um"]["top"]) == (left + 2.0, 11.0)


def test_empty_gap_reports_zero(tmp_path):
    path = tmp_path / "empty.gds"
    _make_empty_gap_layout().write(str(path))
    attr = economy_report(str(path), attribute=True)["attribution"]
    assert attr["top_level_drawn_area_um2"] == 0.0
    assert len(attr["gap_components"]) == 1
    gap = attr["gap_components"][0]
    assert gap["area_um2"] == pytest.approx(2.0)
    assert gap["drawn_area_um2"] == 0.0
    assert gap["drawn_components"] == []


def test_overlapping_layers_one_component_two_pieces(tmp_path):
    path = tmp_path / "overlap.gds"
    _make_overlap_layers_layout().write(str(path))
    attr = economy_report(str(path), attribute=True)["attribution"]
    assert attr["top_level_drawn_area_um2"] == pytest.approx(3.0)
    assert len(attr["gap_components"]) == 1
    gap = attr["gap_components"][0]
    assert gap["area_um2"] == pytest.approx(3.0)
    assert gap["drawn_area_um2"] == pytest.approx(3.0)
    comps = gap["drawn_components"]
    assert len(comps) == 1
    assert comps[0]["drawn_area_um2"] == pytest.approx(3.0)
    assert comps[0]["piece_count"] == 2
    assert (comps[0]["bbox_um"]["left"], comps[0]["bbox_um"]["top"]) == (0.0, 1.0)


def test_reserved_annotation_contributes_nothing(tmp_path):
    path = tmp_path / "ann.gds"
    _make_annotation_layout().write(str(path))
    report = economy_report(str(path), attribute=True)
    assert "994/0" in report["annotation_layers_excluded"]
    attr = report["attribution"]
    assert attr["top_level_drawn_area_um2"] == pytest.approx(3.0)
    gap = attr["gap_components"][0]
    assert gap["drawn_area_um2"] == pytest.approx(3.0)
    assert len(gap["drawn_components"]) == 1
    assert gap["drawn_components"][0]["piece_count"] == 1


def test_wrapper_drawing_not_sourced(tmp_path):
    path = tmp_path / "wrapper.gds"
    _make_wrapper_drawing_layout().write(str(path))
    attr = economy_report(str(path), attribute=True, flatten_wrappers=True)[
        "attribution"
    ]
    assert attr["sums_to_total"] is True
    assert attr["top_level_drawn_area_um2"] == 0.0
    gaps = attr["gap_components"]
    assert sum(g["area_um2"] for g in gaps) == pytest.approx(1.0)
    for gap in gaps:
        assert gap["drawn_area_um2"] == 0.0
        assert gap["drawn_components"] == []


def test_attribution_absent_by_default(tmp_path):
    path = tmp_path / "ring.gds"
    _make_ring_and_strap_layout().write(str(path))
    assert "attribution" not in economy_report(str(path))


# --- CLI -------------------------------------------------------------------


def test_cli_text_and_json_expose_components(tmp_path, capsys):
    path = tmp_path / "ring.gds"
    _make_ring_and_strap_layout().write(str(path))
    assert main(["economy", str(path), "--attribute", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    gap = payload["attribution"]["gap_components"][0]
    assert gap["drawn_area_um2"] == pytest.approx(152.0)
    assert [c["piece_count"] for c in gap["drawn_components"]] == [4, 1, 1]

    assert main(["economy", str(path), "--attribute", "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert "drawn_um2" in text
    assert "drawn_component:" in text
    # Coverage column on the gap row, then the three component detail lines.
    assert "384.000" in text
    assert "152.000" in text
    assert "(0, 0) - (20, 20)" in text
    assert "pieces=4" in text
    assert "pieces=1" in text


def test_equal_area_component_order_is_deterministic(tmp_path, capsys):
    path = tmp_path / "ring.gds"
    _make_ring_and_strap_layout().write(str(path))
    outs = []
    for fmt in ("json", "text"):
        for _ in range(2):
            assert main(["economy", str(path), "--attribute", "--format", fmt]) == 0
            outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1]
    assert outs[2] == outs[3]
    # The two equal-area strap ends keep a stable left-then-right order.
    payload = json.loads(outs[0])
    ends = [
        c
        for c in payload["attribution"]["gap_components"][0]["drawn_components"]
        if c["drawn_area_um2"] == pytest.approx(4.0)
    ]
    assert [e["bbox_um"]["left"] for e in ends] == [6.0, 12.0]
    assert outs[2].index("(6, 9) - (8, 11)") < outs[2].index("(12, 9) - (14, 11)")
