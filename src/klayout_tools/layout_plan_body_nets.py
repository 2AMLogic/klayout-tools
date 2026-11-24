"""Bind generated MOS body-tie ports to the bulk nets in a layout plan."""

from __future__ import annotations

from typing import Any

#: Generators whose declared devices carry a MOS body (``B``) terminal *and*
#: whose generated ``ports[]`` can expose a group-level tie to it -- the
#: contacted ``TAP_PAD<k>`` pads a ring-less array draws (#4143), the one
#: shared ``WELL_TAP`` port a ring-less pfet array draws on a family whose
#: curated deck derives the tie from an in-well implant tap (#1473,
#: ``gen_layer_params._MOS_ARRAY_WELL_TAP_FAMILIES``), or, when the group
#: draws its own guard ring instead, that ring's ``TAP_<side>`` ports.
#: Exactly the generators ``layout_plan_execute._TERMINAL_SUFFIXES`` gives an
#: ``S``/``D``/``G`` map (#5546).
BODY_TIE_GENERATORS = frozenset({"mos_array", "diff_pair", "esd_device"})

#: The digest terminal name a MOS body sits on (``device_class.
#: terminal_definitions()``; see ``netlist_digest``).
BODY_TERMINAL = "B"

#: A ring-less array's contacted tie pad (#4143). Checked before
#: :data:`_RING_TAP_PREFIX`, of which it is a prefix-extension.
_TAP_PAD_PREFIX = "TAP_PAD"

#: The one shared well-tie tap port a ring-less ``mos_array`` draws on a
#: family whose curated deck derives the tie from an implant-covered active
#: shape inside the well (#1473 -- sg13cmos5l today; see
#: ``gen_layer_params._MOS_ARRAY_WELL_TAP_FAMILIES``). ``gen_describe``'s own
#: comment states why it is group-level: one pad ties every unit device's
#: PMOS body in the array, so there is no ``U<i>_B``-per-unit port to
#: resolve. Matched exactly, not by prefix -- unlike the two shapes around it
#: this is a single fixed name, reported beside the port itself as
#: ``gen_layer_params._MOS_ARRAY_WELL_TAP_NET_LABEL``.
_WELL_TAP_PORT_NAME = "WELL_TAP"

#: A ``mos_array``/``diff_pair``/``esd_device`` guard ring's own tap port
#: (``gen_describe._ring_ports`` is called with ``tap_prefix="TAP_"`` by all
#: three). ``GAP_<side>`` -- a marker, never a conductor -- cannot collide.
_RING_TAP_PREFIX = "TAP_"


def body_tie_port(ports: list[dict[str, Any]]) -> str | None:
    """The single generated port a group's body net should be bound to, or
    ``None`` when the generator drew no tie (#5546).

    A ring-less array's ``TAP_PAD<k>`` pads (#4143) or shared ``WELL_TAP``
    port (#1473, #5948) win over a drawn ring's ``TAP_<side>`` ports purely
    so the choice is stable if a generator ever reports both -- today the
    three are mutually exclusive: pads and the well tap are family-scoped
    (``gen_layer_params._TAP_DISTANCE_FAMILIES`` /
    ``_MOS_ARRAY_WELL_TAP_FAMILIES``), and ``gen._optional_tap_pads`` returns
    ``None`` whenever a ring is drawn. **One** port per group is enough
    either way, exactly as for
    :func:`~klayout_tools.layout_plan_ports.bind_ring_nets`: a group's ties all
    contact the one well (pfet) or the one substrate (nfet), and a ring is a
    single conductor, so binding more would only wire the tie to itself.
    """
    names = [
        port["name"]
        for port in ports
        if isinstance(port, dict) and isinstance(port.get("name"), str)
    ]
    for name in names:
        if name == _WELL_TAP_PORT_NAME or name.startswith(_TAP_PAD_PREFIX):
            return name
    for name in names:
        if name.startswith(_RING_TAP_PREFIX):
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
    :func:`~klayout_tools.layout_plan_ports.bind_ring_nets` -- one
    :func:`body_tie_port` pin appended to the
    body net's pin set, after which the existing ``connectivity[]`` /
    ``pins[]`` / ``unmapped_netlist_nets[]`` logic carries it unchanged.

    Two cases do not bind:

    * **More than one body net in one group** -- an application error
      (:class:`~klayout_tools.layout_plan_execute.LayoutPlanExecuteError`,
      exit 1) naming the group and the nets, rather than the silent short
      one shared tie would draw between them.
    * **No tie port** (a family with no tap-distance rule and no requested
      ring, e.g. a ring-less sky130 array -- or either flavor on a family
      whose only drawn tie is the pfet well tap, like sg13cmos5l's nfet) --
      one ``warnings[]`` entry per untied body net, so the caller learns in
      this run that the bodies are untied rather than from a later LVS
      compare. Suppressed for a net a ``guard_ring`` group's own ``net``
      (#5095) already names: the plan ties it.
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
