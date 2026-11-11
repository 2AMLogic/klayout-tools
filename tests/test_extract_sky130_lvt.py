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
