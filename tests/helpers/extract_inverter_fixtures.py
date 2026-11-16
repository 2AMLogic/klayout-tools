"""Shared synthetic per-PDK inverter layout builders for `klt extract` tests.

Lifted out of `tests/test_extract.py` (issue #5596) when the per-PDK
body/substrate-tap resolution tests moved into their own
`tests/test_extract_body_nets_*.py` sibling modules: these two builders are
called both from those new modules *and* from tests that stayed behind in
`tests/test_extract.py`, so they belong to neither and live here instead.

Each builder draws the same minimal inverter topology -- one NMOS with its
active outside the n-well, one PMOS with its active inside it, sharing a
single poly gate bar, contacted up to metal1 with a pin label on every net
-- on one PDK's own layer numbers, with optional well-/substrate-tie
geometry for the derived-tap mechanism (`tap_nplus`/`tap_pplus`). The
sky130 original, `_make_inverter_layout`, stays in `tests/test_extract.py`:
it is used almost exclusively there.
"""

from __future__ import annotations

import klayout.db as kdb


def _make_gf180mcu_inverter_layout(
    *, well_tap_label: str | None = None, substrate_tap_label: str | None = None
) -> kdb.Layout:
    """A minimal gf180mcu inverter (NMOS `Comp` outside `Nwell`, PMOS `Comp`
    inside it, sharing a `Poly2` gate, contacted up through `Contact`/
    `Metal1`) -- the gf180mcu-layer-number counterpart of
    `tests/test_extract.py`'s `_make_inverter_layout`, shaped to exercise
    the deck's derived
    `tap_nplus`/`tap_pplus` well-/substrate-tie mechanism (issue #1084).

    When `well_tap_label` is given, an `Nplus`-covered `Comp` strip is drawn
    *inside* the `Nwell` (a well tie -- n+ diffusion tied to the n-type
    well), contacted + labelled with the given text. When
    `substrate_tap_label` is given, a `Pplus`-covered `Comp` strip is drawn
    *outside* every `Nwell` (a substrate tie -- p+ diffusion tied to the
    p-type substrate), likewise contacted + labelled. Both off to the side
    (x -450..-150), clear of the transistors' own `Comp` geometry (x
    0..2000), so neither can be mistaken for ordinary source/drain
    diffusion. Either or both left `None` (the default) draws no
    corresponding tie -- exercising the pre-#1084 fallback behaviour."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # NMOS: Comp strip 0..2000 x 0..1000 (outside Nwell). PMOS: Comp strip
    # 0..2000 x 2000..3000 (inside Nwell -500..2500 x 1500..3500). One
    # continuous Poly2 gate bar crosses both, mirroring
    # `tests/test_extract.py`'s `_make_inverter_layout`'s sky130 shape
    # one-for-one on gf180mcu's own layer numbers.
    draw(22, 0, kdb.Box(0, 0, 2000, 1000))  # Comp (nmos active)
    draw(22, 0, kdb.Box(0, 2000, 2000, 3000))  # Comp (pmos active)
    draw(21, 0, kdb.Box(-500, 1500, 2500, 3500))  # Nwell
    draw(30, 0, kdb.Box(800, -200, 1200, 3200))  # Poly2 (shared gate bar)

    for y0 in (0, 2000):
        draw(33, 0, kdb.Box(100, y0 + 300, 300, y0 + 700))  # Contact (S side)
        draw(33, 0, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # Contact (D side)
        draw(34, 0, kdb.Box(0, y0 + 200, 400, y0 + 800))  # Metal1 (S side)
        draw(34, 0, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # Metal1 (D side)

    draw(33, 0, kdb.Box(900, 1400, 1100, 1600))  # gate Contact
    draw(34, 0, kdb.Box(850, 1350, 1150, 1650))  # gate Metal1

    label(34, 10, "VGND", 200, 500)
    label(34, 10, "Y", 1800, 500)
    label(34, 10, "VPWR", 200, 2500)
    label(34, 10, "Y", 1800, 2500)
    label(34, 10, "A", 1000, 1500)

    if well_tap_label is not None:
        # Well tie: Nplus-covered Comp inside the Nwell, off to the side so
        # it never touches the PMOS's own Comp strip.
        draw(22, 0, kdb.Box(-400, 2400, -200, 2600))  # Comp
        draw(32, 0, kdb.Box(-400, 2400, -200, 2600))  # Nplus (well-tie doping)
        draw(33, 0, kdb.Box(-380, 2450, -220, 2550))  # Contact
        draw(34, 0, kdb.Box(-450, 2400, -150, 2600))  # Metal1
        label(34, 10, well_tap_label, -300, 2500)

    if substrate_tap_label is not None:
        # Substrate tie: Pplus-covered Comp outside every Nwell, well below
        # the NMOS active strip (y < 0) so it never touches it.
        draw(22, 0, kdb.Box(-400, -800, -200, -200))  # Comp
        draw(31, 0, kdb.Box(-400, -800, -200, -200))  # Pplus (substrate-tie doping)
        draw(33, 0, kdb.Box(-380, -650, -220, -550))  # Contact
        draw(34, 0, kdb.Box(-450, -700, -150, -500))  # Metal1
        label(34, 10, substrate_tap_label, -300, -600)

    return layout


def _make_sg13g2_inverter_layout(
    *, well_tap_label: str | None = None, substrate_tap_label: str | None = None
) -> kdb.Layout:
    """A minimal sg13g2 inverter (NMOS `Activ` outside `NWell`, PMOS
    `Activ` inside it, sharing a `GatPoly` gate, contacted up through
    `Cont`/`Metal1`) -- the sg13g2-layer-number counterpart of
    `_make_gf180mcu_inverter_layout`, shaped to exercise the deck's derived
    `tap_nplus`/`tap_pplus` well-/substrate-tie mechanism (issue #1273).

    When `well_tap_label` is given, an `nSD`-covered `Activ` strip is drawn
    *inside* the `NWell` (a well tie -- n+ diffusion tied to the n-type
    well), contacted + labelled with the given text. When
    `substrate_tap_label` is given, a `pSD`-covered `Activ` strip is drawn
    *outside* every `NWell` (a substrate tie -- p+ diffusion tied to the
    p-type substrate), likewise contacted + labelled. Both off to the side
    (x -450..-150), clear of the transistors' own `Activ` geometry (x
    0..2000), so neither can be mistaken for ordinary source/drain
    diffusion. Either or both left `None` (the default) draws no
    corresponding tie -- exercising the pre-#1273 fallback behaviour."""
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
    # `_make_gf180mcu_inverter_layout`'s shape one-for-one on sg13g2's own
    # layer numbers.
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

    label(8, 25, "VGND", 200, 500)
    label(8, 25, "Y", 1800, 500)
    label(8, 25, "VPWR", 200, 2500)
    label(8, 25, "Y", 1800, 2500)
    label(8, 25, "A", 1000, 1500)

    if well_tap_label is not None:
        # Well tie: nSD-covered Activ inside the NWell, off to the side so
        # it never touches the PMOS's own Activ strip.
        draw(1, 0, kdb.Box(-400, 2400, -200, 2600))  # Activ
        draw(7, 0, kdb.Box(-400, 2400, -200, 2600))  # nSD (well-tie doping)
        draw(6, 0, kdb.Box(-380, 2450, -220, 2550))  # Cont
        draw(8, 0, kdb.Box(-450, 2400, -150, 2600))  # Metal1
        label(8, 25, well_tap_label, -300, 2500)

    if substrate_tap_label is not None:
        # Substrate tie: pSD-covered Activ outside every NWell, well below
        # the NMOS active strip (y < 0) so it never touches it.
        draw(1, 0, kdb.Box(-400, -800, -200, -200))  # Activ
        draw(14, 0, kdb.Box(-400, -800, -200, -200))  # pSD (substrate-tie doping)
        draw(6, 0, kdb.Box(-380, -650, -220, -550))  # Cont
        draw(8, 0, kdb.Box(-450, -700, -150, -500))  # Metal1
        label(8, 25, substrate_tap_label, -300, -600)

    return layout
