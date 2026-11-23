"""Infer layout-plan device flavors from netlist device classes."""

from __future__ import annotations

from typing import Any

from .gen import GenError
from .gen_layer_params import resistor_flavor_for_class
from .pdk import PdkNotFoundError, find_pdk
from .pdk_families import pdk_variant_family


def apply_resistor_flavor(
    group: dict[str, Any],
    params: dict[str, Any],
    pdk_spec: dict[str, Any],
    family: str | None,
) -> list[str]:
    """Select the first mapped device class and disclose mixed materials."""
    if (
        group["generator"] != "res_array"
        or "flavor" in params
        or "metal_level" in params
    ):
        return []
    if family is None:
        try:
            variant = (
                pdk_spec.get("variant")
                or find_pdk(root=pdk_spec.get("root"))["variant"]
            )
        except PdkNotFoundError as exc:
            raise GenError(str(exc)) from exc
        family = pdk_variant_family(variant)
    selected = []
    for device in group["devices"]:
        flavor = resistor_flavor_for_class(family, device["device_class"])
        if flavor is not None and flavor not in selected:
            selected.append(flavor)
    if not selected:
        return []
    first = selected[0]
    warnings = []
    group_label = f"device_groups (id '{group['id']}')"
    if first != "generic":
        params["flavor"] = first
        warnings.append(
            f"{group_label}: derived params.flavor='{first}' "
            "from the declared resistor class"
        )
    if len(selected) > 1:
        warnings.append(
            f"{group_label}: devices disagree on resistor flavor "
            f"({', '.join(selected)}). Using '{first}'"
        )
    return warnings


#: The generic MOS device-class names a netlist digest reports (KLayout's
#: own ``NFET``/``PFET`` primitive classes, what a ``form: "subckt-call"``
#: reference converts a ``sky130_fd_pr__nfet_*``/``__pfet_*`` subcircuit
#: call to) mapped to the ``flavor`` request parameter ``mos_array`` /
#: ``diff_pair`` draw that class with. An explicit
#: ``params.flavor`` always wins; any other class name simply derives
#: nothing (#6912 -- without this, a ``device_class: "PFET"`` group drew
#: the ``nfet`` default silently, no well, wrong device polarity).
_MOS_FLAVOR_FOR_CLASS = {"NFET": "nfet", "PFET": "pfet"}

#: The generators whose ``klt gen`` request declares that ``flavor`` param.
#: ``esd_device`` is deliberately absent: it has no ``flavor`` option (it is
#: always an NMOS-style device, see ``gen_layer_params.py``), so an injected
#: ``flavor`` would abort the group with ``unknown params: flavor`` -- and a
#: ``pfet`` would mis-trigger the ring generators' Nwell-enclosure branch.
#: It belongs in ``GATE_CONTACT_GENERATORS`` / ``BODY_TIE_GENERATORS`` (it
#: declares ``gate_contact``), not here.
_MOS_FLAVOR_GENERATORS = ("mos_array", "diff_pair")


def apply_mos_flavor(group: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """Derive ``params.flavor`` from the declared MOS class (#6912).

    Mirrors :func:`apply_resistor_flavor` (#6919): a MOS group whose
    ``params`` do not name a ``flavor`` reads it from its declared
    ``devices[].device_class`` -- ``PFET`` draws ``pfet`` (the flavour that
    draws its own well), ``NFET`` draws ``nfet`` -- so a declared PFET can
    no longer silently extract as the wrong polarity. Mixed classes in one
    group warn and use the first, exactly as a mixed-material ``res_array``
    group already does.
    """
    if group["generator"] not in _MOS_FLAVOR_GENERATORS or "flavor" in params:
        return []
    selected = []
    for device in group["devices"]:
        flavor = _MOS_FLAVOR_FOR_CLASS.get((device["device_class"] or "").upper())
        if flavor is not None and flavor not in selected:
            selected.append(flavor)
    if not selected:
        return []
    first = selected[0]
    params["flavor"] = first
    group_label = f"device_groups (id '{group['id']}')"
    warnings = [
        f"{group_label}: derived params.flavor='{first}' from the declared MOS class"
    ]
    if len(selected) > 1:
        warnings.append(
            f"{group_label}: devices disagree on MOS flavor "
            f"({', '.join(selected)}). Using '{first}'"
        )
    return warnings


def apply_derived_flavors(
    group: dict[str, Any],
    params: dict[str, Any],
    pdk_spec: dict[str, Any],
    family: str | None,
) -> list[str]:
    """Apply both flavor derivations in one call: the MOS class derivation
    (:func:`apply_mos_flavor`, #6912) then the resistor-class one
    (:func:`apply_resistor_flavor`, #6919), each a no-op for the other's
    generators."""
    return apply_mos_flavor(group, params) + apply_resistor_flavor(
        group, params, pdk_spec, family
    )
