"""Tests for `klt economy` and the `economy_report` library function.

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus (mirrors `tests/test_stats.py`'s
convention) for the bulk of this file. Every fixture's expected numbers are
hand-computed in each test's own docstring/comments so a failure is easy to
diagnose against the geometry that produced it -- not just "the number
changed".

`TestRealCanaries` at the bottom instead runs against two real,
committed-to-repo GDS files -- the same two the `economy-review` skill's
placeholder script (issue #1013, PR #1024) validated its own utilization/
margin math against -- and asserts this module's numbers against that
already-manually-cross-checked evidence (see the PR body for how those
numbers were independently confirmed against `blocks/sky130-bandgap`'s own
`NOTE.md` ground truth).
"""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.economy import EconomyError, economy_report

# --- utilization / whitespace / tightness / margins -------------------------


def _make_density_layout() -> kdb.Layout:
    """A single-cell layout with a known bbox, drawn area, and margins.

    - Layer (1, 0): a 1000x1000 (dbu) box at the origin -- 1.0 um^2 of real
      drawn content occupying the bottom-left quarter of the eventual bbox.
    - Layer (994, 0) (reserved annotation range): a 2000x2000 box at the
      origin -- excluded from every drawn-area computation, but *does*
      widen the reported `bbox_um` (computed over ALL layers) to 2.0 x 2.0
      um, since a real layout's declared extent often comes from a
      full-size boundary/reservation layer, not just active geometry.

    Hand-computed expectations (dbu = 0.001 um, matching `stats.py`'s test
    fixtures):

    - `bbox_um`: (0, 0) - (2.0, 2.0) -- bbox_area_um2 = 4.0.
    - `drawn_area_um2` = 1.0 (only layer (1,0); the annotation layer is
      excluded) -> `utilization` = 0.25.
    - `tight_bbox_um` = (0, 0) - (1.0, 1.0) -- the bbox of the drawn region
      alone -> `tight_bbox_area_um2` = 1.0 -> `bbox_tightness` = 0.25.
    - `dead_margins_um`: drawn content touches the left and bottom edges
      (margin 0 on both), and the empty band from x=1.0 to x=2.0 / y=1.0 to
      y=2.0 is exactly 1.0 um wide/tall on the right/top -- see the
      grid-band-walk arithmetic in `test_dead_margins_um` below for the
      exact (non-approximate) derivation against the fixed internal 8x6
      margin grid.
    - `largest_empty_regions`: bbox minus the drawn square is a single
      connected Gamma/L-shaped region (simply connected, one polygon) of
      area 4.0 - 1.0 = 3.0 um^2, whose own bounding box is the full (0,0)-
      (2.0,2.0) bbox (it touches every edge) -> `fill_fraction` = 3.0/4.0
      = 0.75.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    active = layout.layer(1, 0)
    layout.set_info(active, kdb.LayerInfo(1, 0, "active"))
    top.shapes(active).insert(kdb.Box(0, 0, 1000, 1000))

    annotation = layout.layer(994, 0)
    top.shapes(annotation).insert(kdb.Box(0, 0, 2000, 2000))

    return layout


def test_bbox_and_utilization(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path))

    assert report["file"] == str(path)
    assert report["top"] == "TOP"
    assert report["dbu_um"] == 0.001
    assert report["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 2.0,
        "top": 2.0,
        "width": 2.0,
        "height": 2.0,
    }
    assert report["bbox_area_um2"] == pytest.approx(4.0)
    assert report["bbox_area_mm2"] == pytest.approx(4.0e-6)
    assert report["aspect_ratio"] == pytest.approx(1.0)
    assert report["drawn_area_um2"] == pytest.approx(1.0)
    assert report["utilization"] == pytest.approx(0.25)
    assert report["whitespace_area_um2"] == pytest.approx(3.0)
    assert report["whitespace_fraction"] == pytest.approx(0.75)
    # The reserved 990-999 annotation layer is excluded from drawn area but
    # still recorded as excluded.
    assert report["annotation_layers_excluded"] == ["994/0"]


def test_bbox_tightness(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path))

    assert report["tight_bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 1.0,
        "top": 1.0,
        "width": 1.0,
        "height": 1.0,
    }
    assert report["tight_bbox_area_um2"] == pytest.approx(1.0)
    assert report["bbox_tightness"] == pytest.approx(0.25)


def test_dead_margins_um(tmp_path):
    """Grid-band-walk margins over the fixed internal 8x6 margin grid.

    Column width = 2.0/8 = 0.25 um; row height = 2.0/6 = 1/3 um. The drawn
    1.0x1.0 um square exactly covers grid columns 0-3 and rows 0-2 (both
    boundaries land exactly on grid lines: 4*0.25 = 1.0, 3*(1/3) = 1.0).

    - `left`: column 0's mean covered fraction (averaged over its 6 rows,
      3 fully covered + 3 empty) is 0.5 >= the 0.2 threshold -> the walk
      stops immediately -> margin 0.0.
    - `bottom`: symmetric -> margin 0.0.
    - `right`: columns 7, 6, 5, 4 are entirely empty (mean 0.0 each) before
      column 3 (mean 0.5) stops the walk -> margin = 4 * 0.25 = 1.0 um.
    - `top`: symmetric over rows -> margin = 3 * (1/3) = 1.0 um.
    """
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path))

    margins = report["dead_margins_um"]
    assert margins["left"] == pytest.approx(0.0)
    assert margins["bottom"] == pytest.approx(0.0)
    assert margins["right"] == pytest.approx(1.0)
    assert margins["top"] == pytest.approx(1.0)


def test_largest_empty_regions(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path))

    assert report["empty_region_count"] == 1
    regions = report["largest_empty_regions"]
    assert len(regions) == 1
    region = regions[0]
    assert region["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 2.0,
        "top": 2.0,
        "width": 2.0,
        "height": 2.0,
    }
    assert region["area_um2"] == pytest.approx(3.0)
    assert region["fill_fraction"] == pytest.approx(0.75)


def test_largest_empty_regions_capped(tmp_path):
    """`--max-empty-regions` caps the returned list but not `empty_region_count`."""
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path), max_empty_regions=1)
    assert len(report["largest_empty_regions"]) == 1
    assert report["empty_region_count"] == 1  # only 1 empty region exists here


def test_whitespace_grid_shape(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path), grid_cols=2, grid_rows=2)

    grid = report["whitespace_grid"]
    assert grid["cols"] == 2
    assert grid["rows"] == 2
    assert len(grid["cells"]) == 4

    by_rc = {(c["row"], c["col"]): c for c in grid["cells"]}
    # Bottom-left quadrant (0,0) is exactly the drawn square -> fully covered.
    assert by_rc[(0, 0)]["covered_fraction"] == pytest.approx(1.0)
    assert by_rc[(0, 0)]["whitespace_fraction"] == pytest.approx(0.0)
    # The other three quadrants are entirely empty.
    for rc in ((0, 1), (1, 0), (1, 1)):
        assert by_rc[rc]["covered_fraction"] == pytest.approx(0.0)
        assert by_rc[rc]["whitespace_fraction"] == pytest.approx(1.0)


# --- per-cell utilization -----------------------------------------------


def _make_hierarchy_layout() -> kdb.Layout:
    """TOP draws its own local content and instantiates SUB elsewhere.

    - TOP's own local shape: a 2000x1000 box (2.0 x 1.0 um, area 2.0 um^2)
      at the origin -- fully fills its own local bbox (utilization 1.0).
    - SUB's own local shape: a 500x500 box (0.5 x 0.5 um, area 0.25 um^2)
      at its own origin -- also fully fills its own local bbox.
    - TOP instantiates SUB far away (at (10000, 10000) dbu), so the
      *flattened* top-level utilization is diluted by SUB's placement
      inflating the overall bbox, while each cell's own *local* utilization
      (own shapes over own local bbox) stays 1.0 regardless of where SUB
      was placed -- this is exactly the distinction `cells[]` is meant to
      preserve.
    - EMPTY is a third cell with no shapes of its own (a pure wrapper),
      instantiated inside SUB so it stays out of TOP's own hierarchy top-
      cell-ness -- included to verify it is omitted from `cells[]`
      (utilization is undefined for a cell that draws nothing).
    """
    layout = kdb.Layout()
    li = layout.layer(1, 0)

    top = layout.create_cell("TOP")
    sub = layout.create_cell("SUB")
    empty = layout.create_cell("EMPTY")

    top.shapes(li).insert(kdb.Box(0, 0, 2000, 1000))
    sub.shapes(li).insert(kdb.Box(0, 0, 500, 500))
    sub.insert(kdb.CellInstArray(empty.cell_index(), kdb.Trans()))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(kdb.Vector(10000, 10000))))

    return layout


def test_per_cell_utilization(tmp_path):
    path = tmp_path / "hierarchy.gds"
    _make_hierarchy_layout().write(str(path))

    report = economy_report(str(path))

    cells = {entry["name"]: entry for entry in report["cells"]}
    # EMPTY draws nothing locally -> omitted.
    assert set(cells) == {"TOP", "SUB"}

    assert cells["TOP"]["bbox_area_um2"] == pytest.approx(2.0)
    assert cells["TOP"]["drawn_area_um2"] == pytest.approx(2.0)
    assert cells["TOP"]["utilization"] == pytest.approx(1.0)

    assert cells["SUB"]["bbox_area_um2"] == pytest.approx(0.25)
    assert cells["SUB"]["drawn_area_um2"] == pytest.approx(0.25)
    assert cells["SUB"]["utilization"] == pytest.approx(1.0)

    # cells[] is sorted by name.
    assert [entry["name"] for entry in report["cells"]] == ["SUB", "TOP"]


def test_top_level_utilization_includes_flattened_children(tmp_path):
    """The top-level (flattened) utilization is diluted by SUB's placement,
    unlike each cell's own local utilization (both 1.0, see above)."""
    path = tmp_path / "hierarchy.gds"
    _make_hierarchy_layout().write(str(path))

    report = economy_report(str(path))

    # Flattened drawn area = TOP's own 2.0 um^2 + SUB's own 0.25 um^2.
    assert report["drawn_area_um2"] == pytest.approx(2.25)
    # bbox spans from TOP's own box origin out to SUB's placed extent.
    assert report["utilization"] < 1.0


# --- row utilization ------------------------------------------------------


def _make_row_layout(rows: int = 2, cols: int = 8) -> kdb.Layout:
    """A std-cell-row-style placement: `rows` rows of `cols` identical
    10x20 (dbu) cells each, spaced with a 2 dbu gap between adjacent cells
    in a row (row pitch exactly 20 dbu, no inter-row gap).

    Per-row utilization = (cols * 10 * 20) / ((cols * 12 - 2) * 20)
    -- for cols=8: (8*200) / (94*20) = 1600/1880 = 0.851063829787234...
    """
    layout = kdb.Layout()
    li = layout.layer(1, 0)

    top = layout.create_cell("TOP")
    std = layout.create_cell("STD_A")
    std.shapes(li).insert(kdb.Box(0, 0, 10, 20))

    for row in range(rows):
        for col in range(cols):
            x = col * 12
            y = row * 20
            top.insert(kdb.CellInstArray(std.cell_index(), kdb.Trans(kdb.Vector(x, y))))

    return layout


def test_row_utilization_detected(tmp_path):
    path = tmp_path / "rows.gds"
    _make_row_layout(rows=2, cols=8).write(str(path))

    report = economy_report(str(path))

    row_util = report["row_utilization"]
    assert row_util is not None
    assert row_util["row_count"] == 2
    expected_row_utilization = 1600 / 1880
    assert row_util["mean_utilization"] == pytest.approx(expected_row_utilization)
    for row in row_util["rows"]:
        assert row["instance_count"] == 8
        assert row["utilization"] == pytest.approx(expected_row_utilization)


def test_row_utilization_none_for_non_digital_layout(tmp_path):
    """Two large, unrelated, non-row-aligned instances (an analog-style
    layout) must not be misdetected as a std-cell row."""
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    top = layout.create_cell("TOP")
    a = layout.create_cell("BLOCK_A")
    b = layout.create_cell("BLOCK_B")
    a.shapes(li).insert(kdb.Box(0, 0, 5000, 3000))
    b.shapes(li).insert(kdb.Box(0, 0, 8000, 4000))
    top.insert(kdb.CellInstArray(a.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    # Placed to overlap `a` -- a legal std-cell row can never overlap
    # itself, so this must be rejected rather than reported with an
    # impossible (>1.0) utilization.
    top.insert(kdb.CellInstArray(b.cell_index(), kdb.Trans(kdb.Vector(1000, 500))))

    path = tmp_path / "analog.gds"
    layout.write(str(path))

    report = economy_report(str(path))
    assert report["row_utilization"] is None


def test_row_utilization_none_with_too_few_instances(tmp_path):
    path = tmp_path / "single_instance.gds"
    _make_hierarchy_layout().write(str(path))

    report = economy_report(str(path))
    # TOP has exactly one direct instance (SUB) -- below _MIN_ROW_MEMBERS.
    assert report["row_utilization"] is None


# --- budget / reference ----------------------------------------------------


def test_budget_pass(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path), budget_um2=10.0)
    budget = report["budget"]
    assert budget["budget_um2"] == pytest.approx(10.0)
    assert budget["actual_um2"] == pytest.approx(4.0)
    assert budget["margin_um2"] == pytest.approx(6.0)
    assert budget["margin_fraction"] == pytest.approx(0.6)
    assert budget["status"] == "pass"


def test_budget_fail(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path), budget_um2=2.0)
    budget = report["budget"]
    assert budget["status"] == "fail"
    assert budget["margin_um2"] == pytest.approx(-2.0)


def test_budget_absent_by_default(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path))
    assert "budget" not in report


def test_reference_ratio(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path), reference_area_um2=2.0)
    reference = report["reference"]
    assert reference["reference_area_um2"] == pytest.approx(2.0)
    assert reference["actual_area_um2"] == pytest.approx(4.0)
    assert reference["ratio"] == pytest.approx(2.0)


# --- provenance / determinism ----------------------------------------------


def test_provenance_embeds_input_hash(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    report = economy_report(str(path))
    provenance = report["provenance"]
    assert provenance["input"]["content_hash"].startswith("sha256:")
    assert provenance["klt_version"] is not None
    assert provenance["klayout_version"] is not None
    assert provenance["pdk"] is None
    assert provenance["deck"] is None


def test_deterministic(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    first = json.dumps(economy_report(str(path)), sort_keys=True)
    second = json.dumps(economy_report(str(path)), sort_keys=True)
    assert first == second


# --- error paths -------------------------------------------------------


def test_multiple_top_cells_raises(tmp_path):
    layout = kdb.Layout()
    a = layout.create_cell("A")
    b = layout.create_cell("B")
    a.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 100, 100))
    b.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 100, 100))
    path = tmp_path / "multi_top.gds"
    layout.write(str(path))

    with pytest.raises(EconomyError, match="multiple top cells"):
        economy_report(str(path))


def test_top_selects_named_cell(tmp_path):
    layout = kdb.Layout()
    a = layout.create_cell("A")
    b = layout.create_cell("B")
    a.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 100, 100))
    b.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 200, 200))
    path = tmp_path / "multi_top.gds"
    layout.write(str(path))

    report = economy_report(str(path), top="B")
    assert report["top"] == "B"
    assert report["bbox_area_um2"] == pytest.approx(0.04)


def test_top_not_found_raises(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    with pytest.raises(EconomyError, match="top cell not found"):
        economy_report(str(path), top="NOPE")


def test_no_cells_raises(tmp_path):
    path = tmp_path / "empty.gds"
    kdb.Layout().write(str(path))

    with pytest.raises(EconomyError, match="no cells"):
        economy_report(str(path))


def test_top_cell_with_no_geometry_raises(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("EMPTY_TOP")
    path = tmp_path / "empty_top.gds"
    layout.write(str(path))

    with pytest.raises(EconomyError, match="no geometry"):
        economy_report(str(path))


def test_non_positive_grid_raises(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    with pytest.raises(EconomyError, match="grid must be positive"):
        economy_report(str(path), grid_cols=0)
    with pytest.raises(EconomyError, match="grid must be positive"):
        economy_report(str(path), grid_rows=-1)


def test_non_positive_max_empty_regions_raises(tmp_path):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    with pytest.raises(EconomyError, match="max_empty_regions must be positive"):
        economy_report(str(path), max_empty_regions=0)


# --- CLI ---------------------------------------------------------------


def test_cli_json_format(tmp_path, capsys):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    assert main(["economy", str(path), "--format", "json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["top"] == "TOP"
    assert payload["utilization"] == pytest.approx(0.25)


def test_cli_text_format(tmp_path, capsys):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    assert main(["economy", str(path)]) == 0
    captured = capsys.readouterr()
    assert "top: TOP" in captured.out
    assert "utilization: 0.2500" in captured.out


def test_cli_budget_flag(tmp_path, capsys):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    assert main(["economy", str(path), "--budget-um2", "1.0", "--format", "json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["budget"]["status"] == "fail"


def test_cli_grid_and_max_empty_regions_flags(tmp_path, capsys):
    path = tmp_path / "density.gds"
    _make_density_layout().write(str(path))

    assert (
        main(
            [
                "economy",
                str(path),
                "--grid-cols",
                "2",
                "--grid-rows",
                "2",
                "--max-empty-regions",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["whitespace_grid"]["cols"] == 2
    assert payload["whitespace_grid"]["rows"] == 2
    assert len(payload["largest_empty_regions"]) == 1


# --- real canary tops (issue #1012 acceptance criterion) -------------------

REPO_ROOT = Path(__file__).parent.parent
BANDGAP_GDS = (
    REPO_ROOT / "blocks" / "sky130-bandgap" / "output" / "bandgap_core_routed.gds"
)
BUF4_GDS = (
    REPO_ROOT
    / "blocks"
    / "sky130_fd_sc_hd__buf_4"
    / "output"
    / "sky130_fd_sc_hd__buf_4.gds"
)


@pytest.mark.skipif(not BANDGAP_GDS.is_file(), reason="canary GDS not present")
class TestBandgapCanary:
    """`blocks/sky130-bandgap` -- a known-loose analog block (its own
    `NOTE.md` already documents "area is over the ratified budget pending
    DR-007"). Numbers cross-checked against the `economy-review` skill's
    placeholder script's own committed evidence,
    `evidence/economy-review/sky130-bandgap/*-metrics.json` (PR #1024) --
    computed by an independent implementation of the same utilization/
    margin math, so agreement here is a real cross-check, not a tautology.
    """

    def test_utilization_and_bbox(self):
        report = economy_report(str(BANDGAP_GDS))
        assert report["top"] == "bandgap_core_routed"
        assert report["bbox_um"] == {
            "left": -10.0,
            "bottom": -10.0,
            "right": 318.2,
            "top": 215.44,
            "width": 328.2,
            "height": 225.44,
        }
        assert report["bbox_area_um2"] == pytest.approx(73989.408)
        assert report["utilization"] == pytest.approx(0.38050067950266614)

    def test_dead_margins_match_placeholder_evidence(self):
        report = economy_report(str(BANDGAP_GDS))
        margins = report["dead_margins_um"]
        assert margins["left"] == pytest.approx(41.025, abs=1e-3)
        assert margins["right"] == pytest.approx(41.025, abs=1e-3)
        assert margins["bottom"] == pytest.approx(37.573, abs=1e-3)
        assert margins["top"] == pytest.approx(0.0, abs=1e-3)

    def test_not_row_based(self):
        """An analog block's placement must not be misdetected as a
        std-cell row (it has only 2 direct instances)."""
        report = economy_report(str(BANDGAP_GDS))
        assert report["row_utilization"] is None


@pytest.mark.skipif(not BUF4_GDS.is_file(), reason="canary GDS not present")
class TestStdCellCanary:
    """`blocks/sky130_fd_sc_hd__buf_4` -- a foundry-authored sky130 digital
    standard cell, tight by construction (its footprint *is* its row-
    height/pitch legal minimum). Numbers cross-checked against
    `evidence/economy-review/sky130_fd_sc_hd__buf_4/*-metrics.json` (PR
    #1024), same rationale as `TestBandgapCanary`.
    """

    def test_utilization_and_bbox(self):
        report = economy_report(str(BUF4_GDS))
        assert report["top"] == "sky130_fd_sc_hd__buf_4"
        assert report["bbox_area_um2"] == pytest.approx(10.048)
        assert report["utilization"] == pytest.approx(0.9396795382165606)

    def test_dead_margins_zero(self):
        """A tight-by-construction std cell has no dead margin on any edge."""
        report = economy_report(str(BUF4_GDS))
        margins = report["dead_margins_um"]
        assert margins["left"] == pytest.approx(0.0, abs=1e-3)
        assert margins["right"] == pytest.approx(0.0, abs=1e-3)
        assert margins["bottom"] == pytest.approx(0.0, abs=1e-3)
        assert margins["top"] == pytest.approx(0.0, abs=1e-3)

    def test_looser_than_reference_flags_correctly(self):
        """A `--reference-area-um2` set to this cell's own actual area
        should report ratio 1.0x; a smaller reference should FAIL a budget
        check derived from it -- exercises the two request-side knobs
        end-to-end against a real layout, not just synthetic fixtures."""
        report = economy_report(
            str(BUF4_GDS), reference_area_um2=10.048, budget_um2=8.0
        )
        assert report["reference"]["ratio"] == pytest.approx(1.0)
        assert report["budget"]["status"] == "fail"
