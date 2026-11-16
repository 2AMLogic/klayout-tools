"""gf180mcu derived tap (issue #1084): `tap_nplus`/`tap_pplus` let this deck,
which draws no distinct `tap` layer at all, derive an equivalent well-/
substrate-tie region from its own already-declared `Nplus`/`Pplus` implant
layers -- mirroring the sky130 `tap.drawing` tests in
`tests/test_extract_body_nets_sky130.py` (issue #490), one per
acceptance-criterion case: well-tie resolution, substrate-tie resolution,
no-tie fallback (additive-only guarantee), and a straddling tie extracting
without error.

Split out of `tests/test_extract.py` (issue #5596). The shared fixture
builder `_make_gf180mcu_inverter_layout` lives in
`tests/helpers/extract_inverter_fixtures.py`, since tests that stayed in
`tests/test_extract.py` still call it. Per-island substrate scoping on this
same deck (issue #1128) is in
`tests/test_extract_body_nets_isolated_region.py`.
"""

from __future__ import annotations

import klayout.db as kdb

from helpers.extract_inverter_fixtures import _make_gf180mcu_inverter_layout
from klayout_tools.extract import run_extract
from test_extract import _write_gds


def test_gf180mcu_pmos_body_resolves_to_drawn_well_tap_net(tmp_path):
    """An `Nplus`-over-`Comp` well tie drawn inside `Nwell` and contacted up
    to a labelled net is now the PMOS body terminal's real net -- not an
    anonymous `$n` net (issue #1084, mirroring sky130's `tap.drawing`
    mechanism from issue #490). The NMOS body is unaffected (no substrate
    tie drawn in this fixture), and the floating-PMOS-body warning
    (`unbiased_pmos_body_nets[]`) no longer fires."""
    path = _write_gds(
        _make_gf180mcu_inverter_layout(well_tap_label="WTIE"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert pfet["nets"]["b"] == "WTIE"
    assert nfet["nets"]["b"] == "vsubs"

    assert report["unbiased_pmos_body_nets"] == []
    assert not any("no DC bias path" in warning for warning in report["warnings"])


def test_gf180mcu_nmos_body_resolves_to_drawn_substrate_tap_net(tmp_path):
    """A `Pplus`-over-`Comp` substrate tie drawn outside every `Nwell` and
    contacted up to a labelled net is now the NMOS body terminal's real net
    -- not the deck's synthesized `vsubs` global (issue #1084). The PMOS
    body is unaffected (no well tie drawn in this fixture) -- still an
    anonymous, floating net, exactly as it was before this issue's fix."""
    path = _write_gds(
        _make_gf180mcu_inverter_layout(substrate_tap_label="STIE"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "inv.spice"))

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


def test_gf180mcu_nmos_body_falls_back_to_substrate_global_without_tap(tmp_path):
    """No drawn tie at all: both bodies fall back to exactly the pre-#1084
    behaviour -- the NMOS body terminal ties to the deck's synthesized
    `vsubs` global, and the PMOS body terminal is a floating, anonymous net
    that still fires the `unbiased_pmos_body_nets[]`/`warnings[]` signal --
    the additive-capability guarantee this issue's acceptance criteria
    require for a layout that draws no tie."""
    path = _write_gds(_make_gf180mcu_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] == "vsubs"
    assert pfet["nets"]["b"].startswith("\\$")

    assert report["unbiased_pmos_body_nets"] == [
        {"device": pfet["name"], "net": pfet["nets"]["b"]}
    ]
    assert any("no DC bias path" in warning for warning in report["warnings"])


def test_gf180mcu_tap_straddling_nwell_boundary_extracts_without_error(tmp_path):
    """A single drawn tie shape straddling the `Nwell` boundary -- half
    covered by `Nplus` *inside* the well (a well tie), half covered by
    `Pplus` *outside* it (a substrate tie), physically one connected `Comp`
    conductor -- is a real electrical short in silicon. Confirms the
    well/substrate derivation (issue #1084) handles this without raising and
    without silently dropping or double-counting the geometry: because the
    two halves are genuinely one connected shape, both device bodies land on
    the *same* net (the tie's own label), correctly reflecting the short a
    real straddling tie would draw in silicon -- mirroring
    `test_sky130_tap_straddling_nwell_boundary_extracts_without_error`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # Same NMOS/PMOS/Nwell/gate shape as `_make_gf180mcu_inverter_layout`.
    draw(22, 0, kdb.Box(0, 0, 2000, 1000))  # Comp (nmos active)
    draw(22, 0, kdb.Box(0, 2000, 2000, 3000))  # Comp (pmos active)
    draw(21, 0, kdb.Box(-500, 1500, 2500, 3500))  # Nwell
    draw(30, 0, kdb.Box(800, -200, 1200, 3200))  # Poly2 (shared gate)

    for y0 in (0, 2000):
        draw(33, 0, kdb.Box(100, y0 + 300, 300, y0 + 700))  # Contact (S)
        draw(33, 0, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # Contact (D)
        draw(34, 0, kdb.Box(0, y0 + 200, 400, y0 + 800))  # Metal1 (S)
        draw(34, 0, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # Metal1 (D)
    draw(33, 0, kdb.Box(900, 1400, 1100, 1600))  # gate Contact
    draw(34, 0, kdb.Box(850, 1350, 1150, 1650))  # gate Metal1

    label(34, 10, "VGND", 200, 500)
    label(34, 10, "Y", 1800, 500)
    label(34, 10, "VPWR", 200, 2500)
    label(34, 10, "Y", 1800, 2500)
    label(34, 10, "A", 1000, 1500)

    # One Comp polygon straddling the Nwell's bottom edge (y=1500): drawn
    # 1300..1700, i.e. 200 inside the well (>=1500, covered by Nplus) and
    # 200 outside it (<1500, covered by Pplus), off to the side (x
    # -400..-200) so it never touches the transistors' own Comp geometry.
    # Contacted + labelled on its outside-the-well (substrate) end.
    draw(22, 0, kdb.Box(-400, 1300, -200, 1700))  # Comp (straddling)
    draw(32, 0, kdb.Box(-400, 1500, -200, 1700))  # Nplus (inside-well half)
    draw(31, 0, kdb.Box(-400, 1300, -200, 1500))  # Pplus (outside-well half)
    draw(33, 0, kdb.Box(-380, 1320, -220, 1420))  # Contact (outside-well end)
    draw(34, 0, kdb.Box(-450, 1300, -150, 1440))  # Metal1 (outside-well end)
    label(34, 10, "STRADDLE", -300, 1370)

    path = _write_gds(layout, tmp_path / "straddle.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "straddle.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] is not None
    assert pfet["nets"]["b"] is not None
    assert nfet["nets"]["b"] == pfet["nets"]["b"] == "STRADDLE"
