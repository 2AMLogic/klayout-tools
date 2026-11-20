"""Flat-array device-recognition scaling regression test (issue #5807).

``klt extract --deck gf180mcu`` regressed superlinearly on flat arrays
between v0.5.0 and v0.8.0 (dfc9ae034, issue #2919's gf180mcu MOS-capacitor
recogniser): ``_resolve_mos_capacitors`` walked every ``active`` island and
ran one full-layout ``interacting()`` per island per marker/implant layer,
and ``_detect_unsupported_devices`` repeated that walk -- quadratic in
device count, so an ~8k-cell SRAM array went from 202 s (v0.5.0) to
unfinished after 1831 s (v0.8.0). Issue #4587's
``_flavour_recognised_islands`` later added the same per-island pattern to
the unsupported-device diagnostic.

The test times the two recognition paths directly -- in-process, over the
synthetic flat array below at two sizes -- because an end-to-end wall-time
ratio also sees KLayout's own C++ net clustering (superlinear at high
device counts in every version, v0.5.0 included) and shared-CI contention.
On the pre-fix tree the 800/200 ratio of this block measured ~16x
(quadratic); the bulk-island-set rewrite holds it near 1. The bound 6
leaves jitter headroom while still tripping any return to per-island,
per-layer full-layout scans. Full-run wall times per size are recorded in
the PR, not asserted here.
"""

from __future__ import annotations

import time

import klayout.db as kdb

from klayout_tools._layout import region as _region
from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import _detect_unsupported_devices, _resolve_mos_capacitors

# gf180mcu extraction-deck layers (decks/gf180mcu.py's EXTRACTION_DECK).
_ACTIVE = (22, 0)  # Comp
_POLY = (30, 0)  # Poly2
_NWELL = (21, 0)
_CONTACT = (33, 0)
_NPLUS = (32, 0)
_PPLUS = (31, 0)
_METAL1 = (34, 0)

# Unit-cell geometry, in um (DBU 1 nm). Each cell draws 4 NMOS + 2 PMOS as
# separate active islands -- the island count is what drives the device
# recognition loops -- with contacts and local metal1 straps, laid out on a
# square grid and drawn flat into one top cell. No flavour or MOS-cap
# markers: the fixture mirrors the reporter's plain SRAM array, where the
# regressing loops ran at full cost over every island despite claiming
# nothing.
_AW, _AH = 0.7, 1.4
_GW, _GH = 1.1, 0.5
_CW, _CH = 0.4, 0.4
_PITCH_X = 2.0
_ROW_H = 3.0
_CELL_W = 4 * _PITCH_X
_CELL_H = 2 * _ROW_H


def _flat_mos_array(n_cells: int) -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("ARRAY")

    def box(layer, x, y, w, h):
        top.shapes(layout.layer(*layer)).insert(
            kdb.Box(
                round(x * 1000),
                round(y * 1000),
                round((x + w) * 1000),
                round((y + h) * 1000),
            )
        )

    def transistor(x, y, nmos):
        box(_ACTIVE, x, y, _AW, _AH)
        box(_POLY, x + _AW / 2 - _GW / 2, y + _AH / 2 - _GH / 2, _GW, _GH)
        box(_NPLUS if nmos else _PPLUS, x - 0.2, y - 0.2, _AW + 0.4, _AH + 0.4)
        box(_CONTACT, x + 0.05, y + 0.15, _CW, _CH)
        box(_CONTACT, x + 0.05, y + _AH - 0.15 - _CH, _CW, _CH)
        box(_CONTACT, x + _AW - 0.05 - _CW, y + 0.15, _CW, _CH)
        box(_CONTACT, x + _AW - 0.05 - _CW, y + _AH - 0.15 - _CH, _CW, _CH)
        box(_METAL1, x, y + 0.1, 0.8, _AH - 0.2)
        box(_METAL1, x + _AW - 0.8, y + 0.1, 0.8, _AH - 0.2)

    cells_x = int(n_cells**0.5)
    cells_y = (n_cells + cells_x - 1) // cells_x
    for cy in range(cells_y):
        for cx in range(cells_x):
            if cy * cells_x + cx >= n_cells:
                break
            ox, oy = cx * _CELL_W, cy * _CELL_H
            box(_NWELL, ox - 0.4, oy + _ROW_H - 0.4, _CELL_W - 0.4, _ROW_H - 0.6)
            for k in range(2):
                transistor(ox + 1.0 + k * _PITCH_X, oy + _ROW_H + 0.2, nmos=False)
            for k in range(4):
                transistor(ox + 0.2 + k * _PITCH_X, oy + 0.2, nmos=True)
    return layout


def _recognition_seconds(n_cells: int) -> float:
    """Seconds the issue #5807 recognition paths take over an ``n_cells``
    array: the MOS-capacitor resolver plus the unsupported-device diagnostic
    that re-runs it and the flavour-recognised-islands sweep. The minimum of
    two runs, so a shared host's contention on one sample cannot inflate the
    ratio."""
    deck = get_extraction_deck("gf180mcu")
    layout = _flat_mos_array(n_cells)
    top = layout.top_cell()
    active = _region(layout, top, deck.active)
    poly = _region(layout, top, deck.poly)
    nwell = _region(layout, top, deck.nwell)
    best = None
    for _ in range(2):
        start = time.perf_counter()
        capacitors, remaining, _recognised, _dropped = _resolve_mos_capacitors(
            layout, top, deck, active, poly, nwell, kdb.Region()
        )
        _detect_unsupported_devices(layout, top, deck)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
        # The fixture claims no capacitors (no MOS_CAP_MK drawn): the resolver
        # must hand back the whole active region and the diagnostic must stay
        # quiet, whatever path it takes to get there.
        assert not capacitors
        assert remaining.count() == active.merged().count()
    return best


def test_device_recognition_scales_linearly_on_flat_arrays():
    quarter = _recognition_seconds(400)
    full = _recognition_seconds(1600)
    # 4x the layout must not cost ~16x the recognition block: linear growth
    # is 4x and the per-island loops this guards against were quadratic
    # (~16x here, measured) before issue #5807's bulk-island-set rewrite --
    # bound 6 sits between the two with jitter headroom on both sides.
    assert full / quarter <= 6.0, (
        f"device recognition grew {full / quarter:.2f}x for a 4x-larger "
        "flat array (near-linear expected; per-island, per-layer "
        "full-layout scans regressed superlinearly before issue #5807)"
    )
