"""Unmerged-finger diagnosis for ``klt lvs`` mismatch reports (issues #4729,
#4954); split out of :mod:`klayout_tools.lvs_mismatch`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from .lvs_mismatch import _mismatch, _values_differ


class _UnmergedFingers(NamedTuple):
    """Result of :func:`_group_unmerged_fingers` (issue #4729)."""

    consumed: set[int]
    entries: list[dict[str, Any]]


def _device_param(device: Any, name: str) -> float | None:
    for definition in device.device_class().parameter_definitions():
        if definition.name.upper() == name:
            return device.parameter(definition.id())
    return None


def _device_terminal_key(device: Any) -> tuple[Any, ...] | None:
    """Hashable terminal connectivity of ``device`` (net cluster ids, source
    and drain treated as interchangeable), or ``None`` if a terminal floats."""
    nets: dict[str, int | None] = {}
    for terminal in device.device_class().terminal_definitions():
        net = device.net_for_terminal(terminal.id())
        nets[terminal.name.upper()] = None if net is None else net.cluster_id
    if None in nets.values():
        return None
    if "S" in nets and "D" in nets:
        nets["S"], nets["D"] = sorted((nets["S"], nets["D"]))  # type: ignore[type-var]
    return tuple(sorted(nets.items()))


def _is_finfet(device: Any) -> bool:
    return any(
        d.name.upper() == "NFIN" for d in device.device_class().parameter_definitions()
    )


def _group_unmerged_fingers(
    unmatched: Sequence[tuple[Any, Any]],
    param_pairs: Sequence[tuple[Any, Any]],
) -> _UnmergedFingers:
    """Issue #4729: diagnose parallel fingers left unmerged (no
    ``options.combine_devices``) against a reference holding one device of the
    summed width.

    Layout candidates are the one-sided layout-unmatched devices plus the layout
    side of ``device.property`` pairs; reference candidates are the one-sided
    reference-unmatched devices plus the reference side of those pairs. Layout
    devices of the same class, length and terminal nets whose widths sum to a
    reference device's width (and number at least two) are reported as one
    ``device.unmerged_fingers`` finding naming the group, replacing the
    per-finger ``device.unmatched``/``device.property`` entries. Nothing is
    guessed: unequal summed width, differing length or differing terminal nets
    leave the group untouched. Error severity is kept -- only the wording and
    grouping change.
    """
    from .lvs import CATEGORY_DEVICE_UNMERGED_FINGERS, _name_or_none

    layout_devices = [a for a, b in unmatched if b is None]
    layout_devices += [a for a, _b in param_pairs]
    reference_devices = [b for a, b in unmatched if a is None]
    reference_devices += [b for _a, b in param_pairs]
    paired_reference = {id(a): b for a, b in param_pairs}

    groups: dict[tuple[Any, ...], list[Any]] = {}
    for device in layout_devices:
        width = _device_param(device, "W")
        length = _device_param(device, "L")
        key = _device_terminal_key(device)
        if width is None or length is None or key is None:
            continue
        circuit = device.circuit() if hasattr(device, "circuit") else None
        groups.setdefault(
            (
                device.device_class().name,
                circuit.name if circuit is not None else None,
                round(length, 9),
                key,
            ),
            [],
        ).append(device)

    consumed: set[int] = set()
    entries: list[dict[str, Any]] = []
    used_reference: set[int] = set()
    for key, fingers in groups.items():
        if len(fingers) < 2:
            continue
        total = sum(_device_param(f, "W") or 0.0 for f in fingers)
        target = next(
            (
                r
                for r in reference_devices
                if id(r) not in used_reference
                and r.device_class().name == key[0]
                and (rl := _device_param(r, "L")) is not None
                and round(rl, 9) == key[2]
                and (rw := _device_param(r, "W")) is not None
                and not _values_differ(total, rw)
            ),
            None,
        )
        if target is None:
            continue
        used_reference.add(id(target))
        consumed.update(id(f) for f in fingers)
        consumed.add(id(target))
        for finger in fingers:
            if id(finger) in paired_reference:
                consumed.add(id(paired_reference[id(finger)]))
        names = [_name_or_none(f) for f in fingers]
        if _is_finfet(fingers[0]):
            # Issue #4954: a FinFET deck refuses options.combine_devices (the
            # extractor already merges fingers), so never suggest it.
            remedy = (
                "The extractor merges only exactly-parallel fingers (same "
                "gate/bulk/source-drain nets and L); this group differs in a "
                "net or length, so check the layout connectivity"
            )
            suggested = None
        else:
            remedy = (
                "Set options.combine_devices: true to compare them as one "
                "device (see docs/cli/lvs.md, 'device.unmerged_fingers')"
            )
            suggested = "options.combine_devices"
        entries.append(
            _mismatch(
                CATEGORY_DEVICE_UNMERGED_FINGERS,
                "error",
                f"{len(fingers)} parallel layout fingers of class '{key[0]}' "
                f"(same terminals, L={key[2]:g}) sum to W={total:g}, the width "
                f"of one reference device; the fingers were not merged. {remedy}",
                "both",
                device={
                    "layout": names[0],
                    "reference": _name_or_none(target),
                    "class": key[0],
                },
                details={
                    "fingers": names,
                    "finger_count": len(fingers),
                    "summed_w_um": total,
                    "reference_w_um": _device_param(target, "W"),
                    "l_um": key[2],
                    "suggested_option": suggested,
                },
            )
        )
    return _UnmergedFingers(consumed, entries)
