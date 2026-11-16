"""Shared / stepped diffusion across the sky130 ``lvtn`` claim boundary (#5311).

``lvtn`` over only some gates of one diffusion island must change the model
name only: nets, devices and shared-S/D AS/AD/PS/PD stay what an unmarked
extraction gives. Split out of ``test_extract.py`` (size ratchet).
"""

from __future__ import annotations

import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from test_extract import _make_pdk_install, _write_gds


def _extract(base_path, name, *, marker_right, pmos, floating):
    """3 gates at x=800/2000/3200 on a strip whose middle columns step in
    width (taller diffusion right of x=2400). ``floating`` leaves the two
    shared S/D columns uncontacted. ``lvtn`` spans x=-500..``marker_right``
    (``None`` = no marker)."""
    tmp_path = base_path / name
    tmp_path.mkdir()
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def d(layer, dt, box):
        top.shapes(layout.layer(layer, dt)).insert(box)

    d(65, 20, kdb.Box(0, 0, 2400, 1000))
    d(65, 20, kdb.Box(2400, 0, 4400, 1600))
    for gx in (800, 2000, 3200):
        d(66, 20, kdb.Box(gx, -200, gx + 400, 1800))
    for i, sx in enumerate((100, 1500, 2700, 3900)):
        if floating and i in (1, 2):
            continue
        d(66, 44, kdb.Box(sx, 300, sx + 200, 700))
        d(67, 20, kdb.Box(sx - 100, 200, sx + 300, 800))
        top.shapes(layout.layer(67, 5)).insert(
            kdb.Text(f"N{i}", kdb.Trans(sx + 100, 500))
        )
    if pmos:
        d(64, 20, kdb.Box(-1000, -1000, 5400, 2800))
    if marker_right is not None:
        d(125, 44, kdb.Box(-500, -500, marker_right, 1500))
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
    return report, [ln for ln in text.splitlines() if ln and ln[0] in ("M", "X")]


def _params(line):
    return {k: float(v) for k, v in re.findall(r"\b(AS|AD|PS|PD)=([0-9.eE+-]+)", line)}


@pytest.mark.parametrize("pmos", [True, False])
@pytest.mark.parametrize("floating", [False, True])
def test_lvtn_partial_cover_matches_unmarked(tmp_path, pmos, floating):
    base_r, base = _extract(
        tmp_path, "base", marker_right=None, pmos=pmos, floating=floating
    )
    lvt_r, lvt = _extract(
        tmp_path, "lvt", marker_right=2800, pmos=pmos, floating=floating
    )
    assert lvt_r["device_counts"] == base_r["device_counts"]
    assert sum("_lvt" in ln for ln in lvt) == 2
    assert len(lvt) == len(base) == 3

    # Same shared-S/D areas as the unmarked extraction (0.9.1). The lvt model
    # card omits some perimeter terms, so only AS/AD are comparable.
    def areas(lines):
        return sorted((_params(ln).get("AS"), _params(ln).get("AD")) for ln in lines)

    assert areas(lvt) == areas(base)


def test_lvtn_floating_shared_column_stays_one_net(tmp_path):
    _r, lines = _extract(tmp_path, "float", marker_right=2800, pmos=True, floating=True)
    # Gates 2 and 3 meet in an uncontacted column: one net on both devices,
    # not one per claim half.
    nets = [set(ln.split()[1:5:2]) for ln in lines]
    shared = [n for n in nets if len(n) == 2]
    assert len(shared) == 3
    all_nets = {x for n in nets for x in n}
    assert len(all_nets) == 4, nets
