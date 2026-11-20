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
Before the fix, recognition took 15.9x as long at 1600 cells as at 400
cells (quadratic growth). Linear growth predicts 4x. After the bulk-island-set
rewrite, six runs measured 3.1x to 5.1x. The 9x bound allows for timing
jitter and still catches the original per-island, per-layer scans. Full-run
wall times per size are recorded in PR #6072.

The second test (issue #6527) pins result identity, not timing: with the
gf180mcu MOS-cap marker drawn, the resolver's per-layer ``touching()``
memo must return exactly what an unmemoised reference computes.
"""

from __future__ import annotations

import time

import klayout.db as kdb

from klayout_tools._layout import region as _region
from klayout_tools.decks import ExtractionDeck, get_extraction_deck
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
    five runs (issue #3610's sampling posture; two was too few, issue
    #6502), so a shared host's contention on one sample cannot inflate the
    ratio."""
    deck = get_extraction_deck("gf180mcu")
    layout = _flat_mos_array(n_cells)
    top = layout.top_cell()
    active = _region(layout, top, deck.active)
    poly = _region(layout, top, deck.poly)
    nwell = _region(layout, top, deck.nwell)
    best = None
    for _ in range(5):
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
    # With five runs per size, PR #6889 measured 4.40x and 5.48x after the
    # fix and 12.73x on the pre-fix tree (9aff912a9).
    assert full / quarter <= 9.0, (
        f"device recognition grew {full / quarter:.2f}x for a 4x-larger "
        "flat array (near-linear expected; per-island, per-layer "
        "full-layout scans regressed superlinearly before issue #5807)"
    )


_MOS_CAP_MK = (166, 5)  # gf180mcu MOS-cap marker: the marker-rich path


def _marker_rich_array(n_cells: int) -> kdb.Layout:
    """``_flat_mos_array`` with MOS_CAP_MK drawn over the whole array, so
    ``_resolve_mos_capacitors`` runs its full entry loop (the plain fixture
    above never draws the marker, so the empty-marker short-circuit skips
    it and ``touching`` is never called -- issue #6527's path)."""
    layout = _flat_mos_array(n_cells)
    top = layout.top_cell()
    cells_x = int(n_cells**0.5)
    cells_y = (n_cells + cells_x - 1) // cells_x
    top.shapes(layout.layer(*_MOS_CAP_MK)).insert(
        kdb.Box(0, 0, round(cells_x * _CELL_W * 1000), round(cells_y * _CELL_H * 1000))
    )
    return layout


def _same_region(a: kdb.Region, b: kdb.Region) -> bool:
    """Geometric equality -- ``kdb.Region`` has no value-equality ``==``."""
    return (a - b).is_empty() and (b - a).is_empty()


def _unmemoised_mos_cap_claims(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    active: kdb.Region,
    poly: kdb.Region,
    nwell: kdb.Region,
) -> tuple[dict[int, kdb.Region], kdb.Region]:
    """The ``_resolve_mos_capacitors`` claim loop exactly as before issue
    #6527: every ``touching()`` call recomputes its ``interacting()`` and
    the well complement is rebuilt per entry. The differential oracle for
    the memoised resolver -- kept here so a cache bug cannot hide behind
    the production implementation it is meant to check."""
    layers: dict[tuple[int, int], kdb.Region] = {}

    def region_of(layer: tuple[int, int]) -> kdb.Region:
        if layer not in layers:
            layers[layer] = _region(layout, top_cell, layer)
        return layers[layer]

    islands = active.merged()
    gated = islands.interacting(poly & islands)
    in_well = islands.interacting(nwell)

    def touching(layer: tuple[int, int]) -> kdb.Region:
        region = region_of(layer)
        return kdb.Region() if region.is_empty() else islands.interacting(region)

    claimed: dict[int, kdb.Region] = {}
    taken = kdb.Region()
    for index, entry in enumerate(deck.mos_capacitors):
        if region_of(entry.marker).is_empty():
            continue  # no marker shapes: this entry can claim no island
        candidates = gated & touching(entry.marker) & touching(entry.implant)
        for layer in entry.requires:
            candidates &= touching(layer)
        for layer in entry.excludes:
            candidates -= touching(layer)
        candidates &= in_well if entry.in_well else islands - in_well
        candidates -= taken
        if not candidates.is_empty():
            claimed[index] = candidates
            taken += candidates
    return claimed, islands - taken


def test_mos_cap_resolver_marker_rich_results_unchanged_by_memo():
    """Issue #6527: memoizing ``touching()`` per layer and hoisting the well
    complement out of the entry loop must not move a single polygon -- the
    same entries claim the same islands against an unmemoised reference
    over the same marker-rich fixture."""
    deck = get_extraction_deck("gf180mcu")
    layout = _marker_rich_array(100)
    top = layout.top_cell()
    active = _region(layout, top, deck.active)
    poly = _region(layout, top, deck.poly)
    nwell = _region(layout, top, deck.nwell)
    capacitors, remaining, recognised, dropped = _resolve_mos_capacitors(
        layout, top, deck, active, poly, nwell, kdb.Region()
    )
    claimed, ref_remaining = _unmemoised_mos_cap_claims(
        layout, top, deck, active, poly, nwell
    )
    assert _same_region(remaining, ref_remaining)
    ref_recognised = kdb.Region()
    for island in claimed.values():
        ref_recognised += island
    assert _same_region(recognised, ref_recognised)
    # The marker-rich path engaged: the substrate NMOS entry claims the
    # NPLUS islands and the in-well PMOS entry the PPLUS ones (issue #6527's
    # measured baseline: entries [0, 2] claim, remaining is empty).
    assert sorted(claimed) == [0, 2]
    assert [entry.name for entry, _, _ in capacitors] == [
        "cap_nmos_03v3",
        "cap_pmos_03v3",
    ]
    assert remaining.is_empty()
    assert dropped == 0  # no dummy marker in this fixture
    for (entry, gate, source_drain), index in zip(
        capacitors, sorted(claimed), strict=True
    ):
        island = claimed[index]
        assert entry is deck.mos_capacitors[index]
        assert _same_region(gate, island & poly)
        assert _same_region(source_drain, island - poly)
