"""Device ports, netlist pin promotion, ring binding, and unrouted-net diagnostics
for layout-plan execution.
"""

from __future__ import annotations

import re
from typing import Any

from .gen import GenError
from .gen_layer_params import resistor_flavor_for_class
from .pdk import PdkNotFoundError, find_pdk
from .pdk_families import pdk_variant_family


def device_owned_port_names(ports: list[dict[str, Any]], suffix: str) -> list[str]:
    """Every ``ports[]`` entry whose name ends with ``_<suffix>``, in the
    order ``generate()`` reported them -- see the module docstring's scope
    decision 3 for why this is generator-agnostic (no hardcoded name
    prefix) and why it degrades gracefully (rather than mis-binding) for a
    port-naming shape it does not expect (e.g. ``mos_array``'s
    ``finger_topology: "series"`` mode).
    """
    marker = f"_{suffix}"
    return [
        port["name"]
        for port in ports
        if isinstance(port, dict)
        and isinstance(port.get("name"), str)
        and port["name"].endswith(marker)
    ]


#: Generators whose ``klt gen`` params accept ``gate_contact`` (each reads
#: ``params["gate_contact"]`` in ``gen_describe.py``) and whose digest gate
#: terminal is ``G``.
GATE_CONTACT_GENERATORS = frozenset({"mos_array", "diff_pair", "esd_device"})


def derive_gate_contact(
    group: dict[str, Any],
    digest: dict[str, Any],
    digest_devices_by_key: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    """True when any device the group declares has its gate (``G``) terminal
    on a net with >= 2 terminal connections or on a subckt port
    (``is_pin``) -- such a gate needs a contact + metal pad, because the
    router cannot reach a bare-poly gate (#5093)."""
    if group["generator"] not in GATE_CONTACT_GENERATORS:
        return False
    nets_by_name = {net["name"]: net for net in digest["nets"]}
    for device_ref in group["devices"]:
        digest_device = digest_devices_by_key.get(
            (device_ref["name"], device_ref["device_class"])
        )
        if digest_device is None:
            continue
        net = nets_by_name.get(digest_device["terminals"].get("G"))
        if net is not None and (net.get("is_pin") or len(net["terminals"]) >= 2):
            return True
    return False


def apply_gate_contact(
    resolved: dict[str, Any],
    group: dict[str, Any],
    digest: dict[str, Any] | None,
    digest_devices_by_key: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[str, Any],
) -> list[str]:
    """Set ``resolved["gate_contact"]`` when derived (#5093); return any
    warning. Explicit ``overrides`` still win when layered on afterwards."""
    if digest is None or not derive_gate_contact(group, digest, digest_devices_by_key):
        return []
    resolved["gate_contact"] = True
    if "gate_contact" in overrides:
        return []
    return [gate_contact_warning(group["id"])]


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


def extract_raw_params_by_id(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{device_groups[].id: device_groups[].params}`` read from the
    **original** request, not the validated response.

    :func:`klayout_tools.layout_plan.validate_layout_plan`'s own response
    deliberately echoes only the fields it validates in detail (``id``,
    ``generator``, ``topology``, ``devices``, ``encloses``) -- ``params`` is
    checked for JSON-object shape only and not echoed back, so this module
    reads it from ``request`` directly (already known well-typed, since it
    passed Phase B's own validation) rather than trying to smuggle it
    through the validated response.
    """
    raw_groups = request.get("device_groups")
    if not isinstance(raw_groups, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue
        group_id = raw_group.get("id")
        params = raw_group.get("params")
        if isinstance(group_id, str) and isinstance(params, dict):
            result[group_id] = params
    return result


def gate_contact_warning(group_id: str) -> str:
    return (
        f"device_groups (id '{group_id}'): derived params.gate_contact=true "
        "because a declared device's gate net has >= 2 terminal connections "
        "or is a subckt port"
    )


def port_pins(
    digest: dict[str, Any],
    net_pins: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Promote each subckt port net (``is_pin``) to a labelled top-level pin
    on its first resolved device port (#4966); returns ``(pins, warnings)``.
    A port net with no resolved device port gets a warning and no label."""
    pins: list[dict[str, str]] = []
    warnings: list[str] = []
    for net in digest["nets"]:
        if not net.get("is_pin"):
            continue
        resolved = net_pins.get(net["name"])
        if not resolved:
            warnings.append(
                f"netlist port '{net['name']}' has no mapped device port -- "
                "no pin label is written for it"
            )
            continue
        pins.append(
            {
                "net": net["name"],
                "block": resolved[0]["block"],
                "port": resolved[0]["port"],
            }
        )
    return pins, warnings


_UNROUTED_NOTE_RE = re.compile(
    r"^net '(?P<net>.*)' could not be routed: (?P<reason>.*)$"
)


def unrouted_reasons(composed: dict[str, Any]) -> list[dict[str, str]]:
    """``[{"net", "reason"}]`` parsed from ``compose()``'s
    ``drc_hints.notes`` ``net '<n>' could not be routed: <reason>`` entries
    (#4966), so a caller sees why a net is in ``unrouted_nets[]``."""
    reasons: list[dict[str, str]] = []
    for note in (composed.get("drc_hints") or {}).get("notes") or []:
        match = _UNROUTED_NOTE_RE.match(note) if isinstance(note, str) else None
        if match:
            reasons.append({"net": match["net"], "reason": match["reason"]})
    return reasons


def bind_ring_nets(
    device_groups: list[dict[str, Any]],
    generated: dict[str, dict[str, Any]],
    net_pins: dict[str, list[dict[str, str]]],
    warnings: list[str],
) -> None:
    """Group-level tap/ring nets (#5095): a group declaring ``net`` binds its
    ring to that digest net via its first generated port (the ring is one
    closed conductor; routing all four TAP_* ports would only wire it to
    itself)."""
    for group in device_groups:
        ring_net = group.get("net")
        if ring_net is None:
            continue
        names = [
            p["name"]
            for p in generated[group["id"]].get("ports") or []
            if isinstance(p, dict) and isinstance(p.get("name"), str)
        ]
        if not names:
            warnings.append(
                f"device_groups (id '{group['id']}'): generator "
                f"'{group['generator']}' reported no ports, so net "
                f"'{ring_net}' cannot be bound to the ring"
            )
            continue
        pin = {"block": group["id"], "port": names[0]}
        if pin not in net_pins.setdefault(ring_net, []):
            net_pins[ring_net].append(pin)
