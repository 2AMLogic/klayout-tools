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


def test_render_report_writes_overview(tmp_path):
    """An all-layers composite `overview.png` is always written and reported,
    and a stale one is cleared on re-render like the per-layer PNGs."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = out_dir / "overview.png"
    stale.write_bytes(b"stale")

    path = _write_design(tmp_path)
    report = render_report(str(path), output_dir=str(out_dir))

    overview = Path(report["overview"])
    assert overview == out_dir / "overview.png"
    assert overview.is_file()
    assert overview.read_bytes() != b"stale"
    assert overview.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_report_background(tmp_path):
    """`background` is validated and echoed in the report."""
    path = _write_design(tmp_path)
    report = render_report(
        str(path), output_dir=str(tmp_path / "out"), background="#0b0e13"
    )
    assert report["background"] == "#0b0e13"

    with pytest.raises(RenderError, match="invalid background color"):
        render_report(str(path), output_dir=str(tmp_path / "out2"), background="red")


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
        "background",
        "overview",
        "layer_count",
        "rendered_count",
        "requested_layers",
        "requested_bbox",
        "actual_extent",
        "layers",
    }
    assert isinstance(data["background"], str)
    assert isinstance(data["overview"], str)
    assert data["schema_version"] == 1
    assert isinstance(data["output_dir"], str)
    assert isinstance(data["width"], int)
    assert isinstance(data["height"], int)
    assert isinstance(data["layer_count"], int)
    assert isinstance(data["rendered_count"], int)
    assert data["requested_layers"] is None
    assert data["requested_bbox"] is None
    assert set(data["actual_extent"].keys()) == {"left", "bottom", "right", "top"}

    for entry in data["layers"]:
        assert set(entry.keys()) == {
            "layer",
            "datatype",
            "name",
            "shapes",
            "annotation",
            "path",
            "rendered",
        }
        assert isinstance(entry["rendered"], bool)
        assert isinstance(entry["annotation"], bool)
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


# --- --top (issue #554) ------------------------------------------------------


def _make_multi_top_layout() -> kdb.Layout:
    """Two independent top cells, each drawing on its own, otherwise-unused
    layer -- so a scoped ``--top`` render only picks up one of them."""
    layout = kdb.Layout()

    a = layout.create_cell("BLOCK_A")
    la = layout.layer(1, 0)
    a.shapes(la).insert(kdb.Box(0, 0, 1000, 1000))

    b = layout.create_cell("BLOCK_B")
    lb = layout.layer(2, 0)
    b.shapes(lb).insert(kdb.Box(0, 0, 1000, 1000))

    return layout


def _write_multi_top(tmp_path: Path, name: str = "multi_top.gds") -> Path:
    path = tmp_path / name
    _make_multi_top_layout().write(str(path))
    return path


def test_top_scopes_layer_report_and_render(tmp_path):
    path = _write_multi_top(tmp_path)

    report_a = render_report(
        str(path), output_dir=str(tmp_path / "out_a"), top="BLOCK_A"
    )
    by_pair_a = _by_pair(report_a["layers"])
    assert by_pair_a[(1, 0)]["shapes"] == 1
    assert by_pair_a[(1, 0)]["rendered"] is True
    # Layer (2, 0) belongs only to BLOCK_B's hierarchy -- scoped to
    # BLOCK_A it must report 0 shapes and not be rendered.
    assert by_pair_a[(2, 0)]["shapes"] == 0
    assert by_pair_a[(2, 0)]["rendered"] is False

    report_all = render_report(str(path), output_dir=str(tmp_path / "out_all"))
    by_pair_all = _by_pair(report_all["layers"])
    assert by_pair_all[(1, 0)]["shapes"] == 1
    assert by_pair_all[(2, 0)]["shapes"] == 1


def test_top_unknown_cell_raises(tmp_path):
    path = _write_multi_top(tmp_path)

    with pytest.raises(RenderError, match="top cell not found in stream: NOPE"):
        render_report(str(path), output_dir=str(tmp_path / "out"), top="NOPE")


def test_top_on_single_top_cell_stream_renders_same_as_omitting_it(tmp_path):
    path = _write_design(tmp_path)

    with_top = render_report(str(path), output_dir=str(tmp_path / "out_top"), top="TOP")
    without_top = render_report(str(path), output_dir=str(tmp_path / "out_default"))

    # `output_dir`/per-file `path` fields differ by construction (distinct
    # output directories); everything else -- what got rendered, and with
    # what shape counts -- must match.
    strip = {"output_dir", "overview", "layers"}
    assert {k: v for k, v in with_top.items() if k not in strip} == {
        k: v for k, v in without_top.items() if k not in strip
    }
    layers_with_top = [
        {k: v for k, v in e.items() if k != "path"} for e in with_top["layers"]
    ]
    layers_without_top = [
        {k: v for k, v in e.items() if k != "path"} for e in without_top["layers"]
    ]
    assert layers_with_top == layers_without_top


def test_cli_top_flag_scopes_report(tmp_path, capsys):
    path = _write_multi_top(tmp_path)

    assert (
        main(
            [
                "render",
                str(path),
                "-o",
                str(tmp_path / "out"),
                "--top",
                "BLOCK_A",
                "--format",
                "json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    by_pair = _by_pair(data["layers"])
    assert by_pair[(2, 0)]["shapes"] == 0


def test_cli_unknown_top_exits_one(tmp_path, capsys):
    path = _write_multi_top(tmp_path)

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--top", "NOPE"]) == 1
    )
    err = capsys.readouterr().err
    assert "top cell not found in stream: NOPE" in err


# --- --layers / --bbox (issue #673) -----------------------------------------

#: Full-chip geometry on layer (1, 0): fills the entire layout.
_CHIP_UM = (0.0, 0.0, 100.0, 100.0)
#: A small feature, deep inside the chip, on a distinct layer.
_FEATURE_UM = (40.0, 40.0, 42.0, 42.0)


def _make_chip_and_feature_layout() -> kdb.Layout:
    """A synthetic GDS with large full-chip geometry plus a small feature on
    a separate layer -- the exact shape the issue's acceptance criteria call
    for. Without layer/bbox selection, the small feature is a handful of
    pixels inside the full-chip render; ``--layers``/``--bbox`` isolate it.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    dbu = layout.dbu

    def to_dbu(v: float) -> int:
        return round(v / dbu)

    chip = layout.layer(1, 0)
    top.shapes(chip).insert(
        kdb.Box(*(to_dbu(v) for v in _CHIP_UM)),
    )

    feature = layout.layer(2, 0)
    top.shapes(feature).insert(
        kdb.Box(*(to_dbu(v) for v in _FEATURE_UM)),
    )

    return layout


def _write_chip_and_feature(tmp_path: Path, name: str = "chip.gds") -> Path:
    path = tmp_path / name
    _make_chip_and_feature_layout().write(str(path))
    return path


def test_layers_option_filters_report_and_skips_unselected_files(tmp_path):
    """``layers`` restricts which per-layer PNGs get written -- an
    unselected layer is reported ``rendered: false, path: None`` even though
    it has shapes, and its PNG is never written to disk."""
    path = _write_chip_and_feature(tmp_path)
    out_dir = tmp_path / "out"

    report = render_report(str(path), output_dir=str(out_dir), layers=[(2, 0)])

    assert report["requested_layers"] == [[2, 0]]
    by_pair = _by_pair(report["layers"])

    chip_entry = by_pair[(1, 0)]
    assert chip_entry["shapes"] > 0  # has geometry ...
    assert chip_entry["rendered"] is False  # ... but was not selected
    assert chip_entry["path"] is None
    assert not (out_dir / "1_0.png").exists()

    feature_entry = by_pair[(2, 0)]
    assert feature_entry["rendered"] is True
    assert Path(feature_entry["path"]).is_file()
    assert (out_dir / "2_0.png").exists()

    assert report["rendered_count"] == 1
    # layer_count still reflects the full design, matching `klt layers`.
    assert report["layer_count"] == 2


def test_layers_option_absent_pair_matches_nothing(tmp_path):
    """A requested pair not present in the stream matches nothing -- no
    crash, just an all-unrendered report."""
    path = _write_chip_and_feature(tmp_path)

    report = render_report(
        str(path), output_dir=str(tmp_path / "out"), layers=[(99, 0)]
    )

    assert report["requested_layers"] == [[99, 0]]
    assert report["rendered_count"] == 0
    assert all(not entry["rendered"] for entry in report["layers"])


def test_layers_option_empty_list_raises(tmp_path):
    path = _write_chip_and_feature(tmp_path)
    with pytest.raises(RenderError, match="at least one layer"):
        render_report(str(path), output_dir=str(tmp_path / "out"), layers=[])


def test_layers_option_omitted_matches_default_behavior(tmp_path):
    """``layers=None`` (the default) renders exactly like before this option
    existed -- the no-option regression guard for issue #673."""
    path = _write_chip_and_feature(tmp_path)

    with_none = render_report(
        str(path), output_dir=str(tmp_path / "out_a"), layers=None
    )
    omitted = render_report(str(path), output_dir=str(tmp_path / "out_b"))

    strip = {"output_dir", "overview", "layers"}
    assert {k: v for k, v in with_none.items() if k not in strip} == {
        k: v for k, v in omitted.items() if k not in strip
    }


#: `LayoutView.zoom_box()` snaps the viewport to its internal pixel grid, so
#: `view.box()` can differ from the literal request by a small sub-pixel
#: amount (observed: a fraction of one pixel's physical size) even when no
#: aspect-ratio padding is needed -- not a rounding bug this module
#: introduces, just `zoom_box()`'s own quantization. Tolerance below is
#: generous relative to that (well under one pixel at the sizes used here).
_EXTENT_TOL_UM = 0.05


def test_bbox_option_reports_requested_and_actual_extent(tmp_path):
    """A square ``--bbox`` matching the canvas's own aspect ratio needs no
    aspect-ratio padding, so ``actual_extent`` matches ``requested_bbox`` up
    to `zoom_box()`'s own sub-pixel snapping."""
    path = _write_chip_and_feature(tmp_path)
    window = (38.0, 38.0, 44.0, 44.0)  # 6x6 um square, square canvas below

    report = render_report(
        str(path),
        output_dir=str(tmp_path / "out"),
        width=100,
        height=100,
        bbox=window,
    )

    assert report["requested_bbox"] == {
        "left": 38.0,
        "bottom": 38.0,
        "right": 44.0,
        "top": 44.0,
    }
    assert report["actual_extent"] == pytest.approx(
        {"left": 38.0, "bottom": 38.0, "right": 44.0, "top": 44.0}, abs=_EXTENT_TOL_UM
    )


def test_bbox_option_pads_non_matching_aspect_ratio(tmp_path):
    """A non-square ``--bbox`` against a non-matching canvas aspect ratio is
    padded (not stretched) to the canvas's aspect ratio, centered on the
    request -- `actual_extent` must (up to sub-pixel snapping) contain
    `requested_bbox` and be wider without changing height."""
    path = _write_chip_and_feature(tmp_path)
    window = (38.0, 38.0, 44.0, 44.0)  # 1:1 aspect request ...

    report = render_report(
        str(path),
        output_dir=str(tmp_path / "out"),
        width=200,
        height=100,  # ... against a 2:1 canvas
        bbox=window,
    )

    extent = report["actual_extent"]
    # Vertical extent (the matching axis) is essentially unchanged.
    assert extent["bottom"] == pytest.approx(38.0, abs=_EXTENT_TOL_UM)
    assert extent["top"] == pytest.approx(44.0, abs=_EXTENT_TOL_UM)
    # Horizontal extent is padded to match the 2:1 canvas -- roughly double
    # the requested 6 um width, not stretched to it.
    width = extent["right"] - extent["left"]
    assert width == pytest.approx(12.0, abs=_EXTENT_TOL_UM)
    # Fully contains the requested window (up to sub-pixel snapping).
    assert extent["left"] <= 38.0 + _EXTENT_TOL_UM
    assert extent["right"] >= 44.0 - _EXTENT_TOL_UM
    # Padded roughly symmetrically around the request (centered).
    center_x = (extent["left"] + extent["right"]) / 2
    assert center_x == pytest.approx(41.0, abs=_EXTENT_TOL_UM)


def test_bbox_option_excludes_all_geometry_still_renders(tmp_path):
    """A ``--bbox`` that misses all geometry on the selected layer still
    renders successfully (a blank/background image), doesn't crash."""
    path = _write_chip_and_feature(tmp_path)
    out_dir = tmp_path / "out"

    report = render_report(
        str(path),
        output_dir=str(out_dir),
        layers=[(2, 0)],
        bbox=(90.0, 90.0, 95.0, 95.0),  # far from the (40,40)-(42,42) feature
    )

    by_pair = _by_pair(report["layers"])
    feature_entry = by_pair[(2, 0)]
    # `rendered` reflects the layer's own (whole-layout) shape count, not
    # bbox intersection -- an empty-in-window render still writes a file.
    assert feature_entry["rendered"] is True
    assert Path(feature_entry["path"]).is_file()
    assert Path(feature_entry["path"]).stat().st_size > 0


def test_bbox_invalid_ordering_raises(tmp_path):
    path = _write_chip_and_feature(tmp_path)
    with pytest.raises(RenderError, match="invalid --bbox"):
        render_report(
            str(path), output_dir=str(tmp_path / "out"), bbox=(10.0, 10.0, 10.0, 20.0)
        )
    with pytest.raises(RenderError, match="invalid --bbox"):
        render_report(
            str(path), output_dir=str(tmp_path / "out2"), bbox=(10.0, 20.0, 20.0, 10.0)
        )


def test_layers_and_bbox_combine(tmp_path):
    """``layers`` and ``bbox`` compose: only the selected layer is rendered,
    cropped to the requested window."""
    path = _write_chip_and_feature(tmp_path)
    out_dir = tmp_path / "out"

    report = render_report(
        str(path),
        output_dir=str(out_dir),
        layers=[(2, 0)],
        bbox=(38.0, 38.0, 44.0, 44.0),
    )

    by_pair = _by_pair(report["layers"])
    assert by_pair[(1, 0)]["rendered"] is False
    assert by_pair[(2, 0)]["rendered"] is True
    assert report["requested_layers"] == [[2, 0]]
    assert report["requested_bbox"] == {
        "left": 38.0,
        "bottom": 38.0,
        "right": 44.0,
        "top": 44.0,
    }
    assert not (out_dir / "1_0.png").exists()
    assert (out_dir / "2_0.png").exists()


def test_cli_layers_flag_scopes_report(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(
            [
                "render",
                str(path),
                "-o",
                str(tmp_path / "out"),
                "--layers",
                "[[2, 0]]",
                "--format",
                "json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    by_pair = _by_pair(data["layers"])
    assert by_pair[(1, 0)]["rendered"] is False
    assert by_pair[(2, 0)]["rendered"] is True
    assert data["requested_layers"] == [[2, 0]]


def test_cli_layers_malformed_json_exits_one(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--layers", "not json"])
        == 1
    )
    err = capsys.readouterr().err
    assert "--layers" in err


def test_cli_layers_empty_array_exits_one(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--layers", "[]"]) == 1
    )
    err = capsys.readouterr().err
    assert "at least one layer" in err


def test_cli_bbox_flag_scopes_extent(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(
            [
                "render",
                str(path),
                "-o",
                str(tmp_path / "out"),
                "--bbox",
                "38,38,44,44",
                "--width",
                "100",
                "--height",
                "100",
                "--format",
                "json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["requested_bbox"] == {
        "left": 38.0,
        "bottom": 38.0,
        "right": 44.0,
        "top": 44.0,
    }
    assert data["actual_extent"] == pytest.approx(
        {"left": 38.0, "bottom": 38.0, "right": 44.0, "top": 44.0}, abs=_EXTENT_TOL_UM
    )


def test_cli_bbox_malformed_exits_one(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--bbox", "not,a,box"])
        == 1
    )
    err = capsys.readouterr().err
    assert "--bbox" in err


def test_cli_bbox_wrong_ordering_exits_one(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--bbox", "10,10,5,5"])
        == 1
    )
    err = capsys.readouterr().err
    assert "--bbox" in err


def test_cli_bbox_wrong_field_count_exits_one(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(["render", str(path), "-o", str(tmp_path / "out"), "--bbox", "1,2,3"]) == 1
    )
    err = capsys.readouterr().err
    assert "--bbox" in err


def test_cli_text_format_prints_requested_and_actual_extent(tmp_path, capsys):
    path = _write_chip_and_feature(tmp_path)

    assert (
        main(
            [
                "render",
                str(path),
                "-o",
                str(tmp_path / "out"),
                "--layers",
                "[[2, 0]]",
                "--bbox",
                "38,38,44,44",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "requested_layers: 2/0" in out
    assert "requested_bbox: (38.0,38.0)-(44.0,44.0)" in out
    assert "actual_extent:" in out
