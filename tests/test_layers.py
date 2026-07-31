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

    return layout


def test_layers_report_oasis(tmp_path):
    """OASIS preserves named + empty-declared layers, so it exercises the
    full schema: names, null names, and shapes: 0."""
    path = tmp_path / "design.oas"
    _make_layout().write(str(path))

    report = layers_report(str(path))

    assert report["file"] == str(path)
    assert report["dbu_um"] == 0.001
    assert report["layer_count"] == 3

    layers = report["layers"]
    # Sorted by (layer, datatype): (1,0), (5,0), (66,20)
    assert [(entry["layer"], entry["datatype"]) for entry in layers] == [
        (1, 0),
        (5, 0),
        (66, 20),
    ]

    by_pair = {(entry["layer"], entry["datatype"]): entry for entry in layers}
    assert by_pair[(1, 0)]["name"] == "metal1"
    assert by_pair[(1, 0)]["shapes"] == 2
    assert by_pair[(5, 0)]["name"] == "empty"
    assert by_pair[(5, 0)]["shapes"] == 0  # declared but empty
    assert by_pair[(66, 20)]["name"] is None  # unnamed -> null
    assert by_pair[(66, 20)]["shapes"] == 3


def test_layers_report_gds(tmp_path):
    """GDSII round-trips unnamed layers (names are null) and drops empty
    layers on write — so shape counts still match for the populated layers."""
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    report = layers_report(str(path))

    by_pair = {(entry["layer"], entry["datatype"]): entry for entry in report["layers"]}
    assert by_pair[(1, 0)]["shapes"] == 2
    assert by_pair[(66, 20)]["shapes"] == 3
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
        assert set(entry.keys()) == {"layer", "datatype", "name", "shapes"}
        assert isinstance(entry["layer"], int)
        assert isinstance(entry["datatype"], int)
        assert entry["name"] is None or isinstance(entry["name"], str)
        assert isinstance(entry["shapes"], int)

    # Sorted ascending by (layer, datatype).
    pairs = [(e["layer"], e["datatype"]) for e in data["layers"]]
    assert pairs == sorted(pairs)

    # name: null normalization and shapes: 0 for the empty declared layer.
    by_pair = {(e["layer"], e["datatype"]): e for e in data["layers"]}
    assert by_pair[(66, 20)]["name"] is None
    assert by_pair[(5, 0)]["shapes"] == 0


def test_default_format_is_text(tmp_path, capsys):
    path = tmp_path / "design.gds"
    _make_layout().write(str(path))

    assert main(["layers", str(path)]) == 0
    out = capsys.readouterr().out
    assert "file:" in out
    assert "layer" in out and "datatype" in out and "shapes" in out
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_layers_report_raises_on_missing():
    with pytest.raises(LayersError):
        layers_report("/no/such/path/design.gds")
