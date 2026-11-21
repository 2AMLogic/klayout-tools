"""Device-class census and coverage block for the ``klayout-runset`` LVS
engine (split out of :mod:`klayout_tools.lvs_runset` to keep it under its
file-size ratchet ceiling, issue #5407)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .coverage import build_check_coverage

if TYPE_CHECKING:
    import klayout.db as kdb


def _class_census(
    netlist: kdb.Netlist | None,
) -> tuple[list[str], dict[str, int], int]:
    """``(declared_classes, instances_per_class, unnamed_instances)`` over
    every circuit. Empty-named device classes are kept out of the first two
    (an empty id is not a valid coverage identity) and counted in the third
    so :func:`_build_coverage` can report them instead of losing them."""
    declared = (
        sorted(dc.name for dc in netlist.each_device_class() if dc.name)
        if netlist
        else []
    )
    counts: dict[str, int] = {}
    unnamed = 0
    if netlist is not None:
        for circuit in netlist.each_circuit():
            for device in circuit.each_device():
                cls = device.device_class().name
                if not cls:
                    unnamed += 1
                    continue
                counts[cls] = counts.get(cls, 0) + 1
    return declared, counts, unnamed


def _build_coverage(
    layout_declared: list[str],
    ref_declared: list[str],
    layout_counts: dict[str, int],
    ref_counts: dict[str, int],
    unnamed_instances: int = 0,
) -> dict[str, Any]:
    recognized = sorted(set(layout_counts) | set(ref_counts))
    not_recognized = sorted(
        c for c in set(layout_declared) | set(ref_declared) if c not in recognized
    )
    skipped = [{"id": c, "reason": "declared_not_recognized"} for c in not_recognized]
    if unnamed_instances:
        skipped.append({"id": "<unnamed>", "reason": "unnamed_device_class"})
    coverage = build_check_coverage(checked=recognized, skipped=skipped)
    if unnamed_instances:
        coverage["unnamed_device_instances"] = unnamed_instances
    coverage["declared_device_classes"] = {
        "layout": layout_declared,
        "reference": ref_declared,
    }
    coverage["recognized_device_classes"] = {
        "layout": sorted(layout_counts),
        "reference": sorted(ref_counts),
    }
    coverage["declared_not_recognized"] = not_recognized
    return coverage
