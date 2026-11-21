"""Top-level pin anchoring and the repeated-label retry for the ``klayout``
engine's netlist compare (issues #2692, #4798).

Split out of ``lvs.py`` to keep that file under its size-ratchet ceiling.
Helpers that live in ``lvs.py`` (``_apply_hints`` & co.) are imported lazily
inside the functions that use them, to avoid a circular import.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from .lvs_mismatch import _make_compare_logger, _mismatch
from .lvs_timeout import CompareTimeout

#: Issue #2692: how many anchored pin names the
#: :data:`CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED` disclosure lists before
#: truncating (``pins_truncated: true``). A routed block anchors every bit of
#: every boundary bus, which on a bus-heavy design is hundreds of names --
#: enough to bury the rest of ``mismatches[]`` if the entry dumped all of
#: them. The entry's own ``pin_count`` is always the exact, untruncated total
#: (same discipline as :data:`_POWER_INSTANCE_SAMPLE_LIMIT`).
_ANCHORED_PIN_SAMPLE_LIMIT = 20


class _TopLevelPinAnchor(NamedTuple):
    """One top-level pin name whose net was anchored across the two sides
    (issue #2692) -- the automatic counterpart of a hand-written
    ``hints.same_nets`` pair, carrying the pin name that licensed it.

    ``layout_net``/``reference_net`` are ``Net.expanded_name()`` values, the
    same spelling :func:`_apply_hints` records its declared pairs under (and
    the spelling ``_build_mismatches`` reconciles against the comparer's own
    pairing events).
    """

    pin: str
    layout_net: str
    reference_net: str


_REPEATED_LABEL_SUFFIX = re.compile(r"^(.+)\$(\d+)$")


def _pin_nets_by_name(circuit: Any) -> tuple[dict[str, Any], set[str]]:
    """``({<upper-cased pin name>: <net>}, <repeated-label families>)`` for the
    named top-level pins of ``circuit`` that resolve to a net (issue #2692).

    Upper-cased for the same reason :func:`_circuit_pin_names` upper-cases:
    the two sides reach this module through readers that disagree about case
    (``NetlistSpiceReader`` normalises to upper case; a netlist handed over
    from ``klt extract``'s in-process extraction does not), so folding both
    to one case is what lets a layout pin line up with its reference
    counterpart at all.

    A name declared by **more than one** pin on the same circuit is dropped
    rather than anchored: with two candidate nets there is no single pairing
    the name licenses, and guessing one would be exactly the unsound
    name-matching this anchoring exists to avoid. An unnamed pin, and a pin
    that resolves to no net at all, are skipped for the same reason.

    The same rule extends to **repeated-label families** (issue #4798): the
    extractor writes several pins labelled ``PAD`` as ``PAD``, ``PAD$1``, ...
    and which ``$k`` lands on which unit follows extractor order, not unit
    identity. When two or more pins share a base name this way, every member
    is dropped from the returned mapping and the base name is reported in the
    second element so the caller can disclose the skip. A lone ``X$1`` with no
    sibling is not a family and stays anchored.
    """
    nets: dict[str, Any] = {}
    ambiguous: set[str] = set()
    by_base: dict[str, set[str]] = {}
    for pin in circuit.each_pin():
        name = (pin.name() or "").upper()
        net = circuit.net_for_pin(pin.id())
        if not name or net is None:
            continue
        suffixed = _REPEATED_LABEL_SUFFIX.match(name)
        by_base.setdefault(suffixed.group(1) if suffixed else name, set()).add(name)
        if name in nets:
            ambiguous.add(name)
        nets[name] = net
    for name in ambiguous:
        del nets[name]
    families = {base for base, members in by_base.items() if len(members) > 1}
    for base in families:
        for member in by_base[base]:
            nets.pop(member, None)
    return nets, families


def _repeated_label_pin_nets(circuit: Any) -> dict[str, list[Any]]:
    """``{<BASE>: [<net>, ...]}`` for each repeated-label pin family
    (``BASE``, ``BASE$1``, ... -- issue #4798) of ``circuit``'s pins, the
    nets deduplicated. Single pins are not families."""
    members: dict[str, dict[str, Any]] = {}
    for pin in circuit.each_pin():
        name = (pin.name() or "").upper()
        net = circuit.net_for_pin(pin.id())
        if not name or net is None:
            continue
        suffixed = _REPEATED_LABEL_SUFFIX.match(name)
        members.setdefault(suffixed.group(1) if suffixed else name, {})[name] = net
    return {
        base: list({id(net): net for net in named.values()}.values())
        for base, named in members.items()
        if len(named) > 1
    }


def _rename_nets_neutral(
    families: Mapping[str, Sequence[Any]], side: str
) -> list[tuple[Any, str]]:
    """Give every family net a side-specific unique name; return
    ``[(net, original_name)]`` so the caller can restore them."""
    restore: list[tuple[Any, str]] = []
    counter = 0
    for base in sorted(families):
        for net in families[base]:
            restore.append((net, net.name))
            net.name = f"__RLF_{side}{counter}"
            counter += 1
    return restore


def _retry_with_neutral_family_names(
    request: Mapping[str, Any],
    layout_circuit: Any,
    reference_circuit: Any,
    layout_netlist: Any,
    reference_netlist: Any,
    *,
    first_pass: tuple[Any, ...],
    first_pass_timeout: CompareTimeout | None,
    anchor_top_level_pins: bool,
    compare_timeout_s: float | None,
    compare_context: Mapping[str, Any],
) -> tuple[Any, ...]:
    """Issue #4798: retry a mismatched compare ONCE with repeated-label pin
    nets renamed to unrelated unique names (topology decides).

    ``PAD``/``PAD$1``/... share one label, so which ``$k`` sits on which unit
    is extractor order, not design intent. On a symmetric design
    ``NetlistComparer`` takes same-named nets as tie-break hints and does not
    backtrack, so a swapped ``$k`` pairing reports false ``device.unmatched``.

    ``first_pass`` is ``(compare_result, logger, same_nets_hints,
    equivalent_pins_applied, constant_bindings, constant_warnings,
    anchored_pins, anchor_warnings)`` of the first compare and is returned
    unchanged when the first pass already matched or timed out, when neither
    side has such a family, or when the retry does not produce a real
    ``match``. Otherwise the same-shaped tuple of the matching retry (with
    the disclosure added) is returned.
    """
    if first_pass[0] or first_pass_timeout:
        return first_pass
    import klayout.db as kdb

    from .lvs import (
        _apply_constant_nets,
        _apply_hints,
        _constant_binding_pairs,
        _guarded_compare,
    )

    layout_family = _repeated_label_pin_nets(layout_circuit)
    reference_family = _repeated_label_pin_nets(reference_circuit)
    if not (layout_family or reference_family):
        return first_pass
    hints = request.get("hints") or {}
    retry_logger = _make_compare_logger(layout_circuit, reference_circuit)
    retry_comparer = kdb.NetlistComparer(retry_logger)
    retry_comparer.same_circuits(layout_circuit, reference_circuit)
    retry_hints, retry_equivalent = _apply_hints(
        retry_comparer, hints, layout_circuit, reference_circuit
    )
    retry_constants, retry_constant_warnings = _apply_constant_nets(
        retry_comparer,
        hints,
        layout_circuit,
        reference_circuit,
        declared=retry_hints,
    )
    retry_anchored, retry_anchor_warnings = _apply_top_level_pin_anchors(
        retry_comparer,
        layout_circuit,
        reference_circuit,
        enabled=anchor_top_level_pins,
        declared=[*retry_hints, *_constant_binding_pairs(retry_constants)],
    )
    # Hints/anchors above resolved names to net objects already, so renaming
    # now cannot disturb them.
    restore = _rename_nets_neutral(layout_family, "L") + (
        _rename_nets_neutral(reference_family, "R")
    )
    try:
        retry_result, retry_timeout = _guarded_compare(
            retry_comparer,
            layout_netlist,
            reference_netlist,
            compare_timeout_s,
            compare_context,
        )
    finally:
        for net, name in restore:
            net.name = name
    if not retry_result or retry_timeout:
        return first_pass
    return (
        True,
        retry_logger,
        retry_hints,
        retry_equivalent,
        retry_constants,
        retry_constant_warnings,
        retry_anchored,
        _with_repeated_label_retry_disclosure(
            retry_anchor_warnings,
            sorted(set(layout_family) | set(reference_family)),
        ),
    )


def _with_repeated_label_retry_disclosure(
    anchor_warnings: list[dict[str, Any]], families: Sequence[str]
) -> list[dict[str, Any]]:
    """Add the retry disclosure (issue #4798): extend the
    ``topology.top_level_pins_anchored`` entry's ``details`` with
    ``repeated_label_pin_families``, or emit one when anchoring made none."""
    note = (
        "repeated-label pin famil(ies) "
        + ", ".join(families)
        + " (BASE, BASE$1, ...): the first compare mismatched, and a single "
        "retry with those nets renamed to neutral unique names matched, so "
        "this match is decided by topology alone -- the $k pairing is "
        "extractor-order dependent"
    )
    if anchor_warnings:
        entry = anchor_warnings[0]
        entry["details"]["repeated_label_pin_families"] = list(families)
        entry["description"] += "; " + note
        return anchor_warnings
    from .lvs import CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED

    return [
        _mismatch(
            CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED,
            "warning",
            note,
            "both",
            details={
                "pins": [],
                "pin_count": 0,
                "pins_truncated": False,
                "repeated_label_pin_families": list(families),
            },
        )
    ]


def _apply_top_level_pin_anchors(
    comparer: Any,
    layout_circuit: Any,
    reference_circuit: Any,
    *,
    enabled: bool,
    declared: Sequence[tuple[str, str]] | None = None,
) -> tuple[list[_TopLevelPinAnchor], list[dict[str, Any]]]:
    """Anchor every top-level pin name declared on both sides by name
    (issue #2692), returning ``(anchors, disclosure_warnings)``.

    **The gap this closes.** ``NetlistComparer`` pairs nets by *topology*;
    top-level pin names are never used as anchors. So two nets with identical
    topology are interchangeable, and swapping them compares clean. On a
    black-box (pin-only) macro that covers a whole bus: every bit wired
    straight from a top-level pin to a macro pin -- the ordinary shape of an
    SRAM's data/address bus, or any IP bus taken to the chip boundary -- is
    topologically indistinguishable from its neighbours, so a bit-reversed
    (or otherwise permuted) layout-side bus reported ``status: "match"`` with
    ``mismatch_count: 0``. The same holds for two bits tied to constant nets
    (``__CONST0__``/``__CONST1__``).

    The fix uses the mechanism ``hints.same_nets`` already exposes by hand,
    applied automatically: for each pin name present on both sides,
    ``NetlistComparer.same_nets(..., must_match=True)`` ties the layout net
    that pin lands on to the reference net of the same-named pin. A swap then
    contradicts an assertion the comparer must honor, and the compare reports
    ``mismatch`` -- with the refused pairing surfaced as a ``hints.rejected``
    entry by ``_build_mismatches``, exactly as a hand-written hint's refusal
    is (``details.anchored_top_level_pin`` tells the two apart).

    A pin name present on only **one** side is not an error, matching the
    convention ``options.combine_devices``/``reference.device_bulk`` already
    use -- only names resolvable on both sides are anchored (a
    ``gate-level-verilog`` reference carries no power/ground pins at all, so
    the layout's rails simply fall out of scope here).

    ``declared`` is the ``hints.same_nets`` pairs :func:`_apply_hints`
    already applied. A net named by one of them is left alone: the caller's
    own explicit declaration is the authority on that net, and anchoring it
    a second time would either duplicate the pairing or, where the two
    disagree, report one refusal twice.

    Returns ``([], [])`` untouched when ``enabled`` is false, so a request
    that opts out (or any form this does not default on for) is
    byte-identical to before this hook existed.
    """
    if not enabled:
        return [], []
    layout_nets, layout_families = _pin_nets_by_name(layout_circuit)
    reference_nets, reference_families = _pin_nets_by_name(reference_circuit)
    families = layout_families | reference_families
    # A family seen on only one side must not anchor against the other side's
    # same-named single pin either (issue #4798).
    for family in families:
        layout_nets.pop(family, None)
        reference_nets.pop(family, None)
    spoken_for = {name for pair in declared or () for name in pair}
    anchors: list[_TopLevelPinAnchor] = []
    for pin_name in sorted(set(layout_nets) & set(reference_nets)):
        net_a = layout_nets[pin_name]
        net_b = reference_nets[pin_name]
        layout_name = net_a.expanded_name()
        reference_name = net_b.expanded_name()
        if layout_name in spoken_for or reference_name in spoken_for:
            continue
        comparer.same_nets(layout_circuit, reference_circuit, net_a, net_b, True)
        anchors.append(_TopLevelPinAnchor(pin_name, layout_name, reference_name))
    return anchors, _top_level_pin_anchor_warnings(
        anchors,
        sorted(families),
        _layout_only_pins(layout_circuit, reference_circuit, families),
    )


def _layout_only_pins(
    layout_circuit: Any, reference_circuit: Any, families: set[str]
) -> list[str]:
    """Sorted, upper-cased layout top-level pin names with no same-named
    reference pin (issue #5396) -- e.g. the DEF-named internal nets
    ``klt extract --def-net-names`` lists as pins, which a gate-level Verilog
    reference (ports only) never declares. Repeated-label family members are
    left out: they are disclosed separately as skipped families."""
    reference_names = {
        (pin.name() or "").upper() for pin in reference_circuit.each_pin()
    }
    names: set[str] = set()
    for pin in layout_circuit.each_pin():
        name = (pin.name() or "").upper()
        if not name or name in reference_names:
            continue
        suffixed = _REPEATED_LABEL_SUFFIX.match(name)
        if (suffixed.group(1) if suffixed else name) in families:
            continue
        names.add(name)
    return sorted(names)


def _top_level_pin_anchor_warnings(
    anchors: Sequence[_TopLevelPinAnchor],
    skipped_families: Sequence[str] = (),
    layout_only_pins: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The single ``severity: "warning"``
    :data:`CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED` disclosure for a run
    that anchored at least one top-level pin (issue #2692), or ``[]``.

    Always a warning: anchoring never *creates* a mismatch on its own -- it
    removes a degree of freedom the comparer would otherwise have used, so a
    verdict reached with it is strictly stronger than one without. The entry
    exists so a ``"match"`` that rests on name-anchored boundary pins is
    distinguishable from one reached by topology alone (and so a reader of a
    committed report can tell the hook ran at all).
    """
    if not anchors and not skipped_families:
        return []
    pins = [anchor.pin for anchor in anchors]
    sample = pins[:_ANCHORED_PIN_SAMPLE_LIMIT]
    details: dict[str, Any] = {
        "pins": sample,
        "pin_count": len(pins),
        "pins_truncated": len(sample) < len(pins),
    }
    if layout_only_pins:
        # Issue #5396: additive, present only when non-empty so a report
        # without layout-only pins stays byte-identical.
        only_sample = list(layout_only_pins[:_ANCHORED_PIN_SAMPLE_LIMIT])
        details["layout_only_pins"] = only_sample
        details["layout_only_pin_count"] = len(layout_only_pins)
        details["layout_only_pins_truncated"] = len(only_sample) < len(layout_only_pins)
    family_note = ""
    if skipped_families:
        # Issue #4798: `PAD`/`PAD$1`/... are one repeated label whose `$k`
        # numbering is extractor order, so name-anchoring them would turn an
        # ordering artefact into a hard assertion (false device.unmatched).
        details["skipped_repeated_label_pins"] = list(skipped_families)
        family_note = (
            "; repeated-label pin famil(ies) "
            + ", ".join(skipped_families)
            + " (BASE, BASE$1, ...) were NOT anchored because the $k pairing "
            "is extractor-order dependent and topology decides those nets"
        )
    from .lvs import CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED

    return [
        _mismatch(
            CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED,
            "warning",
            f"{len(pins)} top-level pin name(s) declared on both sides were "
            "anchored by name before comparing (options."
            "anchor_top_level_pins): each one's layout net is asserted to "
            "match the reference net of the same-named pin, which the "
            "comparer's own topology-only net pairing does not otherwise "
            "require. Without it, two boundary nets with identical topology "
            "(e.g. two bits of a black-box macro's bus) are interchangeable "
            "and a swap between them compares clean -- see docs/cli/lvs.md, "
            '"Top-level pin anchoring"' + family_note,
            "both",
            details=details,
        )
    ]
