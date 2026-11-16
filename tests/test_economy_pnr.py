"""Tests for ``klt economy --pnr-report`` (issue #5181): the place-and-route
decomposition of the top bbox (:mod:`klayout_tools.economy_pnr`)."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.economy import economy_report

# --- P&R decomposition (--pnr-report, issue #5181) --------------------------


def _make_pnr_layout(path, *, guard=False):
    """100x100 um block: logical cell 40x40 (1600), tap 10x10 (100), optional
    guard-ring instance spanning an extra 10 um on every side."""
    layout = kdb.Layout()
    layer = layout.layer(1, 0)
    top = layout.create_cell("TOP")
    for name, box in (
        ("inv_1", kdb.Box(0, 0, 40000, 40000)),
        ("tapvpwrvgnd_1", kdb.Box(0, 0, 10000, 10000)),
        ("guard_ring", kdb.Box(0, 0, 100000, 100000)),
    ):
        if name == "guard_ring" and not guard:
            continue
        cell = layout.create_cell(name)
        cell.shapes(layer).insert(box)
        shift = {"tapvpwrvgnd_1": (60000, 60000)}.get(name, (0, 0))
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(*shift)))
    layout.write(str(path))


def _pnr_report(path, die, core):
    path.write_text(
        json.dumps(
            {
                "die_area_um2": die,
                "core_area_um2": core,
                "power": {
                    "tapcell_master": "tapvpwrvgnd_1",
                    "endcap_master": None,
                    "filler_masters": [],
                },
            }
        )
    )


def _run_pnr(gds, rep, capsys, *extra):
    rc = main(
        [
            "economy",
            str(gds),
            "--attribute",
            "--pnr-report",
            str(rep),
            "--format",
            "json",
            *extra,
        ]
    )
    cap = capsys.readouterr()
    return rc, json.loads(cap.out or cap.err)


def test_pnr_decomposition_sums_to_bbox(tmp_path, capsys):
    gds, rep = tmp_path / "b.gds", tmp_path / "r.json"
    _make_pnr_layout(gds)
    # bbox: (0,0)-(70,70) = 4900 um2; die == bbox, core 4000.
    _pnr_report(rep, 4900.0, 4000.0)
    rc, payload = _run_pnr(gds, rep, capsys)
    assert rc == 0
    d = payload["attribution"]["pnr_decomposition"]
    assert d["logical_cells"]["area_um2"] == pytest.approx(1600.0)
    assert d["physical_only_cells"]["area_um2"] == pytest.approx(100.0)
    assert d["core_whitespace"]["area_um2"] == pytest.approx(2300.0)
    assert d["die_margin"]["area_um2"] == pytest.approx(900.0)
    assert d["guard_ring"]["area_um2"] == 0.0
    assert d["sums_to_total"] is True
    assert d["residual_um2"] == pytest.approx(0.0)
    assert d["overrun_ratio"] == pytest.approx(4900.0 / 1600.0)
    assert all(
        d[k]["source"]
        for k in (
            "logical_cells",
            "physical_only_cells",
            "core_whitespace",
            "die_margin",
            "guard_ring",
        )
    )


def test_pnr_decomposition_mismatched_report(tmp_path, capsys):
    gds, rep = tmp_path / "b.gds", tmp_path / "r.json"
    _make_pnr_layout(gds)
    _pnr_report(rep, 4500.0, 4000.0)
    _, payload = _run_pnr(gds, rep, capsys)
    d = payload["attribution"]["pnr_decomposition"]
    assert d["sums_to_total"] is False
    assert d["residual_um2"] == pytest.approx(400.0)


def test_pnr_decomposition_guard_ring(tmp_path, capsys):
    gds, rep = tmp_path / "b.gds", tmp_path / "r.json"
    _make_pnr_layout(gds, guard=True)
    # bbox 100x100 = 10000; non-guard content bbox 70x70 = 4900.
    _pnr_report(rep, 4900.0, 4000.0)
    _, payload = _run_pnr(gds, rep, capsys)
    d = payload["attribution"]["pnr_decomposition"]
    assert d["guard_ring"]["area_um2"] == pytest.approx(5100.0)
    assert d["sums_to_total"] is True


def test_pnr_report_requires_attribute(tmp_path, capsys):
    gds, rep = tmp_path / "b.gds", tmp_path / "r.json"
    _make_pnr_layout(gds)
    _pnr_report(rep, 4900.0, 4000.0)
    rc = main(["economy", str(gds), "--pnr-report", str(rep), "--format", "json"])
    assert rc != 0
    assert "error" in json.loads(capsys.readouterr().err)


@pytest.mark.parametrize("content", [None, "not json", "[]", "{}"])
def test_pnr_report_bad_inputs(tmp_path, capsys, content):
    gds, rep = tmp_path / "b.gds", tmp_path / "r.json"
    _make_pnr_layout(gds)
    if content is not None:
        rep.write_text(content)
    rc, payload = _run_pnr(gds, rep, capsys)
    assert rc != 0
    assert "error" in payload


def test_pnr_core_larger_than_bbox_errors(tmp_path, capsys):
    gds, rep = tmp_path / "b.gds", tmp_path / "r.json"
    _make_pnr_layout(gds)
    _pnr_report(rep, 99999.0, 90000.0)
    rc, payload = _run_pnr(gds, rep, capsys)
    assert rc != 0
    assert "error" in payload


def test_no_pnr_report_output_unchanged(tmp_path):
    gds = tmp_path / "b.gds"
    _make_pnr_layout(gds)
    report = economy_report(str(gds), attribute=True)
    assert "pnr_decomposition" not in report["attribution"]
