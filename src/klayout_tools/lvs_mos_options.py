"""MOS-device options for ``klt lvs`` that rewrite a netlist before the compare.

``options.compare_mos_flavour`` (issue #3650) and ``options.ignore_mos_bulk``
(issue #5125). Kept out of :mod:`klayout_tools.lvs`, which is size-ratcheted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .lvs_combine import _net_for_terminal_or_none
from .pdk_models import MOS_FLAVOUR_PROPERTY, flavoured_mos_class_name

if TYPE_CHECKING:
    import klayout.db as kdb

#: Issue #5125: `options.ignore_mos_bulk` disconnected every MOS device's bulk
#: terminal on both netlists before comparing (always a warning; one entry per
#: device class and side).
CATEGORY_DEVICE_BULK_IGNORED = "device.bulk_ignored"


def parse_ignore_mos_bulk(options: Mapping[str, Any], engine: str) -> bool:
    """Resolve ``options.ignore_mos_bulk`` (issue #5125); default ``False``.

    ``true`` disconnects the bulk terminal of every MOS device on both
    netlists before comparing, so a tap-less planar layout (whose inline
    extraction leaves bulks on deck-synthesized or anonymous nets) can still
    have its gate/drain/source topology graded. A non-boolean, or ``true``
    with an engine other than ``klayout``, is a request error.
    """
    from .lvs import LvsError

    value = options.get("ignore_mos_bulk", False)
    if not isinstance(value, bool):
        raise LvsError(
            "options.ignore_mos_bulk must be a boolean -- true to disconnect "
            "every MOS device's bulk terminal on both sides before comparing, "
            "false (the default) to compare bulk connectivity"
        )
    if value and engine != "klayout":
        raise LvsError("options.ignore_mos_bulk is only supported for engine 'klayout'")
    return value


def apply_ignore_mos_bulk(
    enabled: bool, layout_netlist: kdb.Netlist, reference_netlist: kdb.Netlist
) -> list[dict[str, Any]]:
    """Issue #5125: when ``enabled``, disconnect the ``B`` terminal of every
    MOS device on both netlists and return one ``severity: "warning"``
    ``device.bulk_ignored`` entry per device class and side."""
    if not enabled:
        return []
    from .lvs_mismatch import _mismatch

    entries: list[dict[str, Any]] = []
    for side, netlist in (("layout", layout_netlist), ("reference", reference_netlist)):
        counts: dict[str, int] = {}
        for circuit in netlist.each_circuit():
            for device in circuit.each_device():
                device_class = device.device_class()
                names = {
                    t.name.upper(): t.name for t in device_class.terminal_definitions()
                }
                if not {"S", "G", "D", "B"} <= names.keys():
                    continue
                device.disconnect_terminal(names["B"])
                counts[device_class.name] = counts.get(device_class.name, 0) + 1
        for class_name, count in sorted(counts.items()):
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_BULK_IGNORED,
                    "warning",
                    f"options.ignore_mos_bulk disconnected the bulk terminal "
                    f"of {count} '{class_name}' device(s) on the {side} "
                    "before comparing -- bulk connectivity was not verified",
                    side,
                    details={"device_class": class_name, "device_count": count},
                )
            )
    return entries


def split_layout_mos_flavours(netlist: kdb.Netlist, enabled: bool) -> None:
    """Issue #3650: when ``enabled`` (``options.compare_mos_flavour``), move
    every layout MOS device that inline extraction tagged with
    ``pdk_models.MOS_FLAVOUR_PROPERTY`` onto a flavour-specific device
    class (``nfet`` -> ``nfet__nvt``), the same names the reference conversion
    uses, so ``NetlistComparer`` pairs flavours only with their own kind. A new
    class is built per (base class, flavour) the way
    :func:`_renew_conflicting_device_classes` builds them, and each device is
    re-created on it with the same circuit, name, parameters and nets.
    """
    if not enabled:
        return
    classes: dict[str, Any] = {}
    for circuit in netlist.each_circuit():
        for device in list(circuit.each_device()):
            flavour = device.property(MOS_FLAVOUR_PROPERTY)
            if flavour is None:
                continue
            old_class = device.device_class()
            name = flavoured_mos_class_name(old_class.name, str(flavour))
            new_class = classes.get(name)
            if new_class is None:
                new_class = type(old_class)()
                new_class.name = name
                new_class.description = old_class.description
                netlist.add(new_class)
                classes[name] = new_class
            replacement = circuit.create_device(new_class, device.name)
            for terminal in old_class.terminal_definitions():
                net = _net_for_terminal_or_none(device, terminal.name)
                if net is not None:
                    replacement.connect_terminal(terminal.name, net)
            for parameter in old_class.parameter_definitions():
                replacement.set_parameter(
                    parameter.name, device.parameter(parameter.name)
                )
            circuit.remove_device(device)
