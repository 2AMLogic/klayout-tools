"""sg13g2 derived tap (issue #1273): `tap_nplus`/`tap_pplus` let this deck,
which draws no distinct `tap` layer at all, derive an equivalent well-/
substrate-tie region from its own already-declared `nSD`/`pSD` implant
layers -- mirroring the gf180mcu derived-tap tests in
`tests/test_extract_body_nets_gf180mcu.py` (issue #1084), same technique,
different implant layers/layer numbers.

Split out of `tests/test_extract.py` (issue #5596). The shared fixture
builder `_make_sg13g2_inverter_layout` lives in
`tests/helpers/extract_inverter_fixtures.py`, since tests that stayed in
`tests/test_extract.py` still call it. Two parasitics tests that reuse that
same fixture ride along in this module rather than splitting the deck's
block in half.
"""

from __future__ import annotations

import klayout.db as kdb

from helpers.extract_inverter_fixtures import _make_sg13g2_inverter_layout
from klayout_tools.extract import run_extract
from test_extract import _write_gds


def test_sg13g2_pmos_body_resolves_to_drawn_well_tap_net(tmp_path):
    """An `nSD`-over-`Activ` well tie drawn inside `NWell` and contacted up
    to a labelled net is now the PMOS body terminal's real net -- not an
    anonymous `$n` net (issue #1273, mirroring gf180mcu's `tap_nplus`/
    `tap_pplus` mechanism from issue #1084). The NMOS body is unaffected (no
    substrate tie drawn in this fixture), and the floating-PMOS-body warning
    (`unbiased_pmos_body_nets[]`) no longer fires."""
    path = _write_gds(
        _make_sg13g2_inverter_layout(well_tap_label="WTIE"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "sg13g2", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert pfet["nets"]["b"] == "WTIE"
    assert nfet["nets"]["b"] == "vsubs"

    assert report["unbiased_pmos_body_nets"] == []
    assert not any("no DC bias path" in warning for warning in report["warnings"])


def test_sg13g2_nmos_body_resolves_to_drawn_substrate_tap_net(tmp_path):
    """A `pSD`-over-`Activ` substrate tie drawn outside every `NWell` and
    contacted up to a labelled net is now the NMOS body terminal's real net
    -- not the deck's synthesized `vsubs` global (issue #1273). The PMOS
    body is unaffected (no well tie drawn in this fixture) -- still an
    anonymous, floating net, exactly as it was before this issue's fix."""
    path = _write_gds(
        _make_sg13g2_inverter_layout(substrate_tap_label="STIE"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "sg13g2", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] == "STIE"
    assert pfet["nets"]["b"].startswith("\\$")

    # The synthesized global name never shows up: the tie's own real net
    # fully absorbs the (otherwise-empty) global net.
    net_names = {n["name"] for n in report["nets"]}
    assert "STIE" in net_names
    assert "vsubs" not in net_names


def test_sg13g2_nmos_body_falls_back_to_substrate_global_without_tap(tmp_path):
    """No drawn tie at all: both bodies fall back to exactly the pre-#1273
    behaviour -- the NMOS body terminal ties to the deck's synthesized
    `vsubs` global, and the PMOS body terminal is a floating, anonymous net
    that still fires the `unbiased_pmos_body_nets[]`/`warnings[]` signal --
    the additive-capability guarantee this issue's acceptance criteria
    require for a layout that draws no tie."""
    path = _write_gds(_make_sg13g2_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] == "vsubs"
    assert pfet["nets"]["b"].startswith("\\$")

    assert report["unbiased_pmos_body_nets"] == [
        {"device": pfet["name"], "net": pfet["nets"]["b"]}
    ]
    assert any("no DC bias path" in warning for warning in report["warnings"])


def test_sg13g2_parasitics_metal1_metal2_report_nonzero_rc(tmp_path):
    """Issue #1277 (extended to the full stack by #1281): with
    `sg13g2.PARASITICS.metals`/`metal_overlaps` curated for every declared
    metal level, `--parasitics` on a layout that actually routes Metal1/
    Metal2 no longer reports `r_count: 0, c_count: 0` (issue #1277's own
    repro) and no longer lists any metal level in
    `metals_without_coefficient` -- Metal3-TopMetal2 are now curated too
    (issue #1281), so the gap set is empty even though this fixture only
    routes Metal1/Metal2."""
    layout = _make_sg13g2_inverter_layout()
    top = layout.cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    # Extend the NMOS drain ("Y") net up onto Metal2 through a Via1 cut, so
    # the net's parasitics span both curated metal levels.
    draw(19, 0, kdb.Box(1650, 250, 1950, 550))  # Via1.drawing (Metal1<->Metal2)
    draw(10, 0, kdb.Box(1600, 200, 2000, 1200))  # Metal2.drawing

    path = _write_gds(layout, tmp_path / "inv.gds")
    report = run_extract(
        path, "sg13g2", output=str(tmp_path / "inv.spice"), parasitics=True
    )

    para = report["parasitics"]
    assert para["r_count"] > 0
    assert para["c_count"] > 0
    assert para["total_resistance_ohm"] > 0.0
    assert para["total_capacitance_ff"] > 0.0

    gap_indices = {gap["metal_index"] for gap in para["metals_without_coefficient"]}
    assert 0 not in gap_indices  # Metal1 -- curated (issue #1277)
    assert 1 not in gap_indices  # Metal2 -- curated (issue #1277)
    # Metal3-TopMetal2 are now curated too (issue #1281) -- no gaps remain.
    assert gap_indices == set()


def test_sg13g2_parasitics_topmetal2_routing_reports_no_gaps(tmp_path):
    """Issue #1281: a layout routed all the way up through TopMetal2 (the
    top of `EXTRACTION_DECK.metals`' seven-level stack) no longer lists any
    metal level in `metals_without_coefficient` nor any adjacent pair in
    `overlap_pairs_without_coefficient` -- the acceptance criteria's own
    manual-verification scenario, extending `_make_sg13g2_inverter_layout()`
    up through every via/metal level."""
    layout = _make_sg13g2_inverter_layout()
    top = layout.cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    # Stack the NMOS drain ("Y") net straight up from Metal1 through every
    # via/metal level to TopMetal2, so its parasitics span the full stack.
    stack = (
        (19, 0, 10, 0),  # Via1 (M1<->M2), Metal2
        (29, 0, 30, 0),  # Via2 (M2<->M3), Metal3
        (49, 0, 50, 0),  # Via3 (M3<->M4), Metal4
        (66, 0, 67, 0),  # Via4 (M4<->M5), Metal5
        (125, 0, 126, 0),  # TopVia1 (M5<->TopMetal1), TopMetal1
        (133, 0, 134, 0),  # TopVia2 (TopMetal1<->TopMetal2), TopMetal2
    )
    for via_layer, via_dt, metal_layer, metal_dt in stack:
        draw(via_layer, via_dt, kdb.Box(1650, 250, 1950, 550))
        draw(metal_layer, metal_dt, kdb.Box(1600, 200, 2000, 1200))

    path = _write_gds(layout, tmp_path / "inv.gds")
    report = run_extract(
        path, "sg13g2", output=str(tmp_path / "inv.spice"), parasitics=True
    )

    para = report["parasitics"]
    assert para["r_count"] > 0
    assert para["c_count"] > 0
    assert para["metals_without_coefficient"] == []
    assert para["overlap_pairs_without_coefficient"] == []


def test_sg13g2_tap_straddling_nwell_boundary_extracts_without_error(tmp_path):
    """A single drawn tie shape straddling the `NWell` boundary -- half
    covered by `nSD` *inside* the well (a well tie), half covered by `pSD`
    *outside* it (a substrate tie), physically one connected `Activ`
    conductor -- is a real electrical short in silicon. Confirms the
    well/substrate derivation (issue #1273) handles this without raising and
    without silently dropping or double-counting the geometry: because the
    two halves are genuinely one connected shape, both device bodies land on
    the *same* net (the tie's own label), correctly reflecting the short a
    real straddling tie would draw in silicon -- mirroring
    `test_gf180mcu_tap_straddling_nwell_boundary_extracts_without_error`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # Same NMOS/PMOS/NWell/gate shape as `_make_sg13g2_inverter_layout`.
    draw(1, 0, kdb.Box(0, 0, 2000, 1000))  # Activ (nmos active)
    draw(1, 0, kdb.Box(0, 2000, 2000, 3000))  # Activ (pmos active)
    draw(31, 0, kdb.Box(-500, 1500, 2500, 3500))  # NWell
    draw(5, 0, kdb.Box(800, -200, 1200, 3200))  # GatPoly (shared gate)

    for y0 in (0, 2000):
        draw(6, 0, kdb.Box(100, y0 + 300, 300, y0 + 700))  # Cont (S)
        draw(6, 0, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # Cont (D)
        draw(8, 0, kdb.Box(0, y0 + 200, 400, y0 + 800))  # Metal1 (S)
        draw(8, 0, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # Metal1 (D)
    draw(6, 0, kdb.Box(900, 1400, 1100, 1600))  # gate Cont
    draw(8, 0, kdb.Box(850, 1350, 1150, 1650))  # gate Metal1

    label(8, 25, "VGND", 200, 500)
    label(8, 25, "Y", 1800, 500)
    label(8, 25, "VPWR", 200, 2500)
    label(8, 25, "Y", 1800, 2500)
    label(8, 25, "A", 1000, 1500)

    # One Activ polygon straddling the NWell's bottom edge (y=1500): drawn
    # 1300..1700, i.e. 200 inside the well (>=1500, covered by nSD) and 200
    # outside it (<1500, covered by pSD), off to the side (x -400..-200) so
    # it never touches the transistors' own Activ geometry. Contacted +
    # labelled on its outside-the-well (substrate) end.
    draw(1, 0, kdb.Box(-400, 1300, -200, 1700))  # Activ (straddling)
    draw(7, 0, kdb.Box(-400, 1500, -200, 1700))  # nSD (inside-well half)
    draw(14, 0, kdb.Box(-400, 1300, -200, 1500))  # pSD (outside-well half)
    draw(6, 0, kdb.Box(-380, 1320, -220, 1420))  # Cont (outside-well end)
    draw(8, 0, kdb.Box(-450, 1300, -150, 1440))  # Metal1 (outside-well end)
    label(8, 25, "STRADDLE", -300, 1370)

    path = _write_gds(layout, tmp_path / "straddle.gds")
    report = run_extract(path, "sg13g2", output=str(tmp_path / "straddle.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] is not None
    assert pfet["nets"]["b"] is not None
    assert nfet["nets"]["b"] == pfet["nets"]["b"] == "STRADDLE"
