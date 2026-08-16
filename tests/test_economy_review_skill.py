"""Tests for `.claude/skills/economy-review/scripts/economy_metrics.py`.

This script is a **placeholder**, not part of the installed `klayout_tools`
package (deliberately kept out of `src/` -- see the script's own module
docstring for why) -- so it is loaded the same way `tests/test_ingest_canary.py`
loads `scripts/ingest-canary.py`: `importlib.util.spec_from_file_location`
against its path, not a regular import.

Fixtures are generated programmatically with `klayout.db`, mirroring
`test_render.py`'s approach: an unfilled reserved-annotation-range layer
(990/0, per `klayout_tools._annotation`) draws the notional "die"/floorplan
extent without contributing to the utilization/whitespace numbers, and an
ordinary device layer (1/0) draws the "real" geometry under test -- this
lets each test control bbox size and drawn-area independently, deterministically.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent
    / ".claude"
    / "skills"
    / "economy-review"
    / "scripts"
    / "economy_metrics.py"
)


def _load_economy_metrics():
    spec = importlib.util.spec_from_file_location("economy_metrics", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["economy_metrics"] = module
    spec.loader.exec_module(module)
    return module


em = _load_economy_metrics()


def _write_layout(
    tmp_path: Path,
    *,
    die_box_um: tuple[float, float, float, float],
    device_boxes_um: list[tuple[float, float, float, float]],
    filename: str = "layout.gds",
    second_top: bool = False,
) -> Path:
    """Write a synthetic GDS: an unfilled die-boundary layer (990/0, a
    reserved annotation layer) plus one or more device shapes on layer
    (1, 0)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    boundary_layer = layout.layer(990, 0)
    x0, y0, x1, y1 = die_box_um
    top.shapes(boundary_layer).insert(kdb.DBox(x0, y0, x1, y1).to_itype(layout.dbu))

    device_layer = layout.layer(1, 0)
    for bx0, by0, bx1, by1 in device_boxes_um:
        top.shapes(device_layer).insert(
            kdb.DBox(bx0, by0, bx1, by1).to_itype(layout.dbu)
        )

    if second_top:
        layout.create_cell("TOP2")

    path = tmp_path / filename
    layout.write(str(path))
    return path


class TestUtilizationAndBbox:
    def test_full_coverage_layout_has_near_full_utilization(self, tmp_path):
        path = _write_layout(
            tmp_path,
            die_box_um=(0, 0, 100, 50),
            device_boxes_um=[(0, 0, 100, 50)],
        )
        metrics = em.compute_economy_metrics(str(path))

        assert metrics["top"] == "TOP"
        assert metrics["bbox_area_um2"] == pytest.approx(5000.0, rel=1e-6)
        assert metrics["drawn_area_um2"] == pytest.approx(5000.0, rel=1e-6)
        assert metrics["utilization"] == pytest.approx(1.0, rel=1e-6)
        assert metrics["empty_regions"] == []
        for margin in metrics["dead_margins_um"].values():
            assert margin == pytest.approx(0.0, abs=1e-6)

    def test_small_centered_device_has_low_utilization_and_margins(self, tmp_path):
        path = _write_layout(
            tmp_path,
            die_box_um=(0, 0, 100, 100),
            device_boxes_um=[(45, 45, 55, 55)],  # 10x10 device in a 100x100 die
        )
        metrics = em.compute_economy_metrics(str(path), grid_cols=10, grid_rows=10)

        assert metrics["bbox_area_um2"] == pytest.approx(10000.0, rel=1e-6)
        assert metrics["drawn_area_um2"] == pytest.approx(100.0, rel=1e-6)
        assert metrics["utilization"] == pytest.approx(0.01, rel=1e-6)

        # A 10x10 device centered in a 100x100 die with a 10x10 grid leaves
        # a wide empty margin on every edge.
        margins = metrics["dead_margins_um"]
        assert margins["left"] > 30
        assert margins["right"] > 30
        assert margins["bottom"] > 30
        assert margins["top"] > 30

        assert len(metrics["empty_regions"]) >= 1
        # The largest empty region should be reported first (sorted by area).
        largest = metrics["empty_regions"][0]
        assert largest["cell_count"] >= 1

    def test_aspect_ratio_and_width_height(self, tmp_path):
        path = _write_layout(
            tmp_path,
            die_box_um=(0, 0, 200, 50),
            device_boxes_um=[(0, 0, 200, 50)],
        )
        metrics = em.compute_economy_metrics(str(path))
        assert metrics["width_um"] == pytest.approx(200.0)
        assert metrics["height_um"] == pytest.approx(50.0)
        assert metrics["aspect_ratio"] == pytest.approx(4.0)

    def test_annotation_layer_excluded_from_drawn_area(self, tmp_path):
        """A large shape drawn on the reserved 990-999 annotation range must
        not count toward `drawn_area_um2`/`utilization`, even though it does
        set the cell's bbox (matching `klt layers`/`klt render`'s existing
        annotation-layer handling)."""
        layout = kdb.Layout()
        layout.dbu = 0.001
        top = layout.create_cell("TOP")
        boundary_layer = layout.layer(990, 0)
        # A *filled* boundary shape spanning the whole die -- if this were
        # wrongly counted as "drawn", utilization would read ~1.0.
        top.shapes(boundary_layer).insert(kdb.DBox(0, 0, 100, 100).to_itype(layout.dbu))
        device_layer = layout.layer(1, 0)
        top.shapes(device_layer).insert(kdb.DBox(0, 0, 10, 10).to_itype(layout.dbu))
        path = tmp_path / "layout.gds"
        layout.write(str(path))

        metrics = em.compute_economy_metrics(str(path))
        assert metrics["drawn_area_um2"] == pytest.approx(100.0, rel=1e-6)
        assert metrics["utilization"] == pytest.approx(0.01, rel=1e-6)
        assert "990/0" in metrics["provenance"]["annotation_layers_excluded"]


class TestErrorHandling:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(em.EconomyMetricsError, match="file not found"):
            em.compute_economy_metrics(str(tmp_path / "nope.gds"))

    def test_multi_top_cell_requires_explicit_top(self, tmp_path):
        path = _write_layout(
            tmp_path,
            die_box_um=(0, 0, 10, 10),
            device_boxes_um=[(0, 0, 10, 10)],
            second_top=True,
        )
        with pytest.raises(em.EconomyMetricsError, match="more than one top cell"):
            em.compute_economy_metrics(str(path))

        # Passing --top explicitly resolves the ambiguity.
        metrics = em.compute_economy_metrics(str(path), top="TOP")
        assert metrics["top"] == "TOP"

    def test_unknown_top_cell_name_raises(self, tmp_path):
        path = _write_layout(
            tmp_path, die_box_um=(0, 0, 10, 10), device_boxes_um=[(0, 0, 10, 10)]
        )
        with pytest.raises(em.EconomyMetricsError, match="top cell not found"):
            em.compute_economy_metrics(str(path), top="NOPE")

    def test_empty_top_cell_raises(self, tmp_path):
        layout = kdb.Layout()
        layout.dbu = 0.001
        layout.create_cell("TOP")
        path = tmp_path / "empty.gds"
        layout.write(str(path))
        with pytest.raises(em.EconomyMetricsError, match="no geometry"):
            em.compute_economy_metrics(str(path))

    def test_invalid_grid_raises(self, tmp_path):
        path = _write_layout(
            tmp_path, die_box_um=(0, 0, 10, 10), device_boxes_um=[(0, 0, 10, 10)]
        )
        with pytest.raises(em.EconomyMetricsError, match="grid must be positive"):
            em.compute_economy_metrics(str(path), grid_cols=0, grid_rows=4)

    def test_parse_grid_rejects_malformed_value(self):
        with pytest.raises(em.EconomyMetricsError, match="COLSxROWS"):
            em._parse_grid("bogus")

    def test_parse_grid_accepts_colsxrows(self):
        assert em._parse_grid("8x6") == (8, 6)


class TestCli:
    def test_json_output_matches_library_call(self, tmp_path):
        path = _write_layout(
            tmp_path,
            die_box_um=(0, 0, 100, 100),
            device_boxes_um=[(40, 40, 60, 60)],
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(path), "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["placeholder"] is True
        assert payload["top"] == "TOP"
        assert payload["utilization"] == pytest.approx(0.04, rel=1e-6)

    def test_text_output_is_human_readable(self, tmp_path):
        path = _write_layout(
            tmp_path, die_box_um=(0, 0, 10, 10), device_boxes_um=[(0, 0, 10, 10)]
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "utilization:" in result.stdout
        assert "bbox_area_um2:" in result.stdout

    def test_missing_file_exits_nonzero_with_clean_error(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(tmp_path / "nope.gds")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr
