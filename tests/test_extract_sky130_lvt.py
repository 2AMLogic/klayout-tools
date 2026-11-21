"""sky130 1.8V low-threshold (`lvt`) MOS flavour via the lvtn marker (issue #5283).

Split out of `test_extract.py` (the per-file size ratchet forbids growing it).
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb

from klayout_tools.extract import run_extract
from test_extract import (
    _draw_sky130_nmos,
    _make_pdk_install,
    _sky130_bound_models,
    _write_gds,
)


def test_sky130_lvtn_nfet_binds_lvt_model(tmp_path):
    report, lines = _sky130_bound_models(tmp_path, "lvtn_n", [(125, 44)])
    assert report["device_counts"] == {"nfet": 1}
    (line,) = lines
    assert "sky130_fd_pr__nfet_01v8_lvt" in line


def test_sky130_lvtn_pfet_binds_lvt_model(tmp_path):
    report, lines = _sky130_bound_models(tmp_path, "lvtn_p", [(125, 44)], pmos=True)
    assert report["device_counts"] == {"pfet": 1}
    (line,) = lines
    assert "sky130_fd_pr__pfet_01v8_lvt" in line


def test_sky130_lvtn_with_hvtp_pfet_is_not_lvt(tmp_path):
    _report, lines = _sky130_bound_models(
        tmp_path, "lvtn_hvtp", [(125, 44), (78, 44)], pmos=True
    )
    (line,) = lines
    assert "_lvt" not in line


def test_sky130_hvtp_pfet_without_lvtn_is_plain(tmp_path):
    _report, lines = _sky130_bound_models(tmp_path, "hvtp", [(78, 44)], pmos=True)
    (line,) = lines
    assert "sky130_fd_pr__pfet_01v8" in line
    assert "_lvt" not in line


def test_sky130_hvi_lvtn_nfet_stays_nvt_not_lvt(tmp_path):
    _report, lines = _sky130_bound_models(tmp_path, "nvt2", [(75, 20), (125, 44)])
    (line,) = lines
    assert "sky130_fd_pr__nfet_05v0_nvt" in line
    assert "_01v8_lvt" not in line


def test_sky130_lvtn_partial_cover_claims_whole_device(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw_sky130_nmos(top, layout, 0, "S", drain_label="D")
    top.shapes(layout.layer(125, 44)).insert(kdb.Box(-500, -500, 1000, 1500))
    path = _write_gds(layout, tmp_path / "lvt_partial.gds")
    root = _make_pdk_install(tmp_path, "sky130A")
    report = run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=root,
        output=str(tmp_path / "lvt_partial.spice"),
    )
    assert report["device_counts"] == {"nfet": 1}
    text = Path(report["netlist_path"]).read_text()
    assert text.count("sky130_fd_pr__nfet_01v8_lvt") == 1


def _strip_models(tmp_path, name, *, pmos, marker_right, extra=()):
    """3-gate strip on one diff; gates at x=800/2000/3200 (400 wide), shared
    S/D between them. ``lvtn`` spans x=-500..``marker_right``."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def d(layer, dt, box):
        top.shapes(layout.layer(layer, dt)).insert(box)

    d(65, 20, kdb.Box(0, 0, 4400, 1000))
    for gx in (800, 2000, 3200):
        d(66, 20, kdb.Box(gx, -200, gx + 400, 1200))
    for i, sx in enumerate((100, 1500, 2700, 3900)):
        d(66, 44, kdb.Box(sx, 300, sx + 200, 700))
        d(67, 20, kdb.Box(sx - 100, 200, sx + 300, 800))
        top.shapes(layout.layer(67, 5)).insert(
            kdb.Text(f"N{i}", kdb.Trans(sx + 100, 500))
        )
    if pmos:
        d(64, 20, kdb.Box(-1000, -1000, 5400, 2000))
    d(125, 44, kdb.Box(-500, -500, marker_right, 1500))
    for layer, dt, box in extra:
        d(layer, dt, box)
    path = _write_gds(layout, tmp_path / f"{name}.gds")
    root = _make_pdk_install(tmp_path, "sky130A")
    report = run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=root,
        output=str(tmp_path / f"{name}.spice"),
    )
    text = Path(report["netlist_path"]).read_text()
    lines = [ln for ln in text.splitlines() if ln and ln[0] in ("M", "X")]
    return report, lines


def _count_lvt(lines):
    lvt = sum("_lvt" in ln for ln in lines)
    return lvt, len(lines) - lvt


def test_sky130_lvtn_multigate_pfet_strip_is_per_gate(tmp_path):
    # Marker ends in the S/D shared by gates 2 and 3 (x=2400..3200 region).
    report, lines = _strip_models(tmp_path, "p_strip", pmos=True, marker_right=2800)
    assert report["device_counts"] == {"pfet": 3}
    assert _count_lvt(lines) == (2, 1)


def test_sky130_lvtn_multigate_nfet_strip_is_per_gate(tmp_path):
    report, lines = _strip_models(tmp_path, "n_strip", pmos=False, marker_right=2800)
    assert report["device_counts"] == {"nfet": 3}
    assert _count_lvt(lines) == (2, 1)


def test_sky130_lvtn_strip_shared_sd_stays_connected(tmp_path):
    _report, lines = _strip_models(
        tmp_path, "n_strip_nets", pmos=False, marker_right=2800
    )
    # Nets N1/N2 (shared S/D) each appear on exactly two devices.
    for net in ("N1", "N2"):
        assert sum(net in ln.split() for ln in lines) == 2, (net, lines)


def test_sky130_lvtn_marker_only_on_sd_claims_nothing(tmp_path):
    # Marker confined to the S/D left of gate 1: no gate overlap, no claim.
    _report, lines = _strip_models(tmp_path, "n_sd_only", pmos=False, marker_right=600)
    assert _count_lvt(lines) == (0, 3)
