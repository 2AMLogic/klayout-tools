"""Tests for `klt layers` and the `layers_report` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests —
no dependency on an external corpus.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.layers import LayersError, layers_report


def _make_layout() -> kdb.Layout:
    """A layout with known layers and shape counts.

    - (1, 0)   named "metal1", 2 shapes
    - (66, 20) unnamed,        3 shapes
    - (5, 0)   named "empty",  0 shapes (declared but empty)
    - (994, 0) unnamed,        1 shape (reserved annotation layer)
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans()))

    m1 = layout.layer(1, 0)
    layout.set_info(m1, kdb.LayerInfo(1, 0, "metal1"))
    top.shapes(m1).insert(kdb.Box(0, 0, 10, 10))
    sub.shapes(m1).insert(kdb.Box(20, 20, 30, 30))  # shape lives in a sub-cell

    m66 = layout.layer(66, 20)
    top.shapes(m66).insert(kdb.Box(0, 0, 5, 5))
    top.shapes(m66).insert(kdb.Box(6, 6, 9, 9))
    sub.shapes(m66).insert(kdb.Box(1, 1, 2, 2))

    empty = layout.layer(5, 0)
    layout.set_info(empty, kdb.LayerInfo(5, 0, "empty"))

    annotation = layout.layer(994, 0)
    top.shapes(annotation).insert(kdb.Box(0, 0, 1, 1))

    return layout


def test_layers_report_oasis(tmp_path):
    """OASIS preserves named + empty-declared layers, so it exercises the
    full schema: names, null names, and shapes: 0."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    report = layers_report(str(path))

    assert report["file"] == str(path)
    assert report["dbu_um"] == 0.001
    assert report["layer_count"] == 4

    layers = report["layers"]
    # Sorted by (layer, datatype): (1,0), (5,0), (66,20), (994,0)
    assert [(entry["layer"], entry["datatype"]) for entry in layers] == [
        (1, 0),
        (5, 0),
        (66, 20),
        (994, 0),
    ]

    by_pair = {(entry["layer"], entry["datatype"]): entry for entry in layers}
    assert by_pair[(1, 0)]["name"] == "metal1"
    assert by_pair[(1, 0)]["shapes"] == 2
    assert by_pair[(1, 0)]["annotation"] is False
    assert by_pair[(5, 0)]["name"] == "empty"
    assert by_pair[(5, 0)]["shapes"] == 0  # declared but empty
    assert by_pair[(5, 0)]["annotation"] is False
    assert by_pair[(66, 20)]["name"] is None  # unnamed -> null
    assert by_pair[(66, 20)]["shapes"] == 3
    assert by_pair[(66, 20)]["annotation"] is False
    assert by_pair[(994, 0)]["shapes"] == 1
    assert by_pair[(994, 0)]["annotation"] is True


def test_layers_report_gds(tmp_path):
    """GDSII round-trips unnamed layers (names are null) and drops empty
    layers on write — so shape counts still match for the populated layers."""
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    report = layers_report(str(path))

    by_pair = {(entry["layer"], entry["datatype"]): entry for entry in report["layers"]}
    assert by_pair[(1, 0)]["shapes"] == 2
    assert by_pair[(66, 20)]["shapes"] == 3
    assert by_pair[(994, 0)]["annotation"] is True
    # Plain GDSII carries no layer names.
    assert all(entry["name"] is None for entry in report["layers"])


def test_json_contract(tmp_path, capsys):
    """`--format json` emits exactly the documented schema."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    assert main(["layers", str(path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    # Exact top-level field set.
    assert set(data.keys()) == {
        "schema_version",
        "file",
        "dbu_um",
        "layer_count",
        "layers",
    }
    assert data["schema_version"] == 1
    assert isinstance(data["file"], str)
    assert isinstance(data["dbu_um"], float)
    assert isinstance(data["layer_count"], int)
    assert isinstance(data["layers"], list)

    for entry in data["layers"]:
        assert set(entry.keys()) == {
            "layer",
            "datatype",
            "name",
            "shapes",
            "annotation",
        }
        assert isinstance(entry["layer"], int)
        assert isinstance(entry["datatype"], int)
        assert entry["name"] is None or isinstance(entry["name"], str)
        assert isinstance(entry["shapes"], int)
        assert isinstance(entry["annotation"], bool)

    # Sorted ascending by (layer, datatype).
    pairs = [(e["layer"], e["datatype"]) for e in data["layers"]]
    assert pairs == sorted(pairs)

    # name: null normalization and shapes: 0 for the empty declared layer.
    by_pair = {(e["layer"], e["datatype"]): e for e in data["layers"]}
    assert by_pair[(66, 20)]["name"] is None
    assert by_pair[(5, 0)]["shapes"] == 0
    # Reserved annotation range (990-999, any datatype) is named as such.
    assert by_pair[(994, 0)]["annotation"] is True
    assert by_pair[(1, 0)]["annotation"] is False


def test_default_format_is_text(tmp_path, capsys):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    assert main(["layers", str(path)]) == 0
    out = capsys.readouterr().out
    assert "file:" in out
    assert "layer" in out and "datatype" in out and "shapes" in out
    assert "annotation" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_layers_report_raises_on_missing():
    with pytest.raises(LayersError):
        layers_report("/no/such/path/design.gds")


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
def test_layers_report_annotation_range_boundaries(tmp_path, layer, datatype, expected):
    """(layer, datatype) pairs are named `annotation: true` iff the layer
    number falls in the reserved 990-999 range, regardless of datatype."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    li = layout.layer(layer, datatype)
    top.shapes(li).insert(kdb.Box(0, 0, 1, 1))
    path = tmp_path / "design.oas"
    layout.write(str(path))

    report = layers_report(str(path))
    entry = next(
        e for e in report["layers"] if (e["layer"], e["datatype"]) == (layer, datatype)
    )
    assert entry["annotation"] is expected


# --- --top (issue #554) ------------------------------------------------------


def _make_multi_top_layout() -> kdb.Layout:
    """Two independent top cells with different shape counts on (1, 0):

    - ``BLOCK_A`` draws one shape directly and instantiates
      ``BLOCK_A_CHILD`` (one more shape) -- 2 shapes in ``BLOCK_A``'s
      hierarchy.
    - ``BLOCK_B`` draws a single shape -- 1 shape in its own hierarchy.

    A naive ``--top`` fix that keeps summing shapes across
    ``Layout.each_cell()`` regardless of which top cell was asked for would
    report 3 shapes for both.
    """
    layout = kdb.Layout()
    li = layout.layer(1, 0)

    a = layout.create_cell("BLOCK_A")
    a_child = layout.create_cell("BLOCK_A_CHILD")
    a.shapes(li).insert(kdb.Box(0, 0, 10, 10))
    a_child.shapes(li).insert(kdb.Box(0, 0, 10, 10))
    a.insert(kdb.CellInstArray(a_child.cell_index(), kdb.Trans(20, 20)))

    b = layout.create_cell("BLOCK_B")
    b.shapes(li).insert(kdb.Box(0, 0, 10, 10))

    return layout


def test_top_scopes_shape_counts_to_named_cell_hierarchy(tmp_path):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    report_a = layers_report(str(path), top="BLOCK_A")
    (entry_a,) = report_a["layers"]
    assert entry_a["shapes"] == 2

    report_b = layers_report(str(path), top="BLOCK_B")
    (entry_b,) = report_b["layers"]
    assert entry_b["shapes"] == 1

    # Neither scoped count equals the whole-stream union (3 shapes) --
    # proving the count actually re-scopes, not just accepts the flag.
    report_all = layers_report(str(path))
    (entry_all,) = report_all["layers"]
    assert entry_all["shapes"] == 3


def test_top_unknown_cell_raises(tmp_path):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    with pytest.raises(LayersError, match="top cell not found in stream: NOPE"):
        layers_report(str(path), top="NOPE")


def test_top_on_single_top_cell_stream_matches_omitting_it(tmp_path):
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    assert layers_report(str(path), top="TOP") == layers_report(str(path))


def test_cli_top_flag_scopes_multi_top_report(tmp_path, capsys):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    assert main(["layers", str(path), "--top", "BLOCK_B", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (entry,) = data["layers"]
    assert entry["shapes"] == 1


def test_cli_unknown_top_exits_one(tmp_path, capsys):
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    assert main(["layers", str(path), "--top", "NOPE"]) == 1
    err = capsys.readouterr().err
    assert "top cell not found in stream: NOPE" in err


# --- --flattened / --include-text (issue #675) ------------------------------


def _make_flattened_hierarchy() -> kdb.Layout:
    """``CHILD`` instantiated under multiple transforms and multiplicities on
    ``TOP``, exercising the ``--flattened``/``--include-text`` acceptance
    criteria (issue #675):

    - ``CHILD`` draws one box (0,0)-(10,10) and one text label ``"PIN"`` at
      (5,5) on layer (1, 0) -- definition-level: 2 shapes.
    - ``TOP`` instantiates ``CHILD`` 8 times total: once plain at the
      origin, once rotated 90 degrees and displaced to (100, 0), and once as
      a 2x3 array with a (300, 0) base and (50, 0)/(0, 50) row/column
      vectors -- so flattened counts are 8x the definition count, spanning
      three distinct transform kinds (plain, rotation+displacement, array).
    """
    layout = kdb.Layout()
    li = layout.layer(1, 0)

    child = layout.create_cell("CHILD")
    child.shapes(li).insert(kdb.Box(0, 0, 10, 10))
    child.shapes(li).insert(kdb.Text("PIN", kdb.Trans(5, 5)))

    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(0, 0)))
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Trans.R90, 100, 0)))
    top.insert(
        kdb.CellInstArray(
            child.cell_index(),
            kdb.Trans(300, 0),
            kdb.Vector(50, 0),
            kdb.Vector(0, 50),
            2,
            3,
        )
    )

    return layout


def test_flattened_default_off_preserves_definition_only_schema(tmp_path):
    """Omitting ``--flattened`` leaves the report byte-identical to the
    pre-#675 schema -- no new keys appear at all."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path))
    (entry,) = report["layers"]
    assert set(entry.keys()) == {"layer", "datatype", "name", "shapes", "annotation"}
    assert entry["shapes"] == 2


def test_flattened_definition_count_unchanged(tmp_path):
    """Requesting ``--flattened`` does not change the existing
    per-cell-definition ``shapes`` count -- only adds new fields."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path), flattened=True)
    (entry,) = report["layers"]
    assert entry["shapes"] == 2  # unchanged: 1 box + 1 text, defined once


def test_flattened_shape_count_scales_with_instantiation(tmp_path):
    """``flattened_shapes`` reflects every placement: 8 instances * (1 box +
    1 text) = 16, not the 2-shape definition count."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path), flattened=True)
    (entry,) = report["layers"]
    assert entry["flattened_shapes"] == 16


def test_flattened_bbox_includes_transforms(tmp_path):
    """The flattened bounding box is the union of every placement's
    transformed extents -- plain, rotated+displaced, and arrayed."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path), flattened=True)
    (entry,) = report["layers"]
    assert entry["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 0.36,
        "top": 0.11,
        "width": 0.36,
        "height": 0.11,
    }


def test_flattened_contributors_are_instance_weighted(tmp_path):
    """``contributors`` counts scale with instantiation (16, matching
    ``flattened_shapes``), not the 2-shape definition count -- the whole
    hierarchy funnels through a single contributing cell here."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path), flattened=True)
    (entry,) = report["layers"]
    assert entry["contributors"] == [{"cell": "CHILD", "shapes": 16}]


def test_flattened_omits_text_fields_without_include_text(tmp_path):
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path), flattened=True)
    (entry,) = report["layers"]
    assert "text_count" not in entry
    assert "texts" not in entry


def test_include_text_reports_flattened_occurrence_counts(tmp_path):
    """``text_count``/``texts`` match the flattened placement count (8), not
    the single definition-level text shape."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    report = layers_report(str(path), flattened=True, include_text=True)
    (entry,) = report["layers"]
    assert entry["text_count"] == 8
    assert entry["texts"] == [{"text": "PIN", "count": 8}]


def test_include_text_requires_flattened():
    with pytest.raises(LayersError, match="--include-text requires --flattened"):
        layers_report("/no/such/path/design.gds", include_text=True)


def test_flattened_report_is_deterministic(tmp_path):
    """Two independent runs over the same file produce byte-identical JSON
    -- contributor and text ordering does not depend on iteration order."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    first = layers_report(str(path), flattened=True, include_text=True)
    second = layers_report(str(path), flattened=True, include_text=True)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_flattened_top_scopes_flattening_root(tmp_path):
    """With ``--top``, flattening starts only at the named cell's own
    sub-hierarchy -- a multi-top library's other top cell does not leak into
    the flattened count (issue #675's "top-cell selection for multi-top
    libraries" requirement)."""
    layout = kdb.Layout()
    li = layout.layer(1, 0)

    child = layout.create_cell("CHILD")
    child.shapes(li).insert(kdb.Box(0, 0, 10, 10))

    block_a = layout.create_cell("BLOCK_A")
    block_a.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(0, 0)))
    block_a.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(20, 0)))

    block_b = layout.create_cell("BLOCK_B")
    block_b.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(0, 0)))

    path = tmp_path / "multi_top_hier.gds"
    layout.write(str(path))

    report_a = layers_report(str(path), top="BLOCK_A", flattened=True)
    (entry_a,) = report_a["layers"]
    assert entry_a["flattened_shapes"] == 2

    report_b = layers_report(str(path), top="BLOCK_B", flattened=True)
    (entry_b,) = report_b["layers"]
    assert entry_b["flattened_shapes"] == 1

    # Whole-stream (no --top) sums flattening roots across every top cell.
    report_all = layers_report(str(path), flattened=True)
    (entry_all,) = report_all["layers"]
    assert entry_all["flattened_shapes"] == 3


def test_flattened_layer_with_no_shapes_reports_empty_bbox(tmp_path):
    """A declared-but-empty layer under ``--flattened`` reports the same
    all-zero ``bbox_um`` convention as ``klt stats``, plus zero counts."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))  # layer (5, 0) "empty" carries no shapes

    report = layers_report(str(path), flattened=True)
    entry = next(e for e in report["layers"] if (e["layer"], e["datatype"]) == (5, 0))
    assert entry["flattened_shapes"] == 0
    assert entry["contributors"] == []
    assert entry["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 0.0,
        "top": 0.0,
        "width": 0.0,
        "height": 0.0,
    }


def test_cli_flattened_json_reports_new_fields(tmp_path, capsys):
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    assert (
        main(["layers", str(path), "--flattened", "--include-text", "--format", "json"])
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    (entry,) = data["layers"]
    assert set(entry.keys()) == {
        "layer",
        "datatype",
        "name",
        "shapes",
        "annotation",
        "flattened_shapes",
        "bbox_um",
        "contributors",
        "text_count",
        "texts",
    }
    assert entry["flattened_shapes"] == 16
    assert entry["text_count"] == 8


def test_cli_flattened_text_output_includes_new_columns(tmp_path, capsys):
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    assert main(["layers", str(path), "--flattened", "--include-text"]) == 0
    out = capsys.readouterr().out
    assert "flattened_shapes" in out
    assert "bbox_um" in out
    assert "contributors" in out
    assert "text_count" in out
    assert "texts" in out
    assert "CHILD:16" in out
    assert "PIN:8" in out


def test_cli_default_text_output_unchanged_without_flattened(tmp_path, capsys):
    """Same assertion as ``test_default_format_is_text``, run against the
    flattened-hierarchy fixture: no new columns leak into default output."""
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    assert main(["layers", str(path)]) == 0
    out = capsys.readouterr().out
    assert "flattened_shapes" not in out
    assert "bbox_um" not in out
    assert "contributors" not in out


def test_cli_include_text_without_flattened_exits_one(tmp_path, capsys):
    path = tmp_path / "hier.gds"
    _make_flattened_hierarchy().write(str(path))

    assert main(["layers", str(path), "--include-text"]) == 1
    err = capsys.readouterr().err
    assert "--include-text requires --flattened" in err
