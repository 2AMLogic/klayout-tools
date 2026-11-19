"""Pure geometry for the ``diode`` generator (issues #4300, #6614).

A junction diode: ``n_psub`` (n+ diffusion in the p-substrate, the anode being
the substrate itself, tied through a separate p+ tap) or ``p_nwell`` (p+
diffusion in an nwell, the cathode being an n+ tap in that well). The layout
function is kept free of ``klayout`` and ``gen.py`` imports so the PCell and
the describe/validate callbacks share it; only ``resolve_diode_voltage_class``
reaches into ``gen`` (lazily, for ``GenError``).

Two PDK families are drawn. ``voltage_class`` names the *flavour*, and each
flavour belongs to exactly one family, so it also selects the constants:

- sky130 -- ``05v5`` (default) and ``11v0`` (adds ``hvi``). ``areaid.ed``
  (81/19) is deliberately never drawn: the sky130 extraction deck treats it as
  an *exclusion* on the diode terminals, so an ESD-flagged diode would not
  extract as ``diode_*`` at all.
- gf180mcu -- ``06v0``, the only flavour the curated deck recognises
  (``diode_nd2ps_06v0`` / ``diode_pd2nw_06v0``). It is the ``Dualgate`` (55/0)
  thick-oxide flavour: without that marker the diode extracts as nothing. The
  layer table's ``esd_mark`` role *is* ``Dualgate`` there, so the box under
  that role key is the Dualgate box (sky130 has no such role and never draws
  one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DIODE_KINDS = ("n_psub", "p_nwell")

#: Voltage classes each supported PDK family accepts; the first entry is the
#: family's native flavour, which an omitted ``voltage_class`` resolves to.
DIODE_FAMILY_VOLTAGE_CLASSES = {
    "sky130": ("05v5", "11v0"),
    "gf180mcu": ("06v0",),
}
#: Every class some family accepts (family-agnostic validation).
DIODE_VOLTAGE_CLASSES = ("05v5", "11v0", "06v0")

#: Smallest diffusion edge (um) that fits one contact with enclosure margin.
DIODE_MIN_EDGE_UM = 0.4

_TAP_GAP_UM = 0.7
_TAP_WIDTH_UM = 0.4
_HVI_ENCLOSURE_UM = 0.4

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class _DiodeRules:
    """One flavour's constants (um); see the module-level instances below."""

    contact_um: float
    pitch_um: float
    #: Diffusion edge to the first contact cut.
    inset_um: float
    #: Metal enclosure of the contact array.
    metal_enclosure_um: float
    #: Implant (nsdm/psdm, Nplus/Pplus) extension beyond its diffusion.
    implant_enclosure_um: float
    #: Recognition-mark extension beyond the junction diffusion.
    marker_enclosure_um: float
    #: Nwell extension beyond the junction diffusion and the well tap.
    well_margin_um: float
    #: Dualgate extension beyond the whole cell; ``None`` = no Dualgate.
    dualgate_margin_um: float | None = None
    #: Manufacturing grid the contact array is snapped to; ``None`` = none.
    grid_um: float | None = None


_SKY130 = _DiodeRules(
    contact_um=0.17,
    pitch_um=0.34,
    inset_um=0.115,
    metal_enclosure_um=0.08,
    implant_enclosure_um=0.125,
    marker_enclosure_um=0.2,
    well_margin_um=0.4,
)

# Every value is a multiple of the 0.005 um `ongrid` grid the curated deck
# checks on all of these layers (decks/gf180mcu.py `_OFFGRID_ROWS`).
_GF180MCU = _DiodeRules(
    # contact.width.1: a fixed 0.22 um square.
    contact_um=0.22,
    # contact.space.1 is 0.25 um; 0.28 um of space leaves margin.
    pitch_um=0.5,
    # comp.enclosing.contact.1 is 0.07 um; +0.005 absorbs the grid snap
    # (at most 0.0025 um) so the enclosure never dips under the rule.
    inset_um=0.075,
    # metal1.enclosing.contact.1 is 0.005 um; 0.1 um makes a pad over one
    # contact 0.42 um wide, well clear of metal1.width.1 (0.23 um).
    metal_enclosure_um=0.1,
    # nplus.enclosing.comp.1 / pplus.enclosing.comp.1 are 0.16 um.
    implant_enclosure_um=0.2,
    marker_enclosure_um=0.2,
    # nwell.enclosing.comp.1 is 0.12 um. The 5V/6V `DF.4*_MV` Nwell/Comp
    # rules are not transcribed in the curated deck (its UNMODELED_RULES), so
    # 0.6 um is a conservative choice that is NOT deck-verified.
    well_margin_um=0.6,
    # The deck only checks dualgate.width.1 / dualgate.space.1; the enclosure
    # of the covered Comp is not transcribed, so 0.4 um (the sky130 hvi
    # margin) is NOT deck-verified either.
    dualgate_margin_um=0.4,
    grid_um=0.005,
)

_RULES = {"05v5": _SKY130, "11v0": _SKY130, "06v0": _GF180MCU}


def _grow(box: Box, margin: float) -> Box:
    return (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)


def _snap(value: float, grid: float | None) -> float:
    return value if not grid else round(value / grid) * grid


def _contacts(box: Box, rules: _DiodeRules) -> tuple[list[Box], Box]:
    """A grid of contacts inside diffusion ``box`` and the metal box covering them."""
    x0, y0, x1, y1 = box
    cut, pitch = rules.contact_um, rules.pitch_um
    nx = max(1, int((x1 - x0 - 2 * rules.inset_um - cut) / pitch) + 1)
    ny = max(1, int((y1 - y0 - 2 * rules.inset_um - cut) / pitch) + 1)
    span_x = (nx - 1) * pitch + cut
    span_y = (ny - 1) * pitch + cut
    cx0 = _snap((x0 + x1 - span_x) / 2, rules.grid_um)
    cy0 = _snap((y0 + y1 - span_y) / 2, rules.grid_um)
    cuts = [
        (
            cx0 + i * pitch,
            cy0 + j * pitch,
            cx0 + i * pitch + cut,
            cy0 + j * pitch + cut,
        )
        for i in range(nx)
        for j in range(ny)
    ]
    return cuts, _grow((cx0, cy0, cx0 + span_x, cy0 + span_y), rules.metal_enclosure_um)


def _diode_layout(
    kind: str, voltage_class: str, width_um: float, length_um: float
) -> dict[str, Any]:
    """Boxes (um) per role, plus the two terminal metal boxes.

    The junction diffusion's lower-left corner is the origin; the tap
    (substrate tap for ``n_psub``, well tap for ``p_nwell``) sits to its right.
    """
    rules = _RULES[voltage_class]
    diff: Box = (0.0, 0.0, width_um, length_um)
    tap: Box = (
        width_um + _TAP_GAP_UM,
        0.0,
        width_um + _TAP_GAP_UM + _TAP_WIDTH_UM,
        length_um,
    )
    diff_cuts, diff_li = _contacts(diff, rules)
    tap_cuts, tap_li = _contacts(tap, rules)

    junction_sdm = "nplus" if kind == "n_psub" else "pplus"
    tap_sdm = "pplus" if kind == "n_psub" else "nplus"
    boxes: dict[str, list[Box]] = {
        "active": [diff],
        "tap": [tap],
        junction_sdm: [_grow(diff, rules.implant_enclosure_um)],
        tap_sdm: [_grow(tap, rules.implant_enclosure_um)],
        "diode_mark": [_grow(diff, rules.marker_enclosure_um)],
        "contact": diff_cuts + tap_cuts,
        "metal": [diff_li, tap_li],
    }
    if kind == "p_nwell":
        boxes["well"] = [
            (
                -rules.well_margin_um,
                -rules.well_margin_um,
                tap[2] + rules.well_margin_um,
                length_um + rules.well_margin_um,
            )
        ]
    if voltage_class == "11v0":
        # nwell.9: a well touching hvi must sit wholly inside it.
        hvi_base = boxes["well"][0] if kind == "p_nwell" else diff
        boxes["hvi"] = [_grow(hvi_base, _HVI_ENCLOSURE_UM)]
    if rules.dualgate_margin_um is not None:
        # One Dualgate box over the whole cell (well included), so both the
        # junction and its tap are in the 6V domain.
        boxes["esd_mark"] = [_grow(_bbox(boxes), rules.dualgate_margin_um)]

    # The junction side's terminal is the diffusion's metal; the other is the tap's.
    if kind == "n_psub":
        anode_li, cathode_li = tap_li, diff_li
    else:
        anode_li, cathode_li = diff_li, tap_li
    return {
        "boxes": boxes,
        "anode_li": anode_li,
        "cathode_li": cathode_li,
        "bbox_um": _bbox(boxes),
    }


def _bbox(boxes: dict[str, list[Box]]) -> Box:
    every = [b for group in boxes.values() for b in group]
    return (
        min(b[0] for b in every),
        min(b[1] for b in every),
        max(b[2] for b in every),
        max(b[3] for b in every),
    )


def resolve_diode_voltage_class(
    raw_params: dict[str, Any], resolved: dict[str, Any], variant: str
) -> None:
    """Make ``resolved["voltage_class"]`` valid for ``variant``'s PDK family.

    An *omitted* ``voltage_class`` resolves to the family's native flavour
    (``05v5`` on sky130, ``06v0`` on gf180mcu), so a caller who passes nothing
    gets a recognised device; an explicit one must be a flavour that family
    has. A family with no diode support is left alone -- ``layer_params``
    reports it ("not yet supported").
    """
    from .gen import GenError
    from .gen_layer_params import _pdk_family

    family = _pdk_family(variant)
    allowed = DIODE_FAMILY_VOLTAGE_CLASSES.get(family)
    if allowed is None:
        return
    if "voltage_class" not in raw_params:
        resolved["voltage_class"] = allowed[0]
    elif resolved["voltage_class"] not in allowed:
        raise GenError(
            f"generator 'diode': params.voltage_class {resolved['voltage_class']!r} "
            f"is not a {family} flavour -- must be one of {list(allowed)}"
        )


def diode_notes(kind: str, voltage_class: str) -> list[str]:
    """The ``drc_hints.notes`` for a diode of this ``kind``/``voltage_class``."""
    if voltage_class == "06v0":
        notes = [
            "Dualgate (55/0) covers the whole cell -- without it the diode "
            "extracts as nothing; its enclosure margin is not deck-verified. "
            "The diode_mk (115/5) mark covers the junction only"
        ]
    else:
        notes = [
            "areaid.ed (81/19) is never drawn -- the extraction deck treats it "
            "as an exclusion, so an ESD-flagged diode would not extract as diode_*"
        ]
    if kind == "n_psub":
        notes.append(
            "ANODE is a p+ substrate tap; the p-substrate itself is the anode "
            "terminal in the extracted netlist"
        )
    return notes
