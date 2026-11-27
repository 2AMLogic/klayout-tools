"""Read-side subcircuit-name aliases for reference-netlist conversion.

Issue #7031: the current gf180mcuD PDK spells its MIM capacitors
``cap_mim_{1f0,1f5,2f0}fF`` -- the names its own LVS deck and foundry CDL
regression netlists carry, with the case-folded ``...ff`` spelling its
``sm141064.ngspice`` simulation models define -- while the curated
extraction deck keeps the previous revision's
``cap_mim_*_m4m5_noshield`` class names for netlist stability (see
``decks/gf180mcu.py``). Both spellings name the same drawn device at the
same density, so a reference netlist written against either revision's
naming should convert the same way instead of failing the plain SPICE
reader (a valueless ``C`` card) or the unknown-device check (an ``X``
card).

The aliases here are **read-side only**: :func:`capacitor_lookup_bindings`
merges them into the maps ``klt lvs``'s reference conversion resolves
cards against, while the extraction write direction
(``pdk_models.resolve_device_bindings``) keeps reading the curated
class-keyed ``_CAPACITOR_MODEL_TABLE`` directly -- an extracted netlist
still emits exactly the ``cap_mim_*_m4m5_noshield`` subcircuit names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pdk_models import DeviceLookup

#: (deck_name, pdk_variant_family) -> {alias subckt name -> deck class name}.
#: The ``X``-card route resolves exact-case only, so each density needs both
#: the foundry-CDL ``fF`` and the ngspice-model ``ff`` spelling as keys; the
#: valueless-``C`` route's ``.lower()`` fallback then also matches either
#: input case.
_CAPACITOR_SUBCKT_ALIASES: dict[tuple[str, str], dict[str, str]] = {
    ("gf180mcu", "gf180mcu"): {
        "cap_mim_1f0fF": "cap_mim_1f0_m4m5_noshield",
        "cap_mim_1f0ff": "cap_mim_1f0_m4m5_noshield",
        "cap_mim_1f5fF": "cap_mim_1f5_m4m5_noshield",
        "cap_mim_1f5ff": "cap_mim_1f5_m4m5_noshield",
        "cap_mim_2f0fF": "cap_mim_2f0_m4m5_noshield",
        "cap_mim_2f0ff": "cap_mim_2f0_m4m5_noshield",
    },
}


def capacitor_lookup_bindings(
    curated: Mapping[str, str] | None, deck_name: str, family: str
) -> dict[str, DeviceLookup]:
    """``<subckt-name> -> DeviceLookup("capacitor", ...)`` for the
    reference-read direction: one ``_CAPACITOR_MODEL_TABLE`` pair's curated
    ``{deck class -> subckt}`` mapping (``None`` when the pair has no
    curated entry), inverted and extended with this module's subcircuit-name
    aliases (issue #7031), so an aliased spelling and its curated twin
    resolve through the identical ``DeviceLookup`` -- same deck class, same
    call-site length/width parameter names.

    Aliases are merged with :meth:`dict.setdefault`, so a curated subcircuit
    always wins if a future PDK revision ever makes a name collide. Read
    side only: the extraction write direction never calls this (see the
    module docstring). ``DeviceLookup`` and the pair's parameter style are
    imported lazily, exactly like ``pdk_diode_models.diode_lookup_bindings``,
    so importing this module never cycles back into ``pdk_models``.
    """
    from .pdk_models import _CAPACITOR_PARAM_STYLE, DeviceLookup

    length_param, width_param = _CAPACITOR_PARAM_STYLE.get(family, ("l", "w"))
    subckt_to_class = {
        subckt: device_class for device_class, subckt in (curated or {}).items()
    }
    aliases = _CAPACITOR_SUBCKT_ALIASES.get((deck_name, family), {})
    for alias, device_class in aliases.items():
        subckt_to_class.setdefault(alias, device_class)
    return {
        subckt: DeviceLookup("capacitor", device_class, length_param, width_param)
        for subckt, device_class in subckt_to_class.items()
    }
