"""``klt drc --tile`` / request field ``tile`` (issue #4226): checking a
boundary-cut tileable cell in the context of its abutting neighbours."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.drc import DrcError, parse_tile, run_drc

MET5 = (72, 20)
PR_BOUNDARY = (235, 4)


def _cell_gds(tmp_path, box, *, name="TOP", pr_boundary=None, extra=()) -> str:
    layout = kdb.Layout()
    top = layout.create_cell(name)
    top.shapes(layout.layer(*MET5)).insert(box)
    for extra_box in extra:
        top.shapes(layout.layer(*MET5)).insert(extra_box)
    if pr_boundary is not None:
        top.shapes(layout.layer(*PR_BOUNDARY)).insert(pr_boundary)
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return str(path)


def _met5_count(report) -> int:
    return report["rule_counts"].get("met5.width.1", 0)


def test_boundary_cut_bar_clean_under_tile_but_not_without(tmp_path):
    # 1 um long x 12 um wide: the short axis is below the 1.6 um rule alone.
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))

    assert _met5_count(run_drc(gds, "sky130")) == 1

    tiled = run_drc(gds, "sky130", tile="3x1")
    assert _met5_count(tiled) == 0
    assert tiled["tile"] == {"columns": 3, "rows": 1}
    assert "tile" not in run_drc(gds, "sky130")


def test_genuinely_narrow_bar_still_fails_under_tile(tmp_path):
    # 1 um tall bar running the full pitch: narrow across, tile or not.
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 1000))

    report = run_drc(gds, "sky130", tile="3x1")

    assert _met5_count(report) >= 1
    assert {v["cell"] for v in report["violations"]} == {"TOP"}


def test_neighbour_only_markers_are_dropped_and_centre_markers_kept(tmp_path):
    # A local sliver inside the cell body is reported once per instance in the
    # array; only the centre instance's survives, at the original coordinates.
    gds = _cell_gds(
        tmp_path,
        kdb.Box(0, 0, 10000, 10000),
        pr_boundary=kdb.Box(0, 0, 30000, 10000),
        extra=(kdb.Box(20000, 0, 20500, 3000),),
    )
    untiled = run_drc(gds, "sky130")
    tiled = run_drc(gds, "sky130", tile="3x1")

    assert tiled["violations"] == untiled["violations"]


def test_pr_boundary_sets_pitch_when_present(tmp_path):
    # Geometry bbox is 1 um wide, but the abutment boundary is 12 um: the
    # copies are 12 um apart, do not merge, and the sliver stays narrow.
    gds = _cell_gds(
        tmp_path,
        kdb.Box(0, 0, 1000, 12000),
        pr_boundary=kdb.Box(0, 0, 12000, 12000),
    )

    assert _met5_count(run_drc(gds, "sky130", tile="3x1")) == 1


def test_one_by_one_tile_equals_untiled(tmp_path):
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))
    untiled = run_drc(gds, "sky130")
    tiled = run_drc(gds, "sky130", tile="1x1")

    assert tiled["violations"] == untiled["violations"]
    assert tiled["tile"] == {"columns": 1, "rows": 1}


def test_empty_cell_is_checked_untiled(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = tmp_path / "empty.gds"
    layout.write(str(path))

    assert run_drc(str(path), "sky130", tile="3x3")["violation_count"] == 0


def test_top_scopes_the_tiled_cell(tmp_path):
    layout = kdb.Layout()
    met5 = layout.layer(*MET5)
    layout.create_cell("A").shapes(met5).insert(kdb.Box(0, 0, 1000, 12000))
    layout.create_cell("B").shapes(met5).insert(kdb.Box(0, 0, 1000, 12000))
    path = tmp_path / "two.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130", top="A", tile="3x1")

    assert report["violation_count"] == 0


@pytest.mark.parametrize("raw", ["2x1", "3x2", "0x1", "3", "axb", "3x-1", ""])
def test_malformed_or_even_tile_is_rejected(raw):
    with pytest.raises(DrcError):
        parse_tile(raw)


def test_parse_tile_accepts_odd_counts():
    assert parse_tile("3x1") == (3, 1)
    assert parse_tile("5X3") == (5, 3)
    assert parse_tile(None) is None


def _error_message(capsys) -> str:
    return json.loads(capsys.readouterr().err)["error"]["message"]


def test_cli_tile_clean_and_reported(tmp_path, capsys):
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))

    code = main(["drc", gds, "--deck", "sky130", "--tile", "3x1", "--format", "json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["tile"] == {"columns": 3, "rows": 1}


def test_cli_rejects_tile_with_klayout_engine(tmp_path, capsys):
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))

    code = main(
        ["drc", gds, "--engine", "klayout", "--tile", "3x1", "--format", "json"]
    )

    assert code == 1
    assert "--tile is not supported with --engine klayout" in _error_message(capsys)


def test_cli_rejects_tile_with_check(tmp_path, capsys):
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))
    report = tmp_path / "r.json"
    main(["drc", gds, "--deck", "sky130", "--format", "json"])
    report.write_text(capsys.readouterr().out)

    code = main(["drc", "--check", str(report), "--tile", "3x1", "--format", "json"])

    assert code == 1
    assert "--tile cannot be combined with --check" in _error_message(capsys)


def test_request_form_carries_tile(tmp_path, capsys):
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))
    request = {
        "schema": "klt.drc.request/1",
        "file": gds,
        "deck": "sky130",
        "tile": "3x1",
    }

    code = main(["drc", json.dumps(request), "--format", "json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["tile"]["columns"] == 3


def test_rerun_replays_recorded_tile(tmp_path, capsys):
    gds = _cell_gds(tmp_path, kdb.Box(0, 0, 1000, 12000))
    report = tmp_path / "r.json"
    main(["drc", gds, "--deck", "sky130", "--tile", "3x1", "--format", "json"])
    report.write_text(capsys.readouterr().out)

    code = main(["drc", gds, "--check", str(report), "--rerun", "--format", "json"])

    assert code == 0
