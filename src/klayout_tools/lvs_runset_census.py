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


def _reference_source_census(
    reference_path: str | None, deck_file: str | None
) -> dict[str, int] | None:
    """Positive reference-side evidence for issue #6902: instance counts
    keyed by upper-cased name over the reference *source* netlist, re-read
    with the plain SPICE reader (hierarchy intact, no deck delegate), one
    count per card per circuit definition -- the same per-definition unit
    :func:`_class_census` counts, so the two totals are comparable.

    In scope, exactly the cards the runset's reader delegate is responsible
    for keeping:

    - a subcircuit call whose callee (upper-cased) is in the deck's
      ``DEVICE_LIST`` -- the subcircuits the delegate converts to devices
      (including ones it renames, so per-name equality with the converted
      census does not hold; ``klt signoff`` compares totals instead);
    - every device card the plain reader keeps (``M``/``Q``/``D``/``C``/
      ``R``/``L``...), keyed by its device-class name -- for modelled cards
      that is the model string, as written.

    ``None`` when the reference cannot be re-read: a report without this
    evidence cannot prove the delegate kept every reference device, and the
    grader fails closed on it."""
    if not reference_path or not deck_file:
        return None
    import klayout.db as kdb

    from .lvs_runset import _deck_device_list

    netlist = kdb.Netlist()
    try:
        netlist.read(reference_path, kdb.NetlistSpiceReader())
    except Exception:
        return None
    device_list = _deck_device_list(deck_file)
    counts: dict[str, int] = {}
    for circuit in netlist.each_circuit():
        for subcircuit in circuit.each_subcircuit():
            # ``circuit_ref`` is a method in some klayout bindings and a
            # property in others; accept either. A callee the plain reader
            # could not resolve becomes a placeholder circuit whose name
            # carries the call's parameters (``NAME(PARAM=..,..)``) -- the
            # callee is the part before the first ``(``.
            circuit_ref = subcircuit.circuit_ref
            callee = circuit_ref() if callable(circuit_ref) else circuit_ref
            name = str(getattr(callee, "name", "") or "")
            name = name.split("(", 1)[0].strip().upper()
            if name in device_list:
                counts[name] = counts.get(name, 0) + 1
        for device in circuit.each_device():
            name = str(device.device_class().name or "").upper()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _attach_reference_source(
    verdict: dict[str, Any], reference_path: str | None, deck_file: str | None
) -> None:
    """Attach :func:`_reference_source_census` to a verdict's
    ``device_census`` as ``reference_source`` (issue #6902). A one-call
    helper so ``lvs_runset`` stays at its file-size ceiling."""
    verdict["device_census"]["reference_source"] = _reference_source_census(
        reference_path, deck_file
    )


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
