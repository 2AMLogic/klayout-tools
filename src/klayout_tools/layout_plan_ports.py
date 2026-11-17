"""Port-name and pin/unrouted-reason helpers for ``layout_plan_execute``
(#4966): device-owned port lookup, netlist-port pin promotion, parsing
of ``compose()``'s unrouted-net notes, the per-group param-resolution
helpers (netlist-derived size params, #5765) and the ``diff_pair``
distinct-legs guard (#5765). Split out to keep ``layout_plan_execute.py``
under its file-size ceiling.
"""

from __future__ import annotations

import re
from typing import Any

from ._jsonnum import is_number


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


#: Which resolved digest params (MOS/resistor ``L``/``W``) map onto which
#: ``klt gen`` params field, per generator -- see ``layout_plan_execute``'s
#: module docstring's scope decision 1. Moved here from
#: ``layout_plan_execute.py`` with :func:`_netlist_derived_size_params`
#: (#5765) to keep that module under its file-size ceiling.
_SIZE_PARAM_TARGETS: dict[str, dict[str, str]] = {
    "mos_array": {"L": "l_um", "W": "w_um"},
    "diff_pair": {"L": "l_um", "W": "w_um"},
    "esd_device": {"L": "l_um", "W": "finger_width_um"},
    "res_array": {"L": "length_um", "W": "width_um"},
}


def _netlist_derived_size_params(
    generator: str, device_params: dict[str, Any]
) -> dict[str, float]:
    """Pick the ``L``/``W``-style digest params a given generator's own
    ``klt gen`` params can absorb, per :data:`_SIZE_PARAM_TARGETS`.

    Only a **positive** digest value counts as "the netlist specified this
    device's geometry" -- ``NetlistSpiceReader`` reports ``L``/``W`` as
    ``0.0`` (KLayout's own structural default for an unset device-class
    param) for a resistor written in the bare, purely-electrical
    ``R<name> n1 n2 <ohms>`` SPICE form (no ``L=``/``W=`` at all), which is
    "the netlist did not specify a geometric size for this device," not "the
    netlist specifies a zero-sized device" -- treating it as the latter
    would hand a real generator a rejected (``> 0`` required) param instead
    of falling back to its own documented default.
    """
    targets = _SIZE_PARAM_TARGETS.get(generator)
    if not targets:
        return {}
    resolved: dict[str, float] = {}
    for digest_key, gen_param in targets.items():
        value = device_params.get(digest_key)
        value_is_number = is_number(value)
        if value_is_number and value > 0:
            resolved[gen_param] = float(value)
    return resolved


def diff_pair_leg_error(
    group: dict[str, Any],
    digest_devices_by_key: dict[tuple[str, str], dict[str, Any]],
) -> str | None:
    """Application-error text when a ``diff_pair`` group's declared devices
    are not two electrically distinct legs (#5765), else ``None``.

    A differential pair's two legs necessarily differ on their gate *and*
    drain nets; two digest devices that agree on either are parallel
    fingers of one schematic instance -- typically a folded ``nf > 1`` MOS,
    which a ``form: "subckt-call"`` digest expands into per-finger devices
    named ``<instance>_F0``, ``<instance>_F1``, ... (issue #1487) -- or the
    same device declared twice, and binding both as legs shorts the pair
    (the k-th-device binding ties the legs' shared net to both legs' ports
    and leaves the second leg's own nets unmapped). Scoped to ``diff_pair``
    only: a ``mos_array`` group legitimately declares devices that share a
    gate net (a bias rail), and every other generator has no ``G``/``D``
    terminals at all. The caller raises the error with the returned text --
    this module cannot import
    :class:`klayout_tools.layout_plan_execute.LayoutPlanExecuteError`
    without a circular import.
    """
    if group["generator"] != "diff_pair" or len(group["devices"]) < 2:
        return None
    resolved = [
        (
            device_ref["name"],
            digest_devices_by_key.get((device_ref["name"], device_ref["device_class"])),
        )
        for device_ref in group["devices"]
    ]
    for index, (name_i, device_i) in enumerate(resolved):
        for name_j, device_j in resolved[index + 1 :]:
            if device_i is None or device_j is None:
                # An unresolvable device reference is Phase B's own
                # validation error (exit 1, "does not resolve"); skip it
                # here exactly like derive_gate_contact does.
                continue
            shared = [
                f"terminal {terminal} on net '{device_i['terminals'][terminal]}'"
                for terminal in ("G", "D")
                if device_i["terminals"].get(terminal) is not None
                and device_i["terminals"].get(terminal)
                == device_j["terminals"].get(terminal)
            ]
            if shared:
                return (
                    f"device_groups (id '{group['id']}'): declared diff_pair "
                    f"devices '{name_i}' and '{name_j}' are not electrically "
                    f"distinct legs -- they share {' and '.join(shared)}. Two "
                    "devices that agree on their gate or drain net are parallel "
                    "fingers of one schematic instance (a folded nf>1 MOS "
                    "digests as per-finger devices '<instance>_F0', "
                    "'<instance>_F1', ...), not two legs of a differential "
                    "pair, and binding both shorts the pair. Declare one "
                    "digest device per leg -- write the reference netlist's "
                    "matched pair with no nf (or nf=1) and let params.splits "
                    "do the folding -- or drive klt gen-compose directly with "
                    "explicit connectivity[] (see "
                    "docs/cli/layout-plan-execute.md)."
                )
    return None


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


#: Generators whose declared devices carry a MOS body (``B``) terminal *and*
#: whose generated ``ports[]`` can expose a group-level tie to it -- the
#: contacted ``TAP_PAD<k>`` pads a ring-less array draws (#4143), or, when the
#: group draws its own guard ring instead, that ring's ``TAP_<side>`` ports.
#: Exactly the generators ``layout_plan_execute._TERMINAL_SUFFIXES`` gives an
#: ``S``/``D``/``G`` map (#5546).
BODY_TIE_GENERATORS = frozenset({"mos_array", "diff_pair", "esd_device"})

#: The digest terminal name a MOS body sits on (``device_class.
#: terminal_definitions()``; see ``netlist_digest``).
BODY_TERMINAL = "B"

#: A ring-less array's contacted tie pad (#4143). Checked before
#: :data:`_RING_TAP_PREFIX`, of which it is a prefix-extension.
_TAP_PAD_PREFIX = "TAP_PAD"

#: A ``mos_array``/``diff_pair``/``esd_device`` guard ring's own tap port
#: (``gen_describe._ring_ports`` is called with ``tap_prefix="TAP_"`` by all
#: three). ``GAP_<side>`` -- a marker, never a conductor -- cannot collide.
_RING_TAP_PREFIX = "TAP_"


def body_tie_port(ports: list[dict[str, Any]]) -> str | None:
    """The single generated port a group's body net should be bound to, or
    ``None`` when the generator drew no tie (#5546).

    A ring-less array's ``TAP_PAD<k>`` pads win over a drawn ring's
    ``TAP_<side>`` ports purely so the choice is stable if a generator ever
    reports both -- today they are mutually exclusive
    (``gen._optional_tap_pads`` returns ``None`` whenever a ring is drawn).
    **One** port per group is enough either way, exactly as for
    :func:`bind_ring_nets`: a group's pads all contact the one well (pfet) or
    the one substrate (nfet), and a ring is a single conductor, so binding
    more would only wire the tie to itself.
    """
    names = [
        port["name"]
        for port in ports
        if isinstance(port, dict) and isinstance(port.get("name"), str)
    ]
    for prefix in (_TAP_PAD_PREFIX, _RING_TAP_PREFIX):
        for name in names:
            if name.startswith(prefix):
                return name
    return None


def group_body_nets(
    group: dict[str, Any],
    digest_devices_by_key: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """The distinct, non-null ``B`` nets of every device ``group`` declares,
    in first-declared order (#5546). Empty for a group whose generator has no
    body terminal, whose devices are 3-terminal, or which declares none."""
    if group["generator"] not in BODY_TIE_GENERATORS:
        return []
    nets: list[str] = []
    for device_ref in group["devices"]:
        digest_device = digest_devices_by_key.get(
            (device_ref["name"], device_ref["device_class"])
        )
        if digest_device is None:
            continue
        net = digest_device["terminals"].get(BODY_TERMINAL)
        if isinstance(net, str) and net not in nets:
            nets.append(net)
    return nets


def body_net_conflict_message(group_id: str, nets: list[str]) -> str:
    return (
        f"device_groups (id '{group_id}'): the declared devices' body "
        f"('{BODY_TERMINAL}') terminals sit on more than one net "
        f"({', '.join(repr(net) for net in nets)}), but the group has a "
        "single body tie -- binding it would short them together; split the "
        "group so each body net gets its own"
    )


def unbound_body_net_warning(group_id: str, generator: str, net: str) -> str:
    return (
        f"device_groups (id '{group_id}'): generator '{generator}' reported "
        f"no tap/ring port, so the declared devices' body net '{net}' is "
        "left untied -- the layout can route and pass DRC while every body "
        "extracts onto an anonymous well/substrate net and fails LVS"
    )


def bind_body_nets(
    device_groups: list[dict[str, Any]],
    digest_devices_by_key: dict[tuple[str, str], dict[str, Any]],
    generated: dict[str, dict[str, Any]],
    net_pins: dict[str, list[dict[str, str]]],
    warnings: list[str],
) -> None:
    """Bind each device group's own body tie to the net the netlist already
    puts that group's devices' ``B`` terminal on (#5546).

    The MOS body is deliberately absent from the per-device terminal map
    (``layout_plan_execute``'s scope decision 5) because a tie is a property
    of the *group*, not of a device: it is one well or substrate contact
    shared by every device in the array, with no ``_B``-suffixed per-device
    port to resolve. So it is bound here, group-level, beside
    :func:`bind_ring_nets` -- one :func:`body_tie_port` pin appended to the
    body net's pin set, after which the existing ``connectivity[]`` /
    ``pins[]`` / ``unmapped_netlist_nets[]`` logic carries it unchanged.

    Two cases do not bind:

    * **More than one body net in one group** -- an application error
      (:class:`~klayout_tools.layout_plan_execute.LayoutPlanExecuteError`,
      exit 1) naming the group and the nets, rather than the silent short
      one shared tie would draw between them.
    * **No tie port** (a family with no tap-distance rule and no requested
      ring, e.g. a ring-less sky130 array) -- one ``warnings[]`` entry per
      untied body net, so the caller learns in this run that the bodies are
      untied rather than from a later LVS compare. Suppressed for a net a
      ``guard_ring`` group's own ``net`` (#5095) already names: the plan
      ties it.
    """
    from .layout_plan_execute import LayoutPlanExecuteError

    ring_nets = {
        group["net"] for group in device_groups if isinstance(group.get("net"), str)
    }
    for group in device_groups:
        nets = group_body_nets(group, digest_devices_by_key)
        if not nets:
            continue
        tie_port = body_tie_port(generated[group["id"]].get("ports") or [])
        if tie_port is None:
            for net in nets:
                if net not in ring_nets:
                    warnings.append(
                        unbound_body_net_warning(group["id"], group["generator"], net)
                    )
            continue
        if len(nets) > 1:
            raise LayoutPlanExecuteError(body_net_conflict_message(group["id"], nets))
        pin = {"block": group["id"], "port": tie_port}
        if pin not in net_pins.setdefault(nets[0], []):
            net_pins[nets[0]].append(pin)
