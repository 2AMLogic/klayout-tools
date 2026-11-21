"""Precision-poly-resistor end-head geometry for ``res_array``.

Sibling of :mod:`klayout_tools.gen_layer_params` and :mod:`klayout_tools.gen`
in the oversized-module split series (#1875, #3722), split out while landing
issue #5704 so the new head-length rule fits the per-file size ratchet. It
holds everything that decides how a contacted end head is drawn: the
per-(PDK family, flavor) rectangular end-contact size (issue #2449), the
minimum end-head length (issue #5704), their two resolvers, the note that
discloses a non-default head length, and the requires-mask box that must cover
the heads (issue #2449). The tables and resolvers moved here from
``gen_layer_params`` (which imports the two resolvers it still calls) and the
mask-box helper from ``gen``; their bodies are unchanged. This module imports
nothing from ``gen``.
"""

from __future__ import annotations

from typing import Any

#: Context-dependent **rectangular** end-contact size per ``(family, flavor)``
#: for ``res_array``'s poly-body path (issue #2449), as ``(w_um, h_um)`` --
#: ``w_um`` along the current-flow axis, ``h_um`` across it.
#:
#: sky130's precision poly resistors (``flavor="high"``/``"xhigh"``, the only
#: ones drawn under ``rpm``/``urpm``) are exempt from the ordinary 0.17um licon
#: rule: ``sky130.lydrc`` ``licon.1b/c`` ("minimum/maximum width/length of
#: licon inside poly resistor : 2.0/0.19um") demands every such licon have
#: edges of exactly 0.19um and 2.0um, i.e. a 0.19um x 2.0um slot. The rule
#: constrains the edge lengths only, not which axis is long; the generator
#: draws the 2.0um edge across the current flow (y), so the slot spans the
#: full resistor head. (The foundry's own ``res_high_po``/``res_xhigh_po``
#: cells draw a *body-width* head with the contact at its far end -- there
#: is no dogbone; ``sky130A.tcl``'s ``res_draw`` sets ``hepl = w/2``, so the
#: head is exactly the resistor body width. The generator keeps its centred
#: slot instead -- issue #5704.) The pitch to a neighbouring
#: contact (``licon.2b``/``licon.2c``: 0.35um / 0.51um) stays met by the
#: unit pitch. An empty/absent entry means the ordinary layer-keyed cut size
#: applies.
_PDK_RES_FLAVOR_END_CONTACT_UM: dict[str, dict[str, tuple[float, float]]] = {
    "sky130": {
        "high": (0.19, 2.0),
        "xhigh": (0.19, 2.0),
    },
}


#: Minimum end-head length (um) along the current-flow axis per
#: ``(family, flavor)`` for ``res_array``'s poly-body path (issue #5704) --
#: how far the contacted end head must extend the resistor body. Resolved as
#: a ``max()`` over the enclosure-derived region :func:`_res_unit_layout`
#: would draw anyway, so a family/flavour absent here keeps the generic
#: enclosure-derived head unchanged.
#:
#: sky130's precision poly resistors (``flavor="high"``/``"xhigh"``) are the
#: only entries: open_pdks ``sky130A.tech``'s DRC (``c6d73a35f524``, inside
#: the ``variants (fast),(full)`` POLY block, so it fires under ``drc(fast)``
#: -- Magic's default style -- as well as ``drc(full)``) demands ``extend
#: xpc/a xhrpoly,uhrpoly 2160`` -- "poly contact extends poly resistor by <
#: 2.16um (licon.1c + li.5)" -- one violation per too-short head. 2.16um is
#: exactly what the foundry's own cells draw: ``sky130A.tcl``'s
#: ``res_to_endcont 1.985`` + ``end_contact_size/2`` 0.095 +
#: ``poly_surround`` 0.08, identical at all five widths and both flavours.
#: Measured 2026-11-15: the generator's previous 0.39um head reported the
#: rule twice per unit; heads >= 2.08um measure clean, and the foundry's
#: 2.16um is taken in preference to that unexplained 0.08um slack below the
#: stated rule.
_PDK_RES_FLAVOR_HEAD_LEN_UM: dict[str, dict[str, float]] = {
    "sky130": {
        "high": 2.16,
        "xhigh": 2.16,
    },
}


def _res_end_contact_um(
    family: str, flavor: str, metal_level: int = 0
) -> tuple[float, float]:
    """``(w_um, h_um)`` of ``res_array``'s rectangular precision-poly end
    contact for this request (see :data:`_PDK_RES_FLAVOR_END_CONTACT_UM`), or
    ``(0.0, 0.0)`` when the ordinary square cut applies -- always for
    ``metal_level != 0`` (metal resistors ignore ``flavor``)."""
    if metal_level:
        return (0.0, 0.0)
    return _PDK_RES_FLAVOR_END_CONTACT_UM.get(family, {}).get(flavor, (0.0, 0.0))


def _res_head_len_um(family: str, flavor: str, metal_level: int = 0) -> float:
    """Minimum end-head length (um) along the current-flow axis for this
    request (see :data:`_PDK_RES_FLAVOR_HEAD_LEN_UM`), or ``0.0`` when the
    generic enclosure-derived head applies -- always for ``metal_level != 0``
    (metal resistors ignore ``flavor``)."""
    if metal_level:
        return 0.0
    return _PDK_RES_FLAVOR_HEAD_LEN_UM.get(family, {}).get(flavor, 0.0)


def _res_head_note(head_len_um: float) -> str:
    """Suffix for ``res_array``'s end-contact ``drc_hints`` note disclosing a
    non-default end-head length (issue #5704), or ``""`` for the generic head."""
    if head_len_um <= 0.0:
        return ""
    return (
        f", extended to {head_len_um}um along the body axis (the PDK's "
        "licon.1c + li.5 precision-resistor contact-extension rule; the unit "
        "pitch grows accordingly)"
    )


def _res_flavor_mask_box_um(
    info: dict[str, Any], enclosure_um: float
) -> tuple[float, float, float, float]:
    """Per-unit box (unit-local um) for a precision-resistor requires-mask
    that must cover the *whole* unit -- body plus contacted dogbone heads --
    rather than just :func:`_res_unit_layout`'s body-only ``"marker"`` box
    (issue #2449).

    sky130's upstream deck only exempts a licon from the ordinary 0.17um
    ``licon.1`` rule inside ``prec_resistor = (rpm | urpm) & psdm &
    poly.interacting(poly_rs)`` (``sky130A_mr.drc``), so ``rpm``/``urpm`` and
    ``psdm`` must enclose the end-pad licons too, as the foundry
    ``res_high_po``/``res_xhigh_po`` cells do. ``enclosure_um`` is the
    official enclosure of the poly resistor by that mask (``rpm.3`` 0.2um,
    ``rpm.4`` 0.11um for ``psdm``). Toward a neighbouring unit (within a row
    or across a row fold) the box grows to half the gap when that is larger,
    so neighbouring masks abut and merge instead of leaving a sliver gap
    below ``rpm.2``'s 0.84um / ``psdm``'s 0.38um spacing floor.
    """
    unit = info["unit"]
    pad_y0 = unit["pad_y0_um"]
    pad_y1 = pad_y0 + unit["pad_height_um"]
    ext_x = max(enclosure_um, info["spacing_um"] / 2.0)
    ext_y = max(enclosure_um, (info["row_pitch_um"] - unit["pad_height_um"]) / 2.0)
    return (-ext_x, pad_y0 - ext_y, unit["total_len_um"] + ext_x, pad_y1 + ext_y)
