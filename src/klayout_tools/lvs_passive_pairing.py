"""Summarize SG13G2 passive device signatures involved in failed LVS pairs."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import chain
from typing import Any

from .lvs_runset_findings import _paired_ok

Signature = tuple[str, tuple[str, ...]]
# The input forms diagnosed here are the SG13G2 poly resistor and MIM capacitor.
_MODELS = {"RPPD", "CAP_CMIM"}


def _model(signature: Signature) -> str:
    name = signature[0].upper()
    return name[2:-1] if name.startswith("$[") and name.endswith("]") else name


def _signature(cls: Any) -> Signature:
    return cls.name, tuple(terminal.name for terminal in cls.terminal_definitions())


def _fold(signature: Signature) -> Signature:
    return signature[0].upper(), tuple(name.upper() for name in signature[1])


def _discrepancies(left: Signature, right: Signature) -> tuple[str, ...]:
    left, right = _fold(left), _fold(right)
    reasons = []
    if left[0] != right[0]:
        reasons.append("class_model")
    if left[1] != right[1]:
        reasons.append("terminal_list")
    return tuple(reasons)


def _failed_signatures(
    pairs: Iterable[tuple[Any, Any]],
) -> tuple[set[Signature], set[Signature], set[tuple[Signature, Signature]]]:
    left: set[Signature] = set()
    right: set[Signature] = set()
    direct: set[tuple[Signature, Signature]] = set()
    for layout, reference in pairs:
        a = _signature(layout.device_class()) if layout is not None else None
        b = _signature(reference.device_class()) if reference is not None else None
        if a is not None and _model(a) in _MODELS:
            left.add(a)
        if b is not None and _model(b) in _MODELS:
            right.add(b)
        if a and b and (_model(a) in _MODELS or _model(b) in _MODELS):
            direct.add((a, b))
    return left, right, direct


def _counterparts(
    left: set[Signature], right: set[Signature], netlists: tuple[Any, Any]
) -> Iterable[tuple[Signature, Signature]]:
    """A missing counterpart in one callback is insufficient. Check the class
    definitions on the opposite netlist before linking separate failures."""
    if not left or not right:
        return
    declared_left, declared_right = (
        {_fold(_signature(cls)) for cls in netlist.each_device_class()}
        for netlist in netlists
    )
    for a in left:
        for b in right:
            if _model(a) == _model(b) and (
                _fold(a) not in declared_right or _fold(b) not in declared_left
            ):
                yield a, b


def diagnose(
    pairs: Iterable[tuple[Any, Any]],
    counts: dict[str, int],
    netlists: tuple[Any, Any],
    status: str,
) -> dict[str, Any]:
    left, right, direct = _failed_signatures(pairs)
    findings = []
    for a, b in sorted(direct | set(_counterparts(left, right, netlists))):
        reasons = _discrepancies(a, b)
        if reasons:
            findings.append(
                {
                    "differences": list(reasons),
                    "evidence": "compare_event"
                    if (a, b) in direct
                    else "unmatched_classes",
                    "layout_model": a[0],
                    "reference_model": b[0],
                    "layout_terminals": list(a[1]),
                    "reference_terminals": list(b[1]),
                }
            )
    state = "insufficient_evidence"
    if findings:
        state = "discrepancy_observed"
    elif (
        status == "match"
        and counts["layout"] == counts["reference"] == counts["matched"] > 0
        and not (left or right)
    ):
        state = "all_devices_paired"
    return {"status": state, "findings": findings}


def curated_pairing(
    decks: tuple[str | None, str | None],
    logger: Any,
    counts: dict[str, int],
    status: str,
    netlists: tuple[Any, Any],
) -> dict[str, Any] | None:
    if "sg13g2" not in decks:
        return None
    if status == "inconclusive":
        return {"status": "insufficient_evidence", "findings": []}
    return diagnose(
        chain(logger.device_mismatches, logger.class_mismatches),
        counts,
        netlists,
        status,
    )


def runset_pairing(xref: Any, counts: dict[str, int], status: str) -> dict[str, Any]:
    failed = (
        (pair.first(), pair.second())
        for circuit in xref.each_circuit_pair()
        for pair in xref.each_device_pair(circuit)
        if not _paired_ok(pair)
    )
    return diagnose(failed, counts, (xref.netlist_a(), xref.netlist_b()), status)


def runset_report_field(verdict: dict[str, Any], enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {}
    diagnosis = verdict.get("sg13g2_passive_pairing")
    if diagnosis is None or (
        diagnosis["status"] == "all_devices_paired" and verdict["status"] != "match"
    ):
        # Empty extraction or unresolved reference calls can invalidate a
        # positive cross-reference result during final runset grading.
        diagnosis = {"status": "insufficient_evidence", "findings": []}
    return {"sg13g2_passive_pairing": diagnosis}
