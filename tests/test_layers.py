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
