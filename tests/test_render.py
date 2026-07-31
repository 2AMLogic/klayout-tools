"""Tests for `klt render` and the `render_report` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus (mirrors `test_layers.py`). Rendered PNG
*pixel content* is not asserted (rendering can vary subtly across platforms
and KLayout versions); instead these tests assert the documented, stable
contract -- which files get written, their dimensions, and the JSON shape.
"""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.render import RenderError, render_report


def _make_layout() -> kdb.Layout:
    """A layout with known layers and shape counts.

    - (1, 0)   named "metal1", 2 shapes
    - (66, 20) unnamed,        3 shapes
    - (5, 0)   named "empty",  0 shapes (declared but empty)
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    m1 = layout.layer(1, 0)
    layout.set_info(m1, kdb.LayerInfo(1, 0, "metal1"))
    top.shapes(m1).insert(kdb.Box(0, 0, 10, 10))
    top.shapes(m1).insert(kdb.Box(20, 20, 30, 30))

    m66 = layout.layer(66, 20)
    top.shapes(m66).insert(kdb.Box(0, 0, 5, 5))
    top.shapes(m66).insert(kdb.Box(6, 6, 9, 9))
    top.shapes(m66).insert(kdb.Box(1, 1, 2, 2))

    empty = layout.layer(5, 0)
    layout.set_info(empty, kdb.LayerInfo(5, 0, "empty"))

    return layout


def _write_design(tmp_path: Path, name: str = "design.oas") -> Path:
    path = tmp_path / name
    _make_layout().write(str(path))
    return path


def _by_pair(entries: list[dict]) -> dict[tuple[int, int], dict]:
    return {(e["layer"], e["datatype"]): e for e in entries}


def test_render_report_default_output_dir(tmp_path):
    """Default output_dir is a `renders/` sibling of the input file --
    mirroring `<block>/output/<name>.gds` -> `<block>/output/renders/`."""
    block_output = tmp_path / "block" / "output"
    block_output.mkdir(parents=True)
    path = block_output / "design.gds"
    _make_layout().write(str(path))

    report = render_report(str(path))

    assert report["output_dir"] == str(block_output / "renders")
    assert Path(report["output_dir"]).is_dir()


def test_render_report_writes_pngs_for_non_empty_layers(tmp_path):
    path = _write_design(tmp_path)

    report = render_report(str(path), output_dir=str(tmp_path / "out"))

    assert report["schema_version"] == 1
    assert report["file"] == str(path)
    assert report["width"] == 1024
    assert report["height"] == 768
    assert report["layer_count"] == 3
    assert report["rendered_count"] == 2  # (5, 0) is empty, skipped

    by_pair = _by_pair(report["layers"])

    m1 = by_pair[(1, 0)]
    assert m1["name"] == "metal1"
    assert m1["shapes"] == 2
    assert m1["rendered"] is True
    assert m1["path"] is not None
    assert Path(m1["path"]).is_file()
    assert Path(m1["path"]).stat().st_size > 0

    m66 = by_pair[(66, 20)]
    assert m66["rendered"] is True
    assert Path(m66["path"]).is_file()

    empty = by_pair[(5, 0)]
    assert empty["name"] == "empty"
    assert empty["shapes"] == 0
    assert empty["rendered"] is False
    assert empty["path"] is None


def test_render_report_custom_dimensions(tmp_path):
    path = _write_design(tmp_path)

    report = render_report(
        str(path), output_dir=str(tmp_path / "out"), width=200, height=150
    )

    assert report["width"] == 200
    assert report["height"] == 150

    m1 = _by_pair(report["layers"])[(1, 0)]
    data = Path(m1["path"]).read_bytes()
    # PNG IHDR chunk: signature(8) + length(4) + "IHDR"(4) + width(4) + height(4)
    assert int.from_bytes(data[16:20], "big") == 200
    assert int.from_bytes(data[20:24], "big") == 150


def test_render_report_clears_stale_owned_files(tmp_path):
    """Re-rendering into the same directory removes PNGs this command
    previously wrote for layers no longer present, without touching other
    files a caller placed in that directory."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = out_dir / "999_999.png"
    stale.write_bytes(b"stale")
    unrelated = out_dir / "keepme.txt"
    unrelated.write_text("do not touch")

    path = _write_design(tmp_path)
    render_report(str(path), output_dir=str(out_dir))

    assert not stale.exists()
    assert unrelated.is_file()


def test_render_report_invalid_dimensions(tmp_path):
    path = _write_design(tmp_path)
    with pytest.raises(RenderError, match="invalid image size"):
        render_report(str(path), output_dir=str(tmp_path / "out"), width=0)
    with pytest.raises(RenderError, match="invalid image size"):
        render_report(str(path), output_dir=str(tmp_path / "out"), height=-1)


def test_render_report_missing_file():
    with pytest.raises(RenderError, match="not found"):
        render_report("/no/such/path/design.gds")


def test_render_report_directory(tmp_path):
    with pytest.raises(RenderError, match="not a file"):
        render_report(str(tmp_path))


def test_render_report_non_layout_file(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a layout stream\n" * 4)
    with pytest.raises(RenderError):
        render_report(str(bogus), output_dir=str(tmp_path / "out"))


def test_json_contract(tmp_path, capsys):
    """`--format json` emits exactly the documented schema."""
    path = _write_design(tmp_path)

    exit_code = main(
        ["render", str(path), "-o", str(tmp_path / "out"), "--format", "json"]
    )
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data.keys()) == {
        "schema_version",
        "file",
        "output_dir",
        "width",
        "height",
        "layer_count",
        "rendered_count",
        "layers",
    }
    assert data["schema_version"] == 1
    assert isinstance(data["output_dir"], str)
    assert isinstance(data["width"], int)
    assert isinstance(data["height"], int)
    assert isinstance(data["layer_count"], int)
    assert isinstance(data["rendered_count"], int)

    for entry in data["layers"]:
        assert set(entry.keys()) == {
            "layer",
            "datatype",
            "name",
            "shapes",
            "path",
            "rendered",
        }
        assert isinstance(entry["rendered"], bool)
        assert entry["path"] is None or isinstance(entry["path"], str)


def test_default_format_is_text(tmp_path, capsys):
    path = _write_design(tmp_path)

    assert main(["render", str(path), "-o", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "file:" in out
    assert "output_dir:" in out
    assert "layer" in out and "datatype" in out and "path" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_no_layers_produces_empty_report(tmp_path, capsys):
    """A layout with no layers at all renders nothing, but still succeeds."""
    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = tmp_path / "empty.gds"
    layout.write(str(path))

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--format", "json"])
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["layer_count"] == 0
    assert data["rendered_count"] == 0
    assert data["layers"] == []
