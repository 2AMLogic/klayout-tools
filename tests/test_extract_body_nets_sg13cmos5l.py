"""sg13cmos5l derived tap (issue #1414, the cmos5l sibling of sg13g2's own
#1273): `tap_nplus`/`tap_pplus` let this deck, which draws no distinct
`tap` layer at all, derive an equivalent well-/substrate-tie region from
its own already-declared `nSD`/`pSD` implant layers -- mirroring the
sg13g2 derived-tap tests in `tests/test_extract_body_nets_sg13g2.py`
one-for-one (same layer numbers: cmos5l's
`Activ`/`GatPoly`/`NWell`/`Cont`/`Metal1`/`nSD`/`pSD` are all
byte-identical to sg13g2's own, see `sg13cmos5l.py`'s module docstring's
symlink-chain note), differing only in the label layer (cmos5l labels on
`Metal1.pin`, `8/2`, not sg13g2's `metal1_text`, `8/25`).

Split out of `tests/test_extract.py` (issue #5596). This deck's fixture
builder is called only from here, so it moved with the tests rather than
into `tests/helpers/extract_inverter_fixtures.py`.
"""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.extract import run_extract
from test_extract import _write_gds


def _make_sg13cmos5l_inverter_layout(
    *, well_tap_label: str | None = None, substrate_tap_label: str | None = None
) -> kdb.Layout:
    """A minimal sg13cmos5l inverter (NMOS `Activ` outside `NWell`, PMOS
    `Activ` inside it, sharing a `GatPoly` gate, contacted up through
    `Cont`/`Metal1`) -- the sg13cmos5l-layer-number counterpart of
    `tests/helpers/extract_inverter_fixtures.py`'s
    `_make_sg13g2_inverter_layout`, shaped to exercise the deck's derived
    `tap_nplus`/`tap_pplus` well-/substrate-tie mechanism (issue #1414).

    When `well_tap_label` is given, an `nSD`-covered `Activ` strip is drawn
    *inside* the `NWell` (a well tie -- n+ diffusion tied to the n-type
    well), contacted + labelled with the given text. When
    `substrate_tap_label` is given, a `pSD`-covered `Activ` strip is drawn
    *outside* every `NWell` (a substrate tie -- p+ diffusion tied to the
    p-type substrate), likewise contacted + labelled. Both off to the side
    (x -450..-150), clear of the transistors' own `Activ` geometry (x
    0..2000), so neither can be mistaken for ordinary source/drain
    diffusion. Either or both left `None` (the default) draws no
    corresponding tie -- exercising the pre-#1414 fallback behaviour."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # NMOS: Activ strip 0..2000 x 0..1000 (outside NWell). PMOS: Activ strip
    # 0..2000 x 2000..3000 (inside NWell -500..2500 x 1500..3500). One
    # continuous GatPoly gate bar crosses both, mirroring
    # `helpers.extract_inverter_fixtures._make_sg13g2_inverter_layout`'s
    # shape one-for-one on cmos5l's own (identical) layer numbers.
    draw(1, 0, kdb.Box(0, 0, 2000, 1000))  # Activ.drawing (nmos active)
    draw(1, 0, kdb.Box(0, 2000, 2000, 3000))  # Activ.drawing (pmos active)
    draw(31, 0, kdb.Box(-500, 1500, 2500, 3500))  # NWell.drawing
    draw(5, 0, kdb.Box(800, -200, 1200, 3200))  # GatPoly.drawing (shared gate bar)

    for y0 in (0, 2000):
        draw(6, 0, kdb.Box(100, y0 + 300, 300, y0 + 700))  # Cont (S side)
        draw(6, 0, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # Cont (D side)
        draw(8, 0, kdb.Box(0, y0 + 200, 400, y0 + 800))  # Metal1 (S side)
        draw(8, 0, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # Metal1 (D side)

    draw(6, 0, kdb.Box(900, 1400, 1100, 1600))  # gate Cont
    draw(8, 0, kdb.Box(850, 1350, 1150, 1650))  # gate Metal1

    label(8, 2, "VGND", 200, 500)  # Metal1.pin
    label(8, 2, "Y", 1800, 500)
    label(8, 2, "VPWR", 200, 2500)
    label(8, 2, "Y", 1800, 2500)
    label(8, 2, "A", 1000, 1500)

    if well_tap_label is not None:
        # Well tie: nSD-covered Activ inside the NWell, off to the side so
        # it never touches the PMOS's own Activ strip.
        draw(1, 0, kdb.Box(-400, 2400, -200, 2600))  # Activ
        draw(7, 0, kdb.Box(-400, 2400, -200, 2600))  # nSD (well-tie doping)
        draw(6, 0, kdb.Box(-380, 2450, -220, 2550))  # Cont
        draw(8, 0, kdb.Box(-450, 2400, -150, 2600))  # Metal1
        label(8, 2, well_tap_label, -300, 2500)

    if substrate_tap_label is not None:
        # Substrate tie: pSD-covered Activ outside every NWell, well below
        # the NMOS active strip (y < 0) so it never touches it.
        draw(1, 0, kdb.Box(-400, -800, -200, -200))  # Activ
        draw(14, 0, kdb.Box(-400, -800, -200, -200))  # pSD (substrate-tie doping)
        draw(6, 0, kdb.Box(-380, -650, -220, -550))  # Cont
        draw(8, 0, kdb.Box(-450, -700, -150, -500))  # Metal1
        label(8, 2, substrate_tap_label, -300, -600)

    return layout


def test_sg13cmos5l_pmos_body_resolves_to_drawn_well_tap_net(tmp_path):
    """An `nSD`-over-`Activ` well tie drawn inside `NWell` and contacted up
    to a labelled net is now the PMOS body terminal's real net -- not an
    anonymous `$n` net (issue #1414, mirroring sg13g2's `tap_nplus`/
    `tap_pplus` mechanism from issue #1273). The NMOS body is unaffected (no
    substrate tie drawn in this fixture), and the floating-PMOS-body warning
    (`unbiased_pmos_body_nets[]`) no longer fires."""
    path = _write_gds(
        _make_sg13cmos5l_inverter_layout(well_tap_label="WTIE"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert pfet["nets"]["b"] == "WTIE"
    assert nfet["nets"]["b"] == "vsubs"

    assert report["unbiased_pmos_body_nets"] == []
    assert not any("no DC bias path" in warning for warning in report["warnings"])


def test_sg13cmos5l_nmos_body_resolves_to_drawn_substrate_tap_net(tmp_path):
    """A `pSD`-over-`Activ` substrate tie drawn outside every `NWell` and
    contacted up to a labelled net is now the NMOS body terminal's real net
    -- not the deck's synthesized `vsubs` global (issue #1414). The PMOS
    body is unaffected (no well tie drawn in this fixture) -- still an
    anonymous, floating net, exactly as it was before this issue's fix."""
    path = _write_gds(
        _make_sg13cmos5l_inverter_layout(substrate_tap_label="STIE"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "inv.spice"))

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


def test_sg13cmos5l_nmos_body_falls_back_to_substrate_global_without_tap(tmp_path):
    """No drawn tie at all: both bodies fall back to exactly the pre-#1414
    behaviour -- the NMOS body terminal ties to the deck's synthesized
    `vsubs` global, and the PMOS body terminal is a floating, anonymous net
    that still fires the `unbiased_pmos_body_nets[]`/`warnings[]` signal --
    the additive-capability guarantee this issue's acceptance criteria
    require for a layout that draws no tie."""
    path = _write_gds(_make_sg13cmos5l_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] == "vsubs"
    assert pfet["nets"]["b"].startswith("\\$")

    assert report["unbiased_pmos_body_nets"] == [
        {"device": pfet["name"], "net": pfet["nets"]["b"]}
    ]
    assert any("no DC bias path" in warning for warning in report["warnings"])


def test_sg13cmos5l_tap_straddling_nwell_boundary_extracts_without_error(tmp_path):
    """A single drawn tie shape straddling the `NWell` boundary -- half
    covered by `nSD` *inside* the well (a well tie), half covered by `pSD`
    *outside* it (a substrate tie), physically one connected `Activ`
    conductor -- is a real electrical short in silicon. Confirms the
    well/substrate derivation (issue #1414) handles this without raising and
    without silently dropping or double-counting the geometry: because the
    two halves are genuinely one connected shape, both device bodies land on
    the *same* net (the tie's own label), correctly reflecting the short a
    real straddling tie would draw in silicon -- mirroring
    `test_sg13g2_tap_straddling_nwell_boundary_extracts_without_error`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # Same NMOS/PMOS/NWell/gate shape as `_make_sg13cmos5l_inverter_layout`.
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

    label(8, 2, "VGND", 200, 500)
    label(8, 2, "Y", 1800, 500)
    label(8, 2, "VPWR", 200, 2500)
    label(8, 2, "Y", 1800, 2500)
    label(8, 2, "A", 1000, 1500)

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
    label(8, 2, "STRADDLE", -300, 1370)

    path = _write_gds(layout, tmp_path / "straddle.gds")
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "straddle.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] is not None
    assert pfet["nets"]["b"] is not None
    assert nfet["nets"]["b"] == pfet["nets"]["b"] == "STRADDLE"
