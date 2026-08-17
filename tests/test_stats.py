"""Tests for `klt stats` and the `stats_report` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests —
no dependency on an external corpus.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.stats import StatsError, stats_report


def _make_layout() -> kdb.Layout:
    """A layout with a known bounding box, area, and per-layer breakdown.

    - TOP instantiates SUB at the origin.
    - (1, 0) named "metal1": a 1000x1000 box in TOP + a 1000x1000 box in SUB
      -> area 2,000,000 dbu^2, polygon_count 2, vertex_count 8.
    - (2, 0) unnamed: one triangle (0,0)-(0,500)-(500,500) in TOP
      -> area 125,000 dbu^2, polygon_count 1, vertex_count 3.
    - Top cell bbox (hierarchy-inclusive): (0,0)-(3000,3000)
      -> bbox area 9,000,000 dbu^2.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans()))

    m1 = layout.layer(1, 0)
    layout.set_info(m1, kdb.LayerInfo(1, 0, "metal1"))
    top.shapes(m1).insert(kdb.Box(0, 0, 1000, 1000))
    sub.shapes(m1).insert(kdb.Box(2000, 2000, 3000, 3000))

    m2 = layout.layer(2, 0)
    top.shapes(m2).insert(
        kdb.Polygon([kdb.Point(0, 0), kdb.Point(0, 500), kdb.Point(500, 500)])
    )

    return layout


def test_stats_report_totals(tmp_path):
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    report = stats_report(str(path))

    assert report["file"] == str(path)
    assert report["dbu_um"] == 0.001
    assert report["top_cell"] == "TOP"
    assert report["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 3.0,
        "top": 3.0,
        "width": 3.0,
        "height": 3.0,
    }
    total = report["total"]
    assert total["area_um2"] == pytest.approx(2.125)
    assert total["density"] == pytest.approx(2_125_000 / 9_000_000)
    assert total["polygon_count"] == 3
    assert total["vertex_count"] == 11
    # --per-layer not requested -> layers is null.
    assert report["layers"] is None


def test_stats_report_per_layer(tmp_path):
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    report = stats_report(str(path), per_layer=True)

    layers = report["layers"]
    assert layers is not None
    # Sorted by (layer, datatype).
    assert [(entry["layer"], entry["datatype"]) for entry in layers] == [
        (1, 0),
        (2, 0),
    ]

    by_pair = {(entry["layer"], entry["datatype"]): entry for entry in layers}

    metal1 = by_pair[(1, 0)]
    assert metal1["name"] == "metal1"
    assert metal1["area_um2"] == pytest.approx(2.0)
    assert metal1["density"] == pytest.approx(2_000_000 / 9_000_000)
    assert metal1["polygon_count"] == 2
    assert metal1["vertex_count"] == 8

    layer2 = by_pair[(2, 0)]
    assert layer2["name"] is None
    assert layer2["area_um2"] == pytest.approx(0.125)
    assert layer2["density"] == pytest.approx(125_000 / 9_000_000)
    assert layer2["polygon_count"] == 1
    assert layer2["vertex_count"] == 3
    assert layer2["annotation"] is False
    assert metal1["annotation"] is False


def test_stats_report_gds_round_trip(tmp_path):
    """GDSII drops layer names but preserves geometry, so totals match."""
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    report = stats_report(str(path), per_layer=True)

    assert report["total"]["area_um2"] == pytest.approx(2.125)
    assert report["total"]["polygon_count"] == 3
    assert report["total"]["vertex_count"] == 11
    assert all(entry["name"] is None for entry in report["layers"])


def test_stats_report_deterministic(tmp_path):
    """Repeated calls on the same input produce byte-identical JSON."""
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    first = json.dumps(stats_report(str(path), per_layer=True), sort_keys=True)
    second = json.dumps(stats_report(str(path), per_layer=True), sort_keys=True)
    assert first == second


def test_stats_report_empty_layout(tmp_path):
    """A layout with no cells reports null top_cell and an all-zero bbox."""
    path = tmp_path / "empty.gds"
    kdb.Layout().write(str(path))

    report = stats_report(str(path))

    assert report["top_cell"] is None
    assert report["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 0.0,
        "top": 0.0,
        "width": 0.0,
        "height": 0.0,
    }
    assert report["total"] == {
        "area_um2": 0.0,
        "density": 0.0,
        "polygon_count": 0,
        "vertex_count": 0,
    }


def test_stats_report_multiple_top_cells_raises(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("A")
    layout.create_cell("B")
    path = tmp_path / "multi_top.gds"
    layout.write(str(path))

    with pytest.raises(StatsError, match="multiple top cells"):
        stats_report(str(path))


# --- --top (issue #554) ------------------------------------------------------


def _make_multi_top_layout() -> kdb.Layout:
    """A multi-top-cell stream where per-top-cell scoping is observable.

    - ``BLOCK_A`` draws a 1x1 um box directly and instantiates
      ``BLOCK_A_CHILD`` (another 1x1 um box) -- hierarchy total area 2 um^2.
    - ``BLOCK_B`` (an unrelated, standalone top cell) draws a single 5x5 um
      box -- area 25 um^2.

    A naive ``--top`` fix that only picks the bbox but keeps summing shapes
    across ``Layout.each_cell()`` would report the same (wrong) 27 um^2 total
    for both ``--top BLOCK_A`` and ``--top BLOCK_B``.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    li = layout.layer(1, 0)

    a = layout.create_cell("BLOCK_A")
    a_child = layout.create_cell("BLOCK_A_CHILD")
    a.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    a_child.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    a.insert(kdb.CellInstArray(a_child.cell_index(), kdb.Trans(2000, 2000)))

    b = layout.create_cell("BLOCK_B")
    b.shapes(li).insert(kdb.Box(0, 0, 5000, 5000))

    return layout


def test_top_scopes_area_to_named_cell_hierarchy(tmp_path):
    """``--top`` restricts area/polygon/vertex counts to that cell's own
    hierarchy, not the whole stream (issue #554) -- the risk a naive fix
    (scoping only the bbox) would miss."""
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    report_a = stats_report(str(path), top="BLOCK_A")
    assert report_a["top_cell"] == "BLOCK_A"
    assert report_a["total"]["area_um2"] == pytest.approx(2.0)
    assert report_a["total"]["polygon_count"] == 2

    report_b = stats_report(str(path), top="BLOCK_B")
    assert report_b["top_cell"] == "BLOCK_B"
    assert report_b["total"]["area_um2"] == pytest.approx(25.0)
    assert report_b["total"]["polygon_count"] == 1

    # Neither scoped total equals the whole-library sum (27 um^2, 3 polygons)
    # -- proving the counts actually re-scope, not just the bbox/top_cell name.
    assert report_a["total"]["area_um2"] < 27.0
    assert report_b["total"]["area_um2"] < 27.0


def _make_array_instanced_layout() -> kdb.Layout:
    """A leaf cell placed via one large ``CellInstArray``, plus a strap drawn
    directly in ``TOP`` -- the "array instance plus a couple of straps"
    shape issue #1105 describes.

    - ``LEAF`` draws a single 1x1 um box on layer (1, 0) -- area 1 um^2.
    - ``TOP`` instantiates ``LEAF`` via one ``CellInstArray`` with
      ``na=10, nb=12`` (120 copies) on a 2x2 um pitch, plus one 19x1 um
      strap box drawn directly on the same layer.
    - Hierarchy-inclusive bbox: (0,0)-(19000,23000) dbu = 19x23 um ->
      437 um^2.
    - Correct (instance-weighted) total area: 120 * 1 um^2 (every array
      copy) + 19 um^2 (the strap) = 139 um^2 -> density 139/437.
    - The pre-#1105 bug summed each cell *definition* once regardless of
      instance count: 1 um^2 (LEAF counted once, not x120) + 19 um^2 =
      20 um^2 -> a density undercounting the real occupancy by ~7x.
    """
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    layout.set_info(li, kdb.LayerInfo(1, 0, "metal1"))

    leaf = layout.create_cell("LEAF")
    leaf.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))

    top = layout.create_cell("TOP")
    na, nb = 10, 12
    top.insert(
        kdb.CellInstArray(
            leaf.cell_index(),
            kdb.Trans(),
            kdb.Vector(2000, 0),
            kdb.Vector(0, 2000),
            na,
            nb,
        )
    )
    top.shapes(li).insert(kdb.Box(0, 11000, 19000, 12000))

    return layout


def test_array_instanced_leaf_area_multiplies_by_instance_count(tmp_path):
    """A leaf cell instanced via a single N-copy ``CellInstArray`` (N=120
    here) contributes N times its own drawn area to the density numerator,
    not once per cell definition (issue #1105) -- the bbox denominator is
    already hierarchy-inclusive (spans every array position), so summing
    the numerator per-definition instead produced a near-zero density for
    array-instanced macros.
    """
    path = tmp_path / "array_instanced.gds"
    _make_array_instanced_layout().write(str(path))

    report = stats_report(str(path))

    assert report["top_cell"] == "TOP"
    assert report["bbox_um"]["width"] == pytest.approx(19.0)
    assert report["bbox_um"]["height"] == pytest.approx(23.0)

    bbox_area_um2 = 19.0 * 23.0
    na, nb = 10, 12
    leaf_area_um2 = 1.0
    strap_area_um2 = 19.0
    expected_total_area_um2 = na * nb * leaf_area_um2 + strap_area_um2
    expected_density = expected_total_area_um2 / bbox_area_um2

    assert report["total"]["area_um2"] == pytest.approx(expected_total_area_um2)
    assert report["total"]["density"] == pytest.approx(expected_density)

    # The pre-#1105 bug summed each cell definition once (LEAF's own 1 um^2
    # plus the strap's 19 um^2 = 20 um^2), regardless of instance count --
    # confirm the fixed total is not that undercounted figure.
    buggy_total_area_um2 = leaf_area_um2 + strap_area_um2
    assert report["total"]["area_um2"] != pytest.approx(buggy_total_area_um2)

    # polygon_count/vertex_count stay per cell definition (issue #1105 is
    # scoped to the area/density numerator, not shape counts) -- one box in
    # LEAF's definition plus one strap box drawn directly in TOP, not
    # multiplied by the 120 array copies.
    assert report["total"]["polygon_count"] == 2
    assert report["total"]["vertex_count"] == 8


def test_top_unknown_cell_raises(tmp_path):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    with pytest.raises(StatsError, match="top cell not found in stream: NOPE"):
        stats_report(str(path), top="NOPE")


def test_top_on_single_top_cell_stream_matches_omitting_it(tmp_path):
    """A single-top-cell stream with ``--top`` naming that cell reports
    identically to omitting ``--top`` (issue #554 edge case)."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    with_top = stats_report(str(path), per_layer=True, top="TOP")
    without_top = stats_report(str(path), per_layer=True)
    assert with_top == without_top


def test_cli_top_flag_scopes_multi_top_report(tmp_path, capsys):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    assert main(["stats", str(path), "--top", "BLOCK_A", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["top_cell"] == "BLOCK_A"
    assert data["total"]["area_um2"] == pytest.approx(2.0)


def test_cli_missing_top_on_multi_top_stream_exits_one(tmp_path, capsys):
    """Without ``--top``, a multi-top-cell stream still exits 1 with a clean
    error (not a stack trace) -- unchanged default behaviour."""
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    assert main(["stats", str(path)]) == 1
    err = capsys.readouterr().err
    assert "multiple top cells" in err


def test_json_contract(tmp_path, capsys):
    """`--format json` emits exactly the documented schema."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    assert main(["stats", str(path), "--per-layer", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "dbu_um",
        "top_cell",
        "bbox_um",
        "total",
        "layers",
    }
    assert data["schema_version"] == 1
    assert isinstance(data["file"], str)
    assert isinstance(data["dbu_um"], float)
    assert isinstance(data["top_cell"], str)
    assert set(data["bbox_um"].keys()) == {
        "left",
        "bottom",
        "right",
        "top",
        "width",
        "height",
    }
    assert set(data["total"].keys()) == {
        "area_um2",
        "density",
        "polygon_count",
        "vertex_count",
    }
    assert isinstance(data["layers"], list)
    for entry in data["layers"]:
        assert set(entry.keys()) == {
            "layer",
            "datatype",
            "name",
            "area_um2",
            "density",
            "polygon_count",
            "vertex_count",
            "annotation",
        }
        assert isinstance(entry["annotation"], bool)

    # Sorted ascending by (layer, datatype).
    pairs = [(e["layer"], e["datatype"]) for e in data["layers"]]
    assert pairs == sorted(pairs)


def test_json_without_per_layer_has_null_layers(tmp_path, capsys):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    assert main(["stats", str(path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["layers"] is None


def test_default_format_is_text(tmp_path, capsys):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    assert main(["stats", str(path), "--per-layer"]) == 0
    out = capsys.readouterr().out
    assert "file:" in out
    assert "area_um2:" in out and "density:" in out
    assert "layer" in out and "polygons" in out and "vertices" in out
    assert "annotation" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_stats_report_raises_on_missing():
    with pytest.raises(StatsError):
        stats_report("/no/such/path/design.gds")


@pytest.mark.parametrize(
    ("layer", "datatype", "expected"),
    [
        (989, 0, False),  # just below the reserved range
        (990, 0, True),  # lower bound
        (994, 0, True),  # documented canonical pair
        (994, 5, True),  # any datatype qualifies
        (999, 44, True),  # upper bound, non-zero datatype
        (1000, 0, False),  # just above the reserved range
    ],
)
def test_stats_report_annotation_range_boundaries(tmp_path, layer, datatype, expected):
    """(layer, datatype) pairs are named `annotation: true` iff the layer
    number falls in the reserved 990-999 range, regardless of datatype."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    li = layout.layer(layer, datatype)
    top.shapes(li).insert(kdb.Box(0, 0, 1, 1))
    path = tmp_path / "design.oas"
    layout.write(str(path))

    report = stats_report(str(path), per_layer=True)
    entry = next(
        e for e in report["layers"] if (e["layer"], e["datatype"]) == (layer, datatype)
    )
    assert entry["annotation"] is expected
