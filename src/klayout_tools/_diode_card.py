"""Junction-diode card conversion for the netlist normalizer (issues #4319,
#5491).

Split out of :mod:`klayout_tools.netlist_normalize`, which owns the rest of
the ``X``-card dispatch, for the same reason
:mod:`klayout_tools._capacitor_mf` was: one device family's self-contained
conversion rule, in a module that is already among the package's largest.
The symbols this needs from ``netlist_normalize`` are imported *inside* the
functions -- ``netlist_normalize`` imports this module at module level, so
the deferred import is what keeps the pair acyclic, the same idiom
``_capacitor_mf.capacitor_mf_is_m_alias`` uses for ``NormalizeError``.

Which call shape a PDK's diode cells use is a curated fact, not a guess:
:mod:`klayout_tools.pdk_diode_models` records it per family, and the
:class:`~klayout_tools.pdk_models.DeviceLookup` handed in here already
carries the resolved ``length_param``/``width_param`` (or ``None`` for an
``area``/``perim`` family).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _spice_number as spice_number
from .pdk_models import GEOMETRY_STYLE_BARE_UM

if TYPE_CHECKING:
    from .pdk_models import DeviceLookup


def _parse_um2(
    raw_value: str,
    *,
    device: str,
    param: str,
    geometry_style: str | None = None,
) -> float:
    """Parse a SPICE area literal to square micrometres, with the same
    bare-literal convention as :func:`_parse_um` (a suffixed/exponent literal
    is SI m^2; a bare one is already um^2 under a ``BARE_UM`` deck such as
    sky130's ``.option scale=1.0u``)."""
    from .netlist_normalize import NormalizeError

    match = spice_number.NUMBER_RE.match(raw_value.strip())
    if match is None:
        raise NormalizeError(
            f"device '{device}': parameter '{param}' value '{raw_value}' is "
            "not a plain numeric literal"
        )
    mantissa_text, raw_suffix = match.group(1), match.group(2)
    mantissa = float(mantissa_text)
    if not raw_suffix and "e" not in mantissa_text.lower():
        if geometry_style is None:
            raise NormalizeError(
                f"device '{device}': parameter '{param}' value '{raw_value}' "
                "has no unit suffix or exponent, so it is ambiguous whether it "
                "is SI m^2 or already um^2 under an ambient '.option scale' -- "
                'pass reference.deck (e.g. "sky130") or write an explicit '
                "unit-suffixed literal (e.g. area=1p)"
            )
        if geometry_style == GEOMETRY_STYLE_BARE_UM:
            return mantissa
    return mantissa * spice_number.suffix_scale(raw_suffix) * 1e12


def convert_diode_card(
    instance: str,
    nodes: list[str],
    subckt_name: str,
    lookup: DeviceLookup,
    params: dict[str, str],
    *,
    geometry_style: str | None = None,
) -> str:
    """Convert a PDK junction-diode call (``X<n> anode cathode <subckt>
    area=.. perim=..``, issue #4319) to a plain ``D<n> anode cathode <class>
    A=.. P=..`` card, which KLayout's reader reads as ``DeviceClassDiode``
    (terminals ``A``, ``C``; parameters ``A`` um^2, ``P`` um) -- the class the
    layout-side ``DeviceExtractorDiode`` emits. ``perim`` may be spelled
    ``pj``. An omitted parameter is left off the card (class default).
    ``mult``/``m`` > 1 is rejected, as for resistors/capacitors.

    **Geometry-parameterized diode subcircuits** (issue #5491): a binding
    whose :class:`~klayout_tools.pdk_models.DeviceLookup` carries
    ``length_param``/``width_param`` -- today sg13g2's ``dantenna``/
    ``dpantenna``, whose cells take ``l``/``w`` rather than ``area``/``perim``
    -- states its junction as a rectangle instead, and the card's ``A``/``P``
    are derived from it by the same elementary inverse
    ``netlist_normalize._convert_capacitor_card`` already uses for a MiM
    plate (``area = l * w``, ``perimeter = 2 * (l + w)``). This is exactly the
    inverse of the *forward* direction's ``equivalent_rectangle_um`` recovery
    in ``pdk_models.py``, so an ``X ... dantenna l=4.5U w=4.5U`` card written
    by ``klt extract --pdk`` reads back as the same ``A``/``P`` the extractor
    measured -- which is what keeps ``klt lvs`` matching across the
    round-trip. Both dimensions must be given together. A call that supplies
    neither falls through to the ``area``/``perim`` form below, leaving the
    card at the class default rather than inventing the subcircuit's own
    ``l``/``w`` defaults.
    """
    from .netlist_normalize import (
        NormalizeError,
        _format_um,
        _format_um2,
        _parse_um,
        _reject_multiplicity,
        _require_both,
        _resolve_geometry_dimension,
    )

    if len(nodes) != 2:
        raise NormalizeError(
            f"device '{instance}' (subcircuit '{subckt_name}'): expected 2 "
            f"terminals (anode cathode), found {len(nodes)} "
            f"({' '.join(nodes) or '<none>'})"
        )
    _reject_multiplicity(instance, subckt_name, params)
    has_length_param = lookup.length_param is not None and lookup.length_param in params
    has_width_param = lookup.width_param is not None and lookup.width_param in params
    if has_length_param or has_width_param:
        l_um = _resolve_geometry_dimension(
            instance,
            subckt_name,
            lookup.length_param,
            params,
            has_length_param,
            lookup.fixed_length_um,
            geometry_style=geometry_style,
        )
        w_um = _resolve_geometry_dimension(
            instance,
            subckt_name,
            lookup.width_param,
            params,
            has_width_param,
            lookup.fixed_width_um,
            geometry_style=geometry_style,
        )
        _require_both(instance, subckt_name, lookup, l_um is not None, w_um is not None)
        assert l_um is not None and w_um is not None  # _require_both raised otherwise
        area_um2 = l_um * w_um
        perimeter_um = 2.0 * (l_um + w_um)
        return (
            f"{instance} {' '.join(nodes)} {lookup.device_class}"
            f" A={_format_um2(area_um2)} P={_format_um(perimeter_um)}"
        )
    extra = ""
    if "area" in params:
        area_um2 = _parse_um2(
            params["area"],
            device=instance,
            param="area",
            geometry_style=geometry_style,
        )
        extra += f" A={_format_um2(area_um2)}"
    perim = params.get("perim", params.get("pj"))
    if perim is not None:
        perim_um = _parse_um(
            perim, device=instance, param="perim", geometry_style=geometry_style
        )
        extra += f" P={_format_um(perim_um)}"
    return f"{instance} {' '.join(nodes)} {lookup.device_class}{extra}"
