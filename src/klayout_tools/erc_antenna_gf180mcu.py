"""gf180mcu antenna-ratio limit table for `klt erc --pdk gf180mcu` (#4003).

Split out of `erc.py` to respect the per-file size ratchet; `erc.py` registers
it alongside the sky130 and ihp-sg13g2 tables.
"""

from __future__ import annotations

from typing import Any

from ._layout import ActiveIndex, region

#: gf180mcu antenna-ratio limit (maximum ratio *without* a protection diode),
#: from google/gf180mcu-pdk at commit f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7
#: (the same pin as `decks/gf180mcu.py`'s `_GF180MCU_KLAYOUT_DRC_DECK_COMMIT`),
#: `libs.tech/klayout/tech/drc/rule_decks/antenna.rb`. Every metal level has
#: the same limit, 400: Metal1 `ANT.16_i_ANT.2`, Metal2 `.3`, Metal3 `.4`,
#: Metal4 `.5`, Metal5 `.6`, MetalTop `.7` (the thick-gate `ANT.16_ii_*`
#: variants carry the same 400). The limit does not vary by stack, so one
#: table covers 3LM/4LM/5LM/6LM; a role is only graded when the spec names it.
#:
#: Approximation to be aware of: the deck's metal rules measure
#: `perimeter(layer) * thickness / gate area` (0.54 um thickness; the
#: deck's `metal_top_thickness` for the topmost metal), *not* `klt erc`'s
#: cumulative connected *area* / gate area. This is the same
#: connectivity-area-only approximation the sky130 table carries; the verdict
#: here is not exactly equivalent to the deck's own check. The deck also
#: applies a diode credit (`darea * mf`, mf = 2 thin / 15 thick gate); with
#: no diode it reduces to the plain 400, which is what is used here.
#:
#: Not graded (not representable as a `levels[]` verdict): `ANT.1` (Poly2
#: perimeter / gate, 200), `ANT.8` (Contact / gate, 10), the via ratios
#: `ANT.16_*_ANT.9`..`ANT.13` (Via1..Via5 area / gate, 20), the
#: MIM-referenced `ANT.16_iii_*` rules, and the with-diode credit (`klt erc`
#: does not model protection diodes).
ANTENNA_RATIO_MAX_EGAR: dict[str, tuple[float, str]] = {
    "Metal1": (400.0, "ANT.16_i_ANT.2"),
    "Metal2": (400.0, "ANT.16_i_ANT.3"),
    "Metal3": (400.0, "ANT.16_i_ANT.4"),
    "Metal4": (400.0, "ANT.16_i_ANT.5"),
    "Metal5": (400.0, "ANT.16_i_ANT.6"),
    "MetalTop": (400.0, "ANT.16_i_ANT.7"),
}

ANTENNA_SOURCE_URL = (
    "https://github.com/google/gf180mcu-pdk/blob/"
    "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7/"
    "libs.tech/klayout/tech/drc/rule_decks/antenna.rb"
)

ANTENNA_SOURCE_NOTE = (
    "gf180mcu-pdk f6eeac7, w/o-diode limit (area approximates perimeter)"
)


def annotate_contacted_diffusion(
    violations: list[tuple[dict[str, Any], Any]],
    layout: Any,
    top: Any,
    stackup: list[dict[str, Any]],
    vias: list[dict[str, Any]],
    active: Any,
    l2n: Any,
) -> None:
    """Measure contacted COMP beyond all drawn Poly2 on each violating final net."""
    if not violations:
        return
    for gate, _ in violations:
        gate["has_contacted_diffusion"] = None
    poly = stackup[0]
    if poly["layer"] != (30, 0) or poly["active_layer"] != (22, 0) or active is None:
        return
    metals = {entry["name"] for entry in stackup if entry["layer"] == (34, 0)}
    pairs = [
        (l2n.layer_index(metal), l2n.layer_index(via["name"]))
        for metal in metals
        for via in vias
        if via["layer"] == (33, 0) and set(via["between"]) == {poly["name"], metal}
    ]
    if not pairs:
        return
    # Bbox-indexed (issue #2751's `ActiveIndex`): a bare `net_geometry &
    # outside_channel` visits every active polygon in the chip once per
    # violating gate -- quadratic in design size, and on a routed block nearly
    # every gate net violates.
    outside_channel = ActiveIndex(active - region(layout, top, poly["layer"]))
    for gate, net in violations:
        gate["has_contacted_diffusion"] = any(
            not outside_channel.intersect(
                (
                    l2n.polygons_of_net(net, metal) & l2n.polygons_of_net(net, cut)
                ).merged()
            ).is_empty()
            for metal, cut in pairs
        )
