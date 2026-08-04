"""Tests for `klt layout-metrics` and the `layout_metrics_report` library
function.

Fixtures are generated programmatically with `klayout.db` inside the tests —
no dependency on an external corpus, mirroring `tests/test_cells.py`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.layout_metrics import (
    LayoutMetricsError,
    emit_layout_json,
    layout_metrics_report,
)

# A real sky130_fd_sc_hd GCD macro produced end to end by `klt synthesize` +
# `klt place-and-route` (real Yosys + real OpenROAD) -- the first exercise of
# `klt layout-metrics` against machine-generated, macro-scale layout rather
# than the hand-drawn analog fixtures above. See tests/corpus/README.md's
# "Machine-generated macro-scale fixture" section for full provenance.
CORPUS_DIR = Path(__file__).parent / "corpus"
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"

# poly.width.1 (sky130 deck): minimum poly width is 150 dbu (0.15 um).
_POLY_WIDTH_THRESHOLD_DBU = 150


def _make_layout() -> kdb.Layout:
    """A small two-cell layout: TOP instantiates SUB twice."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")

    m1 = layout.layer(1, 0)
    top.shapes(m1).insert(kdb.Box(0, 0, 10, 10))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans()))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(kdb.Vector(100, 0))))
    sub.shapes(m1).insert(kdb.Box(0, 0, 5, 5))

    return layout


def _make_violation_layout() -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 50, 2000))
    return layout


def _write_block(
    tmp_path,
    *,
    slug: str = "example-block",
    layout_name: str = "layout.gds",
    layout: kdb.Layout | None = None,
    meta: dict | None = None,
    renders: list[str] | None = None,
):
    block_dir = tmp_path / slug
    block_dir.mkdir()

    if layout is not None:
        (layout).write(str(block_dir / layout_name))

    if meta is not None:
        (block_dir / "meta.json").write_text(json.dumps(meta))

    if renders:
        renders_dir = block_dir / "output" / "renders"
        renders_dir.mkdir(parents=True)
        for name in renders:
            (renders_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n")

    return block_dir


# --- library function: ok / status semantics --------------------------------


def test_report_ok_status_and_counts(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())

    report = layout_metrics_report(str(block_dir))

    assert report["schema_version"] == 1
    assert report["slug"] == "example-block"
    assert report["name"] == "Example Block"
    assert report["layout_file"] == "layout.gds"
    assert report["layer_count"] == 1
    assert report["cell_count"] == 2
    assert report["instance_count"] == 2
    assert report["status"] == "ok"
    assert "drc" not in report
    assert "renders" not in report
    assert "description" not in report


def test_report_no_artifacts_when_no_layout_file(tmp_path):
    block_dir = tmp_path / "empty-block"
    block_dir.mkdir()

    report = layout_metrics_report(str(block_dir))

    assert report["slug"] == "empty-block"
    assert report["name"] == "Empty Block"
    assert report["status"] == "no_artifacts"
    assert "layout_file" not in report
    assert "layer_count" not in report
    assert "cell_count" not in report
    assert "instance_count" not in report
    assert "drc" not in report


def test_report_prefers_layout_gds_name(tmp_path):
    block_dir = tmp_path / "example-block"
    block_dir.mkdir()
    _make_layout().write(str(block_dir / "layout.gds"))
    # A second, non-preferred .gds file must not be picked over layout.gds.
    _make_layout().write(str(block_dir / "other.gds"))

    report = layout_metrics_report(str(block_dir))
    assert report["layout_file"] == "layout.gds"


def test_report_falls_back_to_single_gds_file(tmp_path):
    block_dir = tmp_path / "example-block"
    block_dir.mkdir()
    _make_layout().write(str(block_dir / "design.gds"))

    report = layout_metrics_report(str(block_dir))
    assert report["layout_file"] == "design.gds"
    assert report["status"] == "ok"


def test_report_oasis_layout_file(tmp_path):
    block_dir = tmp_path / "example-block"
    block_dir.mkdir()
    _make_layout().write(str(block_dir / "layout.oas"))

    report = layout_metrics_report(str(block_dir))
    assert report["layout_file"] == "layout.oas"
    assert report["status"] == "ok"


def test_report_partial_status_on_corrupt_layout(tmp_path):
    block_dir = tmp_path / "example-block"
    block_dir.mkdir()
    (block_dir / "layout.gds").write_text("this is not a gds stream\n" * 4)

    report = layout_metrics_report(str(block_dir))
    assert report["status"] == "partial"
    assert "layer_count" not in report
    assert "cell_count" not in report


# --- meta.json ----------------------------------------------------------


def test_report_uses_meta_json_name_and_description(tmp_path):
    block_dir = _write_block(
        tmp_path,
        layout=_make_layout(),
        meta={"name": "Custom Name", "description": "A test block."},
    )

    report = layout_metrics_report(str(block_dir))
    assert report["name"] == "Custom Name"
    assert report["description"] == "A test block."


def test_report_ignores_malformed_meta_json(tmp_path):
    block_dir = tmp_path / "example-block"
    block_dir.mkdir()
    _make_layout().write(str(block_dir / "layout.gds"))
    (block_dir / "meta.json").write_text("{not valid json")

    report = layout_metrics_report(str(block_dir))
    assert report["name"] == "Example Block"
    assert "description" not in report


# --- renders --------------------------------------------------------------


def test_report_lists_renders_when_present(tmp_path):
    block_dir = _write_block(
        tmp_path,
        layout=_make_layout(),
        renders=["metal1.png", "poly.png"],
    )

    report = layout_metrics_report(str(block_dir))
    assert report["renders"] == {
        "metal1": "renders/metal1.png",
        "poly": "renders/poly.png",
    }


def test_report_omits_renders_when_absent(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())
    report = layout_metrics_report(str(block_dir))
    assert "renders" not in report


def test_report_renders_present_even_when_no_artifacts(tmp_path):
    block_dir = _write_block(tmp_path, renders=["overview.png"])
    report = layout_metrics_report(str(block_dir))
    assert report["status"] == "no_artifacts"
    assert report["renders"] == {"overview": "renders/overview.png"}


# --- drc --------------------------------------------------------------------


def test_report_includes_drc_when_deck_given(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_violation_layout())

    report = layout_metrics_report(str(block_dir), deck="sky130")
    assert report["drc"] == {
        "deck": "sky130",
        "status": "violations",
        "violation_count": 1,
    }


def test_report_omits_drc_without_deck(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_violation_layout())
    report = layout_metrics_report(str(block_dir))
    assert "drc" not in report


def test_report_omits_drc_on_unsupported_deck_without_affecting_status(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())
    report = layout_metrics_report(str(block_dir), deck="not-a-real-deck")
    assert "drc" not in report
    assert report["status"] == "ok"


# --- error path: bad block_dir -----------------------------------------


def test_report_raises_on_missing_block_dir():
    with pytest.raises(LayoutMetricsError):
        layout_metrics_report("/no/such/block/dir")


def test_report_raises_when_block_dir_is_a_file(tmp_path):
    not_a_dir = tmp_path / "notadir"
    not_a_dir.write_text("hi")
    with pytest.raises(LayoutMetricsError):
        layout_metrics_report(str(not_a_dir))


# --- emit_layout_json ---------------------------------------------------


def test_emit_layout_json_writes_default_path(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())

    written = emit_layout_json(str(block_dir))
    assert written == block_dir / "output" / "layout.json"
    assert written.is_file()

    data = json.loads(written.read_text())
    assert data["slug"] == "example-block"
    assert data["status"] == "ok"


def test_emit_layout_json_respects_output_override(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())
    custom = tmp_path / "custom" / "out.json"

    written = emit_layout_json(str(block_dir), output_path=str(custom))
    assert written == custom
    assert written.is_file()


# --- CLI ------------------------------------------------------------------


def test_cli_writes_layout_json(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())

    assert main(["layout-metrics", str(block_dir)]) == 0
    written = block_dir / "output" / "layout.json"
    assert written.is_file()
    data = json.loads(written.read_text())
    assert data["status"] == "ok"


def test_cli_dry_run_does_not_write(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())

    assert main(["layout-metrics", str(block_dir), "--dry-run"]) == 0
    assert not (block_dir / "output" / "layout.json").exists()


def test_cli_json_format_matches_written_file(tmp_path, capsys):
    block_dir = _write_block(tmp_path, layout=_make_layout())

    assert main(["layout-metrics", str(block_dir), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    written = json.loads((block_dir / "output" / "layout.json").read_text())
    assert data == written


def test_cli_default_format_is_text(tmp_path, capsys):
    block_dir = _write_block(tmp_path, layout=_make_layout())

    assert main(["layout-metrics", str(block_dir)]) == 0
    out = capsys.readouterr().out
    assert "slug: example-block" in out
    assert "status: ok" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_missing_block_dir(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert main(["layout-metrics", str(missing)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt layout-metrics" in captured.err
    assert "not found" in captured.err


def test_cli_missing_block_dir_json_format(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert main(["layout-metrics", str(missing), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "layout-metrics"
    assert "not found" in error["error"]["message"]


def test_cli_requires_block_argument():
    with pytest.raises(SystemExit):
        main(["layout-metrics"])


def test_cli_output_override(tmp_path):
    block_dir = _write_block(tmp_path, layout=_make_layout())
    custom = tmp_path / "custom.json"

    assert main(["layout-metrics", str(block_dir), "--output", str(custom)]) == 0
    assert custom.is_file()
    assert not (block_dir / "output" / "layout.json").exists()


# --------------------------------------------------------------------------- #
# OpenROAD-produced, macro-scale standard-cell fixture (issue #436)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)
def test_openroad_gcd_fixture_report(tmp_path):
    """`klt layout-metrics` against the real, OpenROAD-produced GCD
    macro-scale fixture required no verb-side fix (#436) -- it never
    recomputes metrics itself (see module docstring), so this also confirms
    `klt layers`/`klt cells`/`klt drc` all stay well-formed when called
    through this aggregator against thousands of instances."""
    block_dir = tmp_path / "gcd-block"
    block_dir.mkdir()
    # `_find_layout_file` globs `*.gds.gz` directly (see module docstring) --
    # no test-time decompression needed, matching `run_drc`'s own direct
    # `.gds.gz` support exercised in test_drc.py.
    shutil.copy(PLACE_AND_ROUTE_GDS, block_dir / "layout.gds.gz")

    report = layout_metrics_report(str(block_dir), deck="sky130")

    assert report["schema_version"] == 1
    assert report["slug"] == "gcd-block"
    assert report["layout_file"] == "layout.gds.gz"
    assert report["status"] == "ok"
    assert report["layer_count"] == 31
    assert report["cell_count"] == 69
    assert report["instance_count"] == 3645
    assert report["drc"] == {
        "deck": "sky130",
        "status": "violations",
        "violation_count": 4,
    }
