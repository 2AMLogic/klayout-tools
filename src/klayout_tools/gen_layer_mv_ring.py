"""Per-family margins that scope ``mos_array``'s auto tap ring and tap pads to the
medium-voltage rules a ``voltage_flavor`` marker pulls in (issue #6503).

Split out of ``gen_layer_params.py`` as that module's own satellite -- the
same relationship ``gen_layer_params_checks.py`` already has -- because the
repo's monotonic file-size ratchet requires additive work to land in new
files, never as growth to an already-baselined one.

Why two tables rather than one: ``DF.4d_MV`` only holds the ``Comp`` the
``Dualgate`` marker covers, so gf180mcu's ring/tap-pad well needs 0.16um exactly
when the marker is drawn (a 3.3V-only ring keeps ``DF.4d_LV``'s 0.12um,
cleared by the 0.15um default, byte-for-byte), while the marker's own
enclosure of the ring is required whenever both are drawn at all.
"""

from __future__ import annotations

from typing import Any

#: Per-PDK-family nwell enclosure margin (um) of the n-tap ring or pads when
#: the ``voltage_flavor`` marker is *also* drawn (issue #6503) -- applied on
#: top of ``_PDK_RING_WELL_MARGIN_UM`` only for a request whose marker
#: actually resolves. ``0.0`` for a family absent here.
#:
#: ``gf180mcu`` only: the native ``gf180mcu.drc`` runset's ``DF.4d_MV``
#: ("Min. (Nwell overlap of NCOMP) outside DNWELL", 0.16um) holds the
#: ``Comp`` the ``Dualgate`` marker covers to the MV column, and a ``pfet``
#: + ``medium_voltage`` array's marker covers the automatically-sized n-tap
#: ring (see :data:`_PDK_VOLTAGE_FLAVOR_RING_MARGIN_UM`), or the ringless
#: well-tie pads -- so their well needs 0.16um, not the 0.15um default.
#: Keyed apart from ``_PDK_RING_WELL_MARGIN_UM`` so the 3.3V-only array (whose ring only
#: needs ``DF.4d_LV``'s 0.12um, cleared by the 0.15um default) keeps its
#: geometry byte-for-byte.
_PDK_MV_RING_WELL_MARGIN_UM: dict[str, float] = {
    "gf180mcu": 0.16,  # DF.4d_MV (Nwell overlap of NCOMP) is 0.16um
}


def _mv_ring_well_margin_um(family: str) -> float:
    """Return ``family``'s medium-voltage n-tap ring/pad nwell enclosure margin
    (see :data:`_PDK_MV_RING_WELL_MARGIN_UM`), or ``0.0``."""
    return _PDK_MV_RING_WELL_MARGIN_UM.get(family, 0.0)


#: Per-PDK-family margin (um) the ``voltage_flavor`` marker box extends past
#: the guard ring's final tap edge when both are drawn -- ``0.0`` (marker
#: stays anchored to the array core alone, byte-for-byte) for a family
#: absent here.
#:
#: ``gf180mcu`` only (issue #6503): the native ``gf180mcu.drc`` runset makes
#: the ``Dualgate`` marker's relationship to the ring's own ``Comp`` band
#: load-bearing -- ``DV.6`` ("Min. Dualgate enclose COMP (except substrate
#: tap)", 0.24um) fails a marker sitting inside the band with less than
#: 0.24um of enclosure at the default padding, ``DV.7`` (partial overlap)
#: fires while the growing band crosses the marker edge, and once the band
#: escapes it entirely ``DV.3`` ("Min. Dualgate to COMP space [unrelated]",
#: 0.24um) takes over -- a rule-swapping window in ``ring_padding_um`` no
#: single padding choice avoids. Enclosing the ring's final (grid-snapped)
#: edge by exactly the ``DV.6`` margin resolves enclosure and spacing
#: together at every padding; the deck measures with a strict less-than, so
#: exactly the threshold passes (the same no-headroom convention
#: ``_insert_implant_ring`` documents for ``NP.5b``/``PP.5b``).
_PDK_VOLTAGE_FLAVOR_RING_MARGIN_UM: dict[str, float] = {
    "gf180mcu": 0.24,  # DV.6 (Dualgate enclose COMP) is 0.24um
}


def _voltage_flavor_ring_margin_um(family: str) -> float:
    """Return ``family``'s voltage-flavor marker margin past the guard ring
    (see :data:`_PDK_VOLTAGE_FLAVOR_RING_MARGIN_UM`), or ``0.0``."""
    return _PDK_VOLTAGE_FLAVOR_RING_MARGIN_UM.get(family, 0.0)


# The sky130A.lydrc FEOL rules use the resolved hvi.drawing marker (75/20).
# hvdifftap.22 encloses diffusion/tap with HVI. For a PFET, hvdifftap.17
# and .19 also enclose diffusion and n-tap with the intersection of HVI
# and nwell. Both thresholds are exact multiples of the manufacturing grid.
_SKY130_HVI_ENCLOSURE_UM = 0.18
_SKY130_HVNWELL_ENCLOSURE_UM = 0.33


def apply_hv_mark_margins(
    layer_params: dict[str, Any], family: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Apply the native Sky130 HVI enclosures to either MOS generator."""
    mark = layer_params["voltage_flavor_mark_layer"]
    if family != "sky130" or (mark.layer, mark.datatype) != (75, 20):
        return layer_params

    pfet = params.get("flavor") == "pfet"
    enclosure = _SKY130_HVNWELL_ENCLOSURE_UM if pfet else _SKY130_HVI_ENCLOSURE_UM
    layer_params["voltage_flavor_mark_margin_um"] = max(
        layer_params["voltage_flavor_mark_margin_um"], enclosure
    )
    layer_params["voltage_flavor_well_over_mark"] = pfet
    if pfet and "ring_well_margin_um" in layer_params:
        layer_params["ring_well_margin_um"] = max(
            layer_params["ring_well_margin_um"], _SKY130_HVNWELL_ENCLOSURE_UM
        )
    return layer_params


def apply_mos_array_mv_ring_margins(
    layer_params: dict[str, Any],
    family: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Fold issue #6503's two margins into ``mos_array``'s merged hidden
    layer params, in place, and return them.

    ``voltage_flavor_ring_margin_um`` is always resolved (``0.0`` keeps
    every non-gf180mcu family's marker core-anchored); the ring well margin
    is raised to the MV value only when the marker actually resolved *and*
    the ring role did too. GF180 also resolves that role for ringless tap
    pads: their NCOMP lies under Dualgate and needs the same DF.4d_MV
    enclosure. Both well consumers use 0.16um; 3.3V-only requests keep
    their original margin. Ringless marker geometry is unchanged.

    ``params`` (the request) additionally applies
    :func:`apply_hv_mark_margins` (issue #7061).
    """
    layer_params["voltage_flavor_ring_margin_um"] = _voltage_flavor_ring_margin_um(
        family
    )
    if layer_params.get("voltage_flavor_mark_present") and (
        "ring_well_margin_um" in layer_params
    ):
        mv_margin = _mv_ring_well_margin_um(family)
        if mv_margin > layer_params["ring_well_margin_um"]:
            layer_params["ring_well_margin_um"] = mv_margin
    return apply_hv_mark_margins(layer_params, family, params)
