"""Per-instance attribution tests for ``klt drc``'s ``klayout`` engine
(issue #5661) -- split from test_drc_klayout_engine.py for the file-size
ratchet, mirroring tests/test_drc_klayout_abort.py's split."""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

import klayout_tools.drc_klayout_engine as engine_module
from klayout_tools.drc import run_drc_klayout_engine
from test_drc_klayout_engine import _stub_klayout_drc_subprocess, _write_deck_file

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# --------------------------------------------------------------------------- #
# Issue #5661: per-instance attribution (`source_cell` / `source_path`)
# --------------------------------------------------------------------------- #


def _write_hierarchical_gds(path: Path) -> str:
    """TOP places CHILD (2 um square) twice -- at x=10 um and x=20 um -- and
    MID once at x=30 um; MID itself places CHILD at its origin. Coordinates
    are integer dbu at dbu=0.001, so 10000 dbu == 10 um."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    mid = layout.create_cell("MID")
    child.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 2000, 2000))
    mid.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans()))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(10000, 0)))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(20000, 0)))
    top.insert(kdb.CellInstArray(mid.cell_index(), kdb.Trans(30000, 0)))
    layout.write(str(path))
    return str(path)


def _rdb_with_boxes(items: list[tuple[str, tuple[float, float, float, float]]]) -> str:
    """An RDB with one `W.1` edge-pair item per ``(cell, (x0, y0, x1, y1))``
    entry, coordinates in micrometres (the RDB's own unit)."""
    body = "".join(
        f"""  <item>
   <category>'W.1'</category>
   <cell>{cell}</cell>
   <values>
    <value>edge-pair: ({x0},{y0};{x0},{y1})|({x1},{y1};{x1},{y0})</value>
   </values>
  </item>
"""
        for cell, (x0, y0, x1, y1) in items
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category>
   <name>W.1</name>
   <description>min width</description>
  </category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
{body} </items>
</report-database>
"""


def _by_left(report: dict) -> dict:
    return {v["bbox"]["left"]: v for v in report["violations"]}


def test_klayout_engine_attributes_markers_to_containing_instance(
    tmp_path, monkeypatch
):
    rdb = _rdb_with_boxes(
        [
            ("TOP", (10.5, 0.5, 10.6, 1.0)),  # inside the first CHILD placement
            ("TOP", (20.5, 0.5, 20.6, 1.0)),  # inside the second placement
            ("TOP", (30.5, 0.5, 30.6, 1.0)),  # MID -> CHILD, nested
            ("TOP", (9.5, 0.5, 10.5, 1.0)),  # straddles the CHILD boundary
            ("TOP", (0.1, 0.1, 0.3, 0.9)),  # top-level geometry, no instance
        ]
    )
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=rdb)
    gds = _write_hierarchical_gds(tmp_path / "hier.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    by_left = _by_left(report)
    assert len(by_left) == 5
    for left in (10500, 20500):
        assert by_left[left]["source_cell"] == "CHILD"
        assert by_left[left]["source_path"] == ["CHILD"]
    # Innermost instance wins, and the path runs from TOP's direct child down.
    assert by_left[30500]["source_cell"] == "CHILD"
    assert by_left[30500]["source_path"] == ["MID", "CHILD"]
    # Straddling and top-level markers have no single originating instance.
    for left in (9500, 100):
        assert by_left[left]["source_cell"] is None
        assert by_left[left]["source_path"] is None
    # The top-cell attribution is unchanged for every marker.
    assert {v["cell"] for v in report["violations"]} == {"TOP"}


def test_klayout_engine_marker_under_non_top_rdb_cell_stays_unattributed(
    tmp_path, monkeypatch
):
    # Same coordinates as an in-instance marker, but the deck reported them
    # under CHILD: they are in CHILD's frame, not TOP's, so the walk must not
    # claim an instance for them.
    rdb = _rdb_with_boxes([("CHILD", (10.5, 0.5, 10.6, 1.0))])
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=rdb)
    gds = _write_hierarchical_gds(tmp_path / "hier.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file)

    (violation,) = report["violations"]
    assert violation["cell"] == "CHILD"
    assert violation["source_cell"] is None
    assert violation["source_path"] is None


def test_klayout_engine_top_naming_child_attributes_within_that_subtree(
    tmp_path, monkeypatch
):
    # `--top MID`: the deck runs on MID's subtree, so MID is the frame the
    # RDB coordinates live in and CHILD (at MID's origin) is the instance.
    rdb = _rdb_with_boxes([("MID", (0.5, 0.5, 0.6, 1.0))]).replace(
        "<name>TOP</name>", "<name>MID</name>"
    )
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=rdb)
    gds = _write_hierarchical_gds(tmp_path / "hier.gds")
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(gds, deck_file, top="MID")

    (violation,) = report["violations"]
    assert violation["cell"] == "MID"
    assert violation["source_cell"] == "CHILD"
    assert violation["source_path"] == ["CHILD"]


def test_attribute_violations_to_instances_empty_list_is_a_noop():
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    violations: list = []

    engine_module._attribute_violations_to_instances(violations, top)

    assert violations == []


def test_klayout_engine_coordinate_less_marker_stays_unattributed(
    tmp_path, monkeypatch
):
    # An item whose <value> carries no coordinate pair gets the parser's
    # all-zero bbox -- a location-unknown sentinel, not a coordinate. A
    # CHILD sits exactly on the origin here, so without the sentinel guard
    # the marker would be misattributed to that placement.
    rdb = _rdb_with_boxes([]).replace(
        " <items>\n </items>",
        """ <items>
  <item>
   <category>'W.1'</category>
   <cell>TOP</cell>
   <values>
    <value>drc-marker without any coordinate payload</value>
   </values>
  </item>
 </items>""",
    )
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=rdb)
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    child.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 2000, 2000))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans()))
    gds = tmp_path / "origin-child.gds"
    layout.write(str(gds))
    deck_file = _write_deck_file(tmp_path / "deck.lydrc")

    report = run_drc_klayout_engine(str(gds), deck_file)

    (violation,) = report["violations"]
    assert violation["cell"] == "TOP"
    assert violation["bbox"] == {"left": 0, "bottom": 0, "right": 0, "top": 0}
    assert violation["source_cell"] is None
    assert violation["source_path"] is None
