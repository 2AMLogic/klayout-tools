"""sky130 NMOS body / substrate-tap net resolution (issue #490).

Split out of `tests/test_extract.py` (issue #5596), which held all five
per-PDK body/substrate-tap sections as banner-delimited mirrors of one
another; each now has its own `test_extract_body_nets_*` module. The
derived-tap siblings that mirror these tests on decks drawing no distinct
`tap` layer are `test_extract_body_nets_gf180mcu.py` (issue #1084),
`test_extract_body_nets_sg13g2.py` (issue #1273) and
`test_extract_body_nets_sg13cmos5l.py` (issue #1414);
`test_extract_body_nets_isolated_region.py` covers per-island substrate
scoping (issue #1128).
"""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.extract import run_extract
from test_extract import _make_inverter_layout, _write_gds


def test_sky130_nmos_body_resolves_to_drawn_substrate_tap_net(tmp_path):
    """A `tap.drawing` ring drawn outside every `nwell` and contacted up to a
    labelled net is now the NMOS body terminal's real net -- not the deck's
    synthesized `vsubs` global (issue #490). `bulk_to_substrate` resistors and
    a collector-less bipolar share the same underlying region, so this also
    covers those paths (both funnel through the identical `nfet_body`
    terminal/`connect_global` wiring exercised here)."""
    path = _write_gds(
        _make_inverter_layout(substrate_tap_label="VSUBRING"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] == "VSUBRING"
    # PMOS body is unaffected -- still the real, labelled nwell tap.
    assert pfet["nets"]["b"] == "VPB"

    # The synthesized global name never shows up: the tap ring's own real
    # net fully absorbs the (otherwise-empty) global net, it doesn't merely
    # sit alongside it under a second, separate net entry.
    net_names = {n["name"] for n in report["nets"]}
    assert "VSUBRING" in net_names
    assert "vsubs" not in net_names


def test_sky130_nmos_body_falls_back_to_substrate_global_without_tap(tmp_path):
    """No drawn substrate-tap ring at all: the NMOS body terminal still
    falls back to the deck's synthesized `vsubs` global, exactly as before
    #490 -- the additive-capability guarantee for the common (no-ring)
    case."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    nfet = next(d for d in report["devices"] if d["class"] == "nfet")
    assert nfet["nets"]["b"] == "vsubs"


def test_sky130_tap_straddling_nwell_boundary_extracts_without_error(tmp_path):
    """A single `tap.drawing` polygon straddling the `nwell` boundary (part
    inside, part outside) is a real electrical short in silicon -- the
    `tap & nwell` / `tap - nwell` split (issue #490) must not silently drop
    or double-count that geometry, and extraction must not raise. Confirms
    the split behaves sanely: both device bodies still resolve to *some*
    net (not `None`), and -- because the straddling shape is genuinely one
    connected conductor bridging the well and the substrate -- both device
    bodies land on the *same* net, correctly reflecting the short a real
    straddling tap would draw in silicon."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # Same NMOS/PMOS/nwell/gate shape as `_make_inverter_layout`: NMOS
    # active strip 0..2000 x 0..1000, PMOS active strip 0..2000 x
    # 2000..3000, nwell -500..2500 x 1500..3500 -- the nwell's bottom edge
    # sits at y=1500, with a 500-wide substrate gap (1000..1500) below it.
    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))  # diff.drawing (pmos active)
    draw(64, 20, kdb.Box(-500, 1500, 2500, 3500))  # nwell.drawing
    draw(66, 20, kdb.Box(800, -200, 1200, 3200))  # poly.drawing (shared gate)

    for y0 in (0, 2000):
        draw(66, 44, kdb.Box(100, y0 + 300, 300, y0 + 700))  # licon1 (S)
        draw(66, 44, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # licon1 (D)
        draw(67, 20, kdb.Box(0, y0 + 200, 400, y0 + 800))  # li1 (S)
        draw(67, 20, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # li1 (D)
    draw(66, 44, kdb.Box(900, 1400, 1100, 1600))  # gate licon
    draw(67, 20, kdb.Box(850, 1350, 1150, 1650))  # gate li1

    label(67, 5, "VGND", 200, 500)
    label(67, 5, "Y", 1800, 500)
    label(67, 5, "VPWR", 200, 2500)
    label(67, 5, "Y", 1800, 2500)
    label(67, 5, "A", 1000, 1500)

    # One tap polygon straddling the nwell's bottom edge (y=1500): drawn
    # 1300..1700, i.e. 200 inside the well (>=1500) and 200 outside it
    # (<1500), off to the side (x -400..-200) so it never touches the
    # active/gate geometry itself. Contacted + labelled on its
    # outside-the-well (substrate) end.
    draw(65, 44, kdb.Box(-400, 1300, -200, 1700))  # tap.drawing (straddling)
    draw(66, 44, kdb.Box(-380, 1320, -220, 1420))  # licon1 (outside-well end)
    draw(67, 20, kdb.Box(-450, 1300, -150, 1440))  # li1 (outside-well end)
    label(67, 5, "STRADDLE", -300, 1370)

    path = _write_gds(layout, tmp_path / "straddle.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "straddle.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] is not None
    assert pfet["nets"]["b"] is not None
    # The straddling shape genuinely bridges nwell and substrate -- both
    # bodies land on the one shorted net (the correct, if unfortunate,
    # electrical reading of that geometry), and that net carries the
    # ring's own label rather than an anonymous or dropped identity.
    assert nfet["nets"]["b"] == pfet["nets"]["b"] == "STRADDLE"
