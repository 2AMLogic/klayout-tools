"""Tests for `klt clip` and the `run_clip` library function (issue #1608).

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus or PDK deck, mirroring
`tests/test_ring_check.py`/`tests/test_components.py`. One fixture layout
covers both modes:

- **Region-clip**: `TOP` carries a box + text inside an intended clip window
  and a box + text well outside it, plus a nested `CHILD` instance whose
  *flattened* shape also lands inside the window -- proving region-clip
  flattens across hierarchy rather than only looking at `TOP`'s own shapes.
- **Cell-extraction**: a `MACRO` cell (deliberately *not* a top cell --
  instantiated inside `TOP`) with its own nested `SUBCELL` instance -- proving
  `--cell` can name any cell in the stream and that `copy_tree` preserves
  multi-level hierarchy independent of where the source cell happened to be
  placed.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.clip import ClipError, run_clip

_DBU = 0.001  # 1 nm/dbu -> 1 um == 1000 dbu, exact at this grid.

_METAL = (10, 0)
_LABEL = (11, 0)


def _build_fixture(path) -> None:
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("TOP")

    metal = layout.layer(*_METAL)
    label = layout.layer(*_LABEL)

    # Directly on TOP: one box + one label inside the intended [0,0,15,15]
    # clip window, one box + one label well outside it.
    top.shapes(metal).insert(kdb.Box(0, 0, 5000, 5000))  # 0,0 - 5,5 um
    top.shapes(label).insert(kdb.Text("IN", kdb.Trans(2000, 2000)))  # 2,2 um
    top.shapes(metal).insert(kdb.Box(50000, 50000, 55000, 55000))  # 50,50-55,55 um
    top.shapes(label).insert(kdb.Text("OUT", kdb.Trans(52000, 52000)))

    # A nested instance whose flattened shape (10,10 - 12,12 um) also lands
    # inside the clip window -- region-clip must flatten across hierarchy.
    child = layout.create_cell("CHILD")
    child.shapes(metal).insert(kdb.Box(0, 0, 2000, 2000))
    top.insert(
        kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Point(10000, 10000)))
    )

    # A non-top-cell macro with its own nested subcell.
    subcell = layout.create_cell("SUBCELL")
    subcell.shapes(metal).insert(kdb.Box(0, 0, 1000, 1000))
    macro = layout.create_cell("MACRO")
    macro.shapes(metal).insert(kdb.Box(0, 0, 3000, 3000))
    macro.insert(kdb.CellInstArray(subcell.cell_index(), kdb.Trans()))
    top.insert(
        kdb.CellInstArray(macro.cell_index(), kdb.Trans(kdb.Point(30000, 30000)))
    )

    layout.write(str(path))


def _build_multi_top_fixture(path) -> None:
    layout = kdb.Layout()
    layout.dbu = _DBU
    metal = layout.layer(*_METAL)
    top1 = layout.create_cell("TOP1")
    top1.shapes(metal).insert(kdb.Box(0, 0, 1000, 1000))
    top2 = layout.create_cell("TOP2")
    top2.shapes(metal).insert(kdb.Box(0, 0, 1000, 1000))
    layout.write(str(path))


# --- Region-clip mode ---------------------------------------------------


def test_region_clip_flattens_across_hierarchy_and_isolates_window(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    report = run_clip(str(fixture), str(out_path), region_um=(0.0, 0.0, 15.0, 15.0))

    assert report["schema_version"] == 1
    assert report["file"] == str(fixture)
    assert report["output"] == str(out_path)
    assert report["mode"] == "region"
    assert report["cell"] is None
    assert report["region_um"] == [0.0, 0.0, 15.0, 15.0]
    assert report["top"] == "TOP"
    assert report["dbu_um"] == _DBU
    assert report["cell_count"] == 1
    # TOP's in-window box + CHILD's flattened box (region, 2 polygons) +
    # the "IN" text -- the far box/text are excluded by the clip window.
    assert report["shape_count"] == 3

    out = kdb.Layout()
    out.read(str(out_path))
    assert [c.name for c in out.each_cell()] == ["CLIP"]
    assert out.dbu == _DBU

    clip_top = out.cell("CLIP")
    metal_index = out.find_layer(*_METAL)
    label_index = out.find_layer(*_LABEL)
    region = kdb.Region(clip_top.begin_shapes_rec(metal_index))
    assert region.count() == 2
    expected_bbox = kdb.Box(0, 0, 5000, 5000) + kdb.Box(10000, 10000, 12000, 12000)
    assert region.bbox() == expected_bbox
    texts = kdb.Texts(clip_top.begin_shapes_rec(label_index))
    assert texts.count() == 1
    (text,) = list(texts.each())
    assert text.string == "IN"


def test_region_clip_defaults_to_sole_top_cell(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    report = run_clip(str(fixture), str(out_path), region_um=(0.0, 0.0, 5.0, 5.0))

    assert report["top"] == "TOP"
    # The 0,0-5,5 um box (exactly matching the window) + the "IN" text.
    assert report["shape_count"] == 2


def test_region_clip_respects_explicit_top_among_multiple(tmp_path):
    fixture = tmp_path / "multi_top.gds"
    _build_multi_top_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    report = run_clip(
        str(fixture), str(out_path), region_um=(0.0, 0.0, 1.0, 1.0), top="TOP2"
    )

    assert report["top"] == "TOP2"
    assert report["shape_count"] == 1


def test_region_clip_ambiguous_top_without_top_flag_errors(tmp_path):
    fixture = tmp_path / "multi_top.gds"
    _build_multi_top_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    with pytest.raises(ClipError, match="has 2 top cells"):
        run_clip(str(fixture), str(out_path), region_um=(0.0, 0.0, 1.0, 1.0))


def test_region_clip_matching_no_geometry_errors(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    with pytest.raises(ClipError, match="matches no geometry"):
        run_clip(str(fixture), str(out_path), region_um=(900.0, 900.0, 901.0, 901.0))


def test_region_clip_degenerate_at_dbu_errors(tmp_path):
    """A region with positive area in micrometres can still round to zero
    width/height at the layout's own database unit -- a distinct error from
    "matches no geometry" (the window itself is degenerate, not merely
    unlucky about where it landed)."""
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    with pytest.raises(ClipError, match="zero width or height"):
        run_clip(str(fixture), str(out_path), region_um=(0.0, 0.0, 0.0000001, 5.0))


def test_region_clip_output_is_deterministic(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_a = tmp_path / "a.gds"
    out_b = tmp_path / "b.gds"

    run_clip(str(fixture), str(out_a), region_um=(0.0, 0.0, 15.0, 15.0))
    run_clip(str(fixture), str(out_b), region_um=(0.0, 0.0, 15.0, 15.0))

    assert out_a.read_bytes() == out_b.read_bytes()


# --- Cell-extraction mode ------------------------------------------------


def test_cell_extraction_preserves_nested_hierarchy(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "macro.gds"

    report = run_clip(str(fixture), str(out_path), cell="MACRO")

    assert report["mode"] == "cell"
    assert report["cell"] == "MACRO"
    assert report["region_um"] is None
    assert report["top"] is None
    assert report["cell_count"] == 2  # MACRO + SUBCELL
    assert report["shape_count"] == 2  # MACRO's own box + SUBCELL's box

    out = kdb.Layout()
    out.read(str(out_path))
    assert sorted(c.name for c in out.each_cell()) == ["MACRO", "SUBCELL"]

    macro = out.cell("MACRO")
    metal_index = out.find_layer(*_METAL)
    assert macro.shapes(metal_index).size() == 1
    (inst,) = list(macro.each_inst())
    # copy_tree preserves the subcell's own placement inside MACRO's local
    # frame (identity, in the fixture) -- independent of where MACRO itself
    # was instantiated inside the original TOP.
    assert out.cell(inst.cell_index).name == "SUBCELL"
    assert inst.trans == kdb.Trans()


def test_cell_extraction_works_for_a_non_top_cell(tmp_path):
    """`--cell` can name any cell in the stream, not just a top cell (MACRO
    is instantiated inside TOP in the fixture, never a top cell itself)."""
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)

    layout = kdb.Layout()
    layout.read(str(fixture))
    assert [c.name for c in layout.top_cells()] == ["TOP"]

    out_path = tmp_path / "macro.gds"
    report = run_clip(str(fixture), str(out_path), cell="MACRO")
    assert report["cell"] == "MACRO"


def test_cell_extraction_nonexistent_cell_errors(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    with pytest.raises(ClipError, match="cell 'NOPE' not found"):
        run_clip(str(fixture), str(out_path), cell="NOPE")


def test_cell_extraction_output_is_deterministic(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_a = tmp_path / "a.gds"
    out_b = tmp_path / "b.gds"

    run_clip(str(fixture), str(out_a), cell="MACRO")
    run_clip(str(fixture), str(out_b), cell="MACRO")

    assert out_a.read_bytes() == out_b.read_bytes()


# --- Mode validation ------------------------------------------------------


def test_neither_region_nor_cell_errors(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    with pytest.raises(ClipError, match="exactly one of"):
        run_clip(str(fixture), str(out_path))


def test_both_region_and_cell_errors(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    with pytest.raises(ClipError, match="exactly one of"):
        run_clip(
            str(fixture),
            str(out_path),
            region_um=(0.0, 0.0, 1.0, 1.0),
            cell="MACRO",
        )


def test_missing_file_errors(tmp_path):
    out_path = tmp_path / "out.gds"

    with pytest.raises(ClipError, match="file not found"):
        run_clip(str(tmp_path / "missing.gds"), str(out_path), cell="MACRO")


# --- CLI -------------------------------------------------------------------


def test_cli_region_mode_writes_output_and_reports_json(tmp_path, capsys):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "clipped.gds"

    exit_code = main(
        [
            "clip",
            str(fixture),
            "--region",
            "[0, 0, 15, 15]",
            "-o",
            str(out_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert out_path.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["mode"] == "region"
    assert payload["shape_count"] == 3


def test_cli_cell_mode_writes_output_and_reports_json(tmp_path, capsys):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "macro.gds"

    exit_code = main(
        [
            "clip",
            str(fixture),
            "--cell",
            "MACRO",
            "-o",
            str(out_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert out_path.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "cell"
    assert payload["cell"] == "MACRO"
    assert payload["cell_count"] == 2


def test_cli_text_format_reports_key_fields(tmp_path, capsys):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "macro.gds"

    exit_code = main(["clip", str(fixture), "--cell", "MACRO", "-o", str(out_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "mode: cell" in out
    assert "cell: MACRO" in out


def test_cli_nonexistent_cell_is_application_error(tmp_path, capsys):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    exit_code = main(
        [
            "clip",
            str(fixture),
            "--cell",
            "NOPE",
            "-o",
            str(out_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "clip"
    assert "not found" in error["error"]["message"]
    assert not out_path.exists()


def test_cli_region_matching_no_geometry_is_application_error(tmp_path, capsys):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    exit_code = main(
        [
            "clip",
            str(fixture),
            "--region",
            "[900, 900, 901, 901]",
            "-o",
            str(out_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert "matches no geometry" in error["error"]["message"]
    assert not out_path.exists()


def test_cli_degenerate_bbox_um_is_application_error(tmp_path, capsys):
    """A zero-area micrometre bbox (`right == left`) is caught by the shared
    `--region` parser (`load_region`), the same as `klt ring-check`/`klt
    components`."""
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    exit_code = main(
        [
            "clip",
            str(fixture),
            "--region",
            "[3, 3, 3, 3]",
            "-o",
            str(out_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert "right > left" in error["error"]["message"]


def test_cli_both_region_and_cell_is_usage_error(tmp_path, capsys):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "clip",
                str(fixture),
                "--region",
                "[0, 0, 1, 1]",
                "--cell",
                "MACRO",
                "-o",
                str(out_path),
            ]
        )
    assert exc_info.value.code == 2


def test_cli_neither_region_nor_cell_is_usage_error(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)
    out_path = tmp_path / "out.gds"

    with pytest.raises(SystemExit) as exc_info:
        main(["clip", str(fixture), "-o", str(out_path)])
    assert exc_info.value.code == 2


def test_cli_missing_output_flag_is_usage_error(tmp_path):
    fixture = tmp_path / "fixture.gds"
    _build_fixture(fixture)

    with pytest.raises(SystemExit) as exc_info:
        main(["clip", str(fixture), "--cell", "MACRO"])
    assert exc_info.value.code == 2


def test_cli_missing_file_is_application_error(tmp_path, capsys):
    out_path = tmp_path / "out.gds"

    exit_code = main(
        [
            "clip",
            str(tmp_path / "missing.gds"),
            "--cell",
            "MACRO",
            "-o",
            str(out_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert "file not found" in error["error"]["message"]
