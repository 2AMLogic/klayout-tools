"""Power/ground connectivity check (issue #1952)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .lvs_mismatch import _body_unverified_counts
from .socket_check import classify_pin_by_name


def _parse_power_connectivity(
    options: Mapping[str, Any],
) -> tuple[bool, dict[str, str] | None, Any]:
    """Resolve ``options.power_connectivity`` into
    ``(enabled, expected_nets, echo)`` (issue #1952).

    Accepted shapes:

    * **absent** (the default) -- ``(True, None, None)``. The
      cross-instance consistency check runs; nothing is echoed back as an
      explicit request value, matching ``netgen_setup``/
      ``combine_devices_per_circuit``'s "``null`` when the option was
      omitted" convention.
    * ``false`` -- ``(False, None, False)``. The opt-out for a genuinely
      multi-domain design, where one pin name legitimately reaches more
      than one net and the consistency invariant does not hold.
    * ``true`` -- ``(True, None, True)``. Identical behaviour to omitting
      the key, stated explicitly so a committed request document records
      that the check was wanted.
    * ``{"expected_nets": {"<PIN>": "<NET>", ...}}`` -- ``(True, {...},
      {...})``. Names the net each power/ground pin must reach, which turns
      the *relative* consistency check into an *absolute* one (see
      :data:`RULE_POWER_UNEXPECTED_PIN_NET`). Declaring a subset is fine:
      every pin the mapping does not name still gets the consistency check.

    Pin and net names are upper-cased here, once, for the same reason
    :func:`_circuit_pin_names` upper-cases: SPICE is case-insensitive and
    the layout netlist reaches this module through readers that disagree
    about case (``NetlistSpiceReader`` normalises to upper case; a netlist
    handed over from ``klt extract``'s own in-process extraction does not).
    The *echo* keeps the caller's own spelling, so a committed report still
    round-trips back into the request document it came from.

    A wrong-shaped value is a clean request error, matching every other
    option parser in this module (see :func:`_parse_compare_parameters`).
    """

    from .lvs import LvsError

    if "power_connectivity" not in options:
        return True, None, None
    value = options["power_connectivity"]
    if isinstance(value, bool):
        return value, None, value
    if not isinstance(value, dict):
        raise LvsError(
            "options.power_connectivity must be a boolean or a JSON object, "
            'e.g. {"expected_nets": {"VPWR": "VPWR", "VGND": "VGND"}}'
        )
    unknown = sorted(set(value) - {"expected_nets"})
    if unknown:
        raise LvsError(
            "options.power_connectivity accepts only 'expected_nets' "
            f"(got: {', '.join(unknown)})"
        )
    raw = value.get("expected_nets")
    if raw is None:
        return True, None, dict(value)
    if not isinstance(raw, dict) or not raw:
        raise LvsError(
            "options.power_connectivity.expected_nets must be a non-empty "
            "JSON object mapping a power/ground pin name to the net name it "
            'must reach, e.g. {"VPWR": "VPWR", "VGND": "VGND"}'
        )
    expected: dict[str, str] = {}
    for pin, net in raw.items():
        if not isinstance(pin, str) or not pin.strip():
            raise LvsError(
                "options.power_connectivity.expected_nets keys must be "
                "non-empty power/ground pin-name strings"
            )
        if not isinstance(net, str) or not net.strip():
            raise LvsError(
                "options.power_connectivity.expected_nets values must be "
                f"non-empty net-name strings (pin {pin!r})"
            )
        expected[pin.strip().upper()] = net.strip().upper()
    return True, expected, dict(value)


def _power_pin_connections(
    layout_netlist: Any, power_pin_names: frozenset[str]
) -> list[dict[str, Any]]:
    """Every ``(instance, power/ground pin) -> net`` edge in
    ``layout_netlist`` (issue #1952).

    Walks each circuit's subcircuit instances and, for every pin its master
    declares whose (upper-cased) name is in ``power_pin_names`` -- the
    library-derived power-pin universe :func:`_gate_level_power_pin_names`
    computes, never a hardcoded per-PDK name table -- records which net that
    instance actually connects it to. ``net`` is ``None`` when the pin
    resolved to no net at all.

    **Must run before :func:`_prune_power_only_circuits`**, not
    after. A filler/tap cell is *only* power pins, so pruning removes
    exactly the instances whose power connectivity is the only thing about
    them a compare could ever check -- and an unconditionally-inserted
    filler shorting ``VPWR``/``VGND`` onto a signal net is the concrete
    defect class issue #1442 fixed at the source. Checking after the prune
    would silently exempt them.
    """
    rows: list[dict[str, Any]] = []
    for circuit in layout_netlist.each_circuit():
        for sub in circuit.each_subcircuit():
            ref = sub.circuit_ref()
            if ref is None:
                continue
            for pin in ref.each_pin():
                name = pin.name()
                if not name or name.upper() not in power_pin_names:
                    continue
                net = sub.net_for_pin(pin.id())
                rows.append(
                    {
                        "circuit": circuit.name,
                        "instance": sub.expanded_name(),
                        "cell": ref.name,
                        "pin": name.upper(),
                        "net": net.expanded_name() if net is not None else None,
                    }
                )
    return rows


def _power_net_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group one pin name's :func:`_power_pin_connections` rows by the net
    each instance reaches, as ``power_connectivity.findings[].nets[]``
    entries (issue #1952).

    ``instance_count`` is always the exact, untruncated number of instances
    in the group; ``instances`` lists at most
    :data:`_POWER_INSTANCE_SAMPLE_LIMIT` of them, with
    ``instances_truncated`` saying whether anything was left out -- a real
    routed block puts hundreds of instances on one rail, and the few that
    differ are the whole point of the finding.
    """

    from .lvs import _POWER_INSTANCE_SAMPLE_LIMIT

    by_net: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        by_net.setdefault(row["net"], []).append(row)
    groups: list[dict[str, Any]] = []
    # `None` (an unconnected pin) sorts first, then net names alphabetically.
    for net in sorted(by_net, key=lambda name: (name is not None, name or "")):
        members = sorted(by_net[net], key=lambda row: (row["circuit"], row["instance"]))
        sample = members[:_POWER_INSTANCE_SAMPLE_LIMIT]
        groups.append(
            {
                "net": net,
                "instance_count": len(members),
                "instances": [
                    {
                        "circuit": row["circuit"],
                        "instance": row["instance"],
                        "cell": row["cell"],
                    }
                    for row in sample
                ],
                "instances_truncated": len(sample) < len(members),
            }
        )
    return groups


def _describe_power_net_groups(groups: list[dict[str, Any]]) -> str:
    """A compact ``'<net>' (N instance(s), e.g. X1)`` rendering of
    :func:`_power_net_groups`'s output for a finding's ``description``."""
    parts = []
    for group in groups:
        net = "no net" if group["net"] is None else repr(group["net"])
        example = group["instances"][0]["instance"] if group["instances"] else "?"
        parts.append(f"{net} ({group['instance_count']} instance(s), e.g. {example})")
    return "; ".join(parts)


#: Appended to a ground-class pin's ``power.inconsistent_pin_net`` /
#: ``power.unexpected_pin_net`` ``description`` (issue #2971). Abstract-cell
#: extraction black-boxes each cell, erasing the substrate taps that join
#: separate ground rails in a transistor-level extraction of the same GDS, so
#: a ground finding here can be a modelling artifact rather than a defect.
_GROUND_SUBSTRATE_CAVEAT = (
    ". Caveat for this ground pin: substrate continuity through the cells' "
    "own taps is not modelled in abstract-cell extraction, so separate "
    "Metal1 ground rails that a transistor-level extraction joins through "
    "the substrate are reported here as distinct nets -- this finding may "
    "be an artifact. To classify it, re-run klt extract without "
    "--abstract-cells and compare the pin's net count"
)


def _ground_pin_caveat(pin: str) -> str:
    """:data:`_GROUND_SUBSTRATE_CAVEAT` for a ground-class ``pin`` (by the
    repo's shared name heuristic), ``""`` for a power or signal pin."""
    return _GROUND_SUBSTRATE_CAVEAT if classify_pin_by_name(pin)[1] == "GROUND" else ""


def _power_connectivity_findings(
    rows: list[dict[str, Any]], expected_nets: dict[str, str] | None
) -> list[dict[str, Any]]:
    """The ``power_connectivity.findings[]`` list (issue #1952).

    Per power/ground pin name, in this order:

    1. :data:`RULE_POWER_UNCONNECTED_PIN` when any instance's pin reached no
       net at all.
    2. :data:`RULE_POWER_UNEXPECTED_PIN_NET` when ``expected_nets`` names
       this pin and at least one instance reaches a different net. This
       *replaces* rule 3 for that pin: it is the strictly stronger check
       (it also catches a design where every instance is miswired the same
       way, which cross-instance agreement cannot).
    3. :data:`RULE_POWER_INCONSISTENT_PIN_NET` when no expectation was
       declared for this pin and its instances reach more than one distinct
       net.

    One finding per offending *pin name*, never per instance: with two
    instances disagreeing there is no majority to appeal to, so the check
    reports the disagreement and names both sides rather than nominating a
    winner it cannot justify. Every finding is ``severity: "error"`` -- a
    power/ground pin on the wrong net is a real defect in every case this
    fires, and unlike a ``mismatches[]`` warning there is no verdict for a
    lesser severity to soften (``power_connectivity.status`` is derived from
    the presence of findings, and the report's own ``status`` is untouched).
    """

    from .lvs import (
        RULE_POWER_INCONSISTENT_PIN_NET,
        RULE_POWER_UNCONNECTED_PIN,
        RULE_POWER_UNEXPECTED_PIN_NET,
    )

    by_pin: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_pin.setdefault(row["pin"], []).append(row)

    findings: list[dict[str, Any]] = []
    for pin in sorted(by_pin):
        entries = by_pin[pin]
        expected = (expected_nets or {}).get(pin)

        unconnected = [row for row in entries if row["net"] is None]
        if unconnected:
            groups = _power_net_groups(unconnected)
            findings.append(
                {
                    "rule": RULE_POWER_UNCONNECTED_PIN,
                    "severity": "error",
                    "pin": pin,
                    "expected_net": expected,
                    "description": (
                        f"standard-cell power/ground pin {pin!r} reaches no "
                        f"net at all on {len(unconnected)} instance(s) -- "
                        f"{_describe_power_net_groups(groups)}"
                    ),
                    "instance_count": len(entries),
                    "nets": groups,
                }
            )

        connected = [row for row in entries if row["net"] is not None]
        if not connected:
            continue

        if expected is not None:
            offending = [row for row in connected if row["net"].upper() != expected]
            if offending:
                groups = _power_net_groups(offending)
                findings.append(
                    {
                        "rule": RULE_POWER_UNEXPECTED_PIN_NET,
                        "severity": "error",
                        "pin": pin,
                        "expected_net": expected,
                        "description": (
                            f"standard-cell power/ground pin {pin!r} must "
                            f"reach net {expected!r} "
                            "(options.power_connectivity.expected_nets) but "
                            f"{len(offending)} instance(s) reach a different "
                            f"net -- {_describe_power_net_groups(groups)}. "
                            "Confirm expected_nets still names the net this "
                            "design's power domain actually uses; if this "
                            "design has no power distribution network "
                            "routed at all, the more likely fix is "
                            "request.power in klt place-and-route rather "
                            "than this option"
                            f"{_ground_pin_caveat(pin)}"
                        ),
                        "instance_count": len(entries),
                        "nets": groups,
                    }
                )
            continue

        distinct = {row["net"] for row in connected}
        if len(distinct) > 1:
            groups = _power_net_groups(connected)
            findings.append(
                {
                    "rule": RULE_POWER_INCONSISTENT_PIN_NET,
                    "severity": "error",
                    "pin": pin,
                    "expected_net": None,
                    "description": (
                        f"standard-cell power/ground pin {pin!r} reaches "
                        f"{len(distinct)} distinct nets across "
                        f"{len(connected)} instance(s) -- "
                        f"{_describe_power_net_groups(groups)}. A single-"
                        "power-domain block must wire every instance's "
                        "same-named supply pin to the same net; declare "
                        "options.power_connectivity.expected_nets to state "
                        "which one is correct, or set "
                        "options.power_connectivity: false if this design is "
                        "genuinely multi-domain. If this design has no power "
                        "distribution network routed at all -- every "
                        "instance's supply pin landing on its own net is "
                        "the same signature as no PDN having been added -- "
                        "the more likely fix is request.power in klt "
                        "place-and-route rather than either option above"
                        f"{_ground_pin_caveat(pin)}"
                    ),
                    "instance_count": len(entries),
                    "nets": groups,
                }
            )
    return findings


def _body_verification_unchecked(reason: str) -> dict[str, Any]:
    """A ``body_verification`` block for a run that could not determine how
    the layout's MOS body terminals were resolved (issue #1983), stating
    *why* in ``reason``.

    Always emitted -- including for the pre-extracted ``layout.netlist``
    request form this determination does not apply to -- so the question
    "did this run verify that the device bodies are tied?" is answerable
    from any ``klt lvs`` report on its own, rather than requiring a reader
    to know which ``layout`` request shape produced it and infer the answer
    from the *absence* of a ``device.body_unverified`` warning. That
    absence is exactly the ambiguity issue #1983 reported: on a
    pre-extracted netlist it means "not checked", and on an inline
    extraction it means "checked and clean", and nothing in the report
    distinguished them. Same discipline as
    :func:`_power_connectivity_unchecked`.
    """

    from .lvs import BODY_STATUS_UNCHECKED

    return {
        "status": BODY_STATUS_UNCHECKED,
        "reason": reason,
        "device_classes": [],
        "device_count": 0,
        "findings": [],
        "finding_count": 0,
    }


def _body_verification_report(layout_circuit: Any, deck: Any) -> dict[str, Any]:
    """The ``body_verification`` block for an inline-extraction compare
    (issue #1983) -- the machine-checkable form of the
    ``device.body_unverified`` warning (issue #281).

    Rendered from :func:`~klayout_tools.lvs_mismatch._body_unverified_counts`,
    the *same* determination :func:`_body_net_warnings` renders its prose
    ``mismatches[]`` entries from, so the warning a human reads and the field
    a grader reads can never disagree about how many devices are affected or
    which classes they belong to.

    Why this needs to exist at all, given the warning already did: a
    ``mismatches[]`` entry is not gradeable. Answering "were this layout's
    device bodies verifiably tied?" from a committed ``klt lvs`` record meant
    string-matching a ``category`` inside an array whose other entries are
    ordinary compare findings -- so in practice nothing downstream asked, and
    a record carrying the warning was indistinguishable from a clean one at
    every consumer that only reads ``status`` (``klt signoff`` included). See
    ``docs/cli/lvs.md`` -> ``body_verification``.

    **This does not change ``status``.** A layout with unverified bodies
    still reports ``status: "match"`` when the compare matched, and the
    warning entries are still ``severity: "warning"`` with
    ``mismatch_count`` counting them -- unchanged from before this block
    existed. The block makes the condition *visible to a grader*; whether it
    should also block a verdict is a separate policy question, deliberately
    left to the consumer (see ``docs/design-evidence-tiers.md`` item 7).
    """

    from .lvs import (
        BODY_STATUS_UNVERIFIED,
        BODY_STATUS_VERIFIED,
    )

    counts = _body_unverified_counts(layout_circuit, deck)
    if not counts:
        return {
            "status": BODY_STATUS_VERIFIED,
            "reason": None,
            "device_classes": [],
            "device_count": 0,
            "findings": [],
            "finding_count": 0,
        }
    findings = [
        {"class": device_class, "device_count": count}
        for device_class, count in sorted(counts.items())
    ]
    return {
        "status": BODY_STATUS_UNVERIFIED,
        "reason": None,
        "device_classes": sorted(counts),
        "device_count": sum(counts.values()),
        "findings": findings,
        "finding_count": len(findings),
    }


def _power_pins_derivation(
    masters: list[str],
    corroborated: bool,
    libraries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``power_connectivity.power_pins_derivation`` block (issue #2076):
    how this run decided which pin names are power/ground, and how strong
    the evidence behind that decision is.

    ``power_pins`` is the field a consumer quotes to say what the check
    covered, and before this block existed nothing in the report said where
    that list came from -- a reader could not tell a genuine four-supply
    library (``VPWR``/``VGND``/``VPB``/``VNB``) from three supplies plus a
    misclassified signal pin without reverse-engineering the netlist.

    ``corroborated`` is the honest-evidence flag, computed by
    :func:`_gate_level_power_pin_evidence` (not re-derived here from
    ``len(masters)``): its second condition ("every instantiated master
    declares the pin") is a *cross-master* corroboration, so it says
    nothing when the instantiated masters are all the same declared-pin
    shape -- one master, or several drive-strength variants of one master,
    are equally uninformative. A single-master reference that leaves one of
    that cell's pins dangling offers no evidence separating that pin from
    the cell's supplies. Rather than claim a guessed universe or refuse to
    check designs whose single master connects everything (the
    overwhelmingly common single-master case, where the universe is exactly
    right), the check runs and says so here -- ``corroborated: false`` plus
    a ``reason`` naming the limitation.
    """

    from .lvs import (
        POWER_PINS_RULE_EVERY_INSTANTIATED_MASTER,
        POWER_PINS_RULE_EVERY_MASTER_PER_LIBRARY,
    )

    if libraries is not None:
        # Issue #2707: multi-library derivation. `reason` names each library
        # that could not be corroborated on its own.
        weak = [entry for entry in libraries if not entry["corroborated"]]
        lib_reason = None
        if weak:
            lib_reason = (
                "the power/ground pin universe is derived per library "
                "(union of each library's own intersection), and "
                + "; ".join(
                    f"library {entry['library']!r} contributes "
                    + (
                        f"a single master ({entry['masters'][0]})"
                        if entry["master_count"] == 1
                        else f"{entry['master_count']} masters of one identical pin set"
                    )
                    + ", so no second distinct master corroborates which of "
                    "its declared-but-unconnected pins are supplies"
                    for entry in weak
                )
                + ' (see docs/cli/lvs.md, "power_pins_derivation")'
            )
        return {
            "rule": POWER_PINS_RULE_EVERY_MASTER_PER_LIBRARY,
            "masters": list(masters),
            "master_count": len(masters),
            "corroborated": corroborated,
            "reason": lib_reason,
            "libraries": libraries,
        }
    reason: str | None = None
    if not masters:
        reason = (
            "the reference instantiates no cell of the resolved standard-cell "
            "library, so nothing establishes which pin names are power/ground"
        )
    elif not corroborated:
        if len(masters) == 1:
            reason = (
                "the reference instantiates a single standard-cell master "
                f"({masters[0]}), so no second master can corroborate which of "
                "its declared-but-unconnected pins are supplies -- a signal pin "
                "left dangling on that master is indistinguishable here from a "
                "power/ground pin (see docs/cli/lvs.md, "
                '"power_pins_derivation")'
            )
        else:
            reason = (
                f"the reference instantiates {len(masters)} standard-cell "
                f"masters ({', '.join(masters)}) that all declare an "
                "identical pin set -- e.g. drive-strength variants of one "
                "logical cell -- so no genuinely distinct second shape "
                "corroborates which of their declared-but-unconnected pins "
                "are supplies -- a signal pin left dangling on all of them "
                "is indistinguishable here from a power/ground pin (see "
                'docs/cli/lvs.md, "power_pins_derivation")'
            )
    return {
        "rule": POWER_PINS_RULE_EVERY_INSTANTIATED_MASTER,
        "masters": list(masters),
        "master_count": len(masters),
        "corroborated": corroborated,
        "reason": reason,
    }


def _power_connectivity_unchecked(
    reason: str, derivation: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A ``power_connectivity`` block for a run that did not perform the
    check (issue #1952), stating *why* in ``reason``.

    Always emitted -- including for a reference form this check does not
    apply to -- so the question "was power/ground connectivity verified by
    this run?" is answerable from any ``klt lvs`` report on its own, rather
    than requiring a reader to know which ``reference.form`` the run used
    and what that form's scope boundary is. Making that boundary visible in
    the evidence is the original friction issue #1952 reported.

    ``derivation`` (issue #2076) is the :func:`_power_pins_derivation` block
    when this run got far enough to derive a power-pin universe and stopped
    for some later reason, and ``None`` when it did not (a non-gate-level
    reference form, the ``power_connectivity: false`` opt-out, or no
    derivable universe at all) -- never omitted, so the key's presence is
    not itself a signal a consumer has to branch on.
    """

    from .lvs import POWER_STATUS_UNCHECKED

    return {
        "status": POWER_STATUS_UNCHECKED,
        "reason": reason,
        "power_pins": [],
        "power_pins_derivation": derivation,
        "instance_count": 0,
        "expected_nets": None,
        "unchecked_expected_pins": [],
        "findings": [],
        "finding_count": 0,
    }


def _pin_only_stub_cells(circuit: Any) -> list[str]:
    """Sorted names of the pin-only (black-box) circuits ``circuit``
    instantiates directly -- a circuit with no devices and no subcircuit
    instances of its own, the shape ``klt extract --abstract-cells`` (and
    ``request.layout.abstract_cells``, issue #2853) writes for every
    abstracted cell type. Empty when ``circuit`` instantiates none."""
    names: set[str] = set()
    for subcircuit in circuit.each_subcircuit():
        ref = subcircuit.circuit_ref()
        if ref is None:
            continue
        if any(True for _ in ref.each_device()) or any(
            True for _ in ref.each_subcircuit()
        ):
            continue
        names.add(ref.name)
    return sorted(names)


def _stub_power_connectivity_reason(reference_form: str, stub_cells: list[str]) -> str:
    """``power_connectivity.reason`` for a non-``gate-level-verilog`` compare
    whose layout instantiates pin-only black-box cells (issue #2853).

    Option (b) of that issue, chosen deliberately over a per-instance audit:
    the audit :func:`_power_connectivity_report` runs for the gate-level form
    needs a *power-pin universe* -- which pin names are power/ground -- and
    that form derives it from the PDK library's own pin-order data. A
    ``plain-element``/``subckt-call`` reference has no such source: its stub
    circuits are arbitrary hand-written ``.SUBCKT`` declarations, and naming
    any of their pins "power" would be a guess. It also does not need one:
    the reference *does* carry the stubs' power/ground connections, and the
    comparer pairs a pin-only circuit's pins by name (stub pin order is
    irrelevant), so a power pin wired to the wrong net is already a compare
    mismatch. The report says exactly that, rather than the generic reason
    implying there is nothing cell-level to say.
    """
    shown = ", ".join(stub_cells[:5]) + (
        f", ... ({len(stub_cells)} total)" if len(stub_cells) > 5 else ""
    )
    return (
        f"reference.form is {reference_form!r}, whose reference netlist "
        "carries its own power/ground pins and nets -- they take part in "
        "the ordinary compare. That includes the pin-only black-box cell(s) "
        f"this layout instantiates ({shown}): every stub pin, power/ground "
        "pins included, was compared as ordinary subcircuit-pin "
        "connectivity, paired by pin name (stub pin order does not matter), "
        "so a power pin wired to the wrong net is a compare mismatch, not a "
        "silent pass. No separate per-instance power-pin audit ran: that "
        "audit needs a library-derived power/ground pin universe, which only "
        "the 'gate-level-verilog' form has"
    )


def _with_stub_power_connectivity_reason(
    power_connectivity: dict[str, Any],
    reference_form: str,
    layout_circuit: Any,
) -> dict[str, Any]:
    """``power_connectivity`` with its ``unchecked`` reason replaced by
    :func:`_stub_power_connectivity_reason` when a non-``gate-level-verilog``
    compare's layout instantiates pin-only black-box cells (issue #2853
    option (b): a transistor-level region next to abstracted cells, against a
    reference carrying pin-only stubs for them). Unchanged for the
    ``gate-level-verilog`` form (it runs the real audit) and when the layout
    instantiates no such cell."""
    if reference_form == "gate-level-verilog":
        return power_connectivity
    stub_cells = _pin_only_stub_cells(layout_circuit)
    if not stub_cells:
        return power_connectivity
    return _power_connectivity_unchecked(
        _stub_power_connectivity_reason(reference_form, stub_cells)
    )


def _power_connectivity_report(
    layout_netlist: Any,
    reference_netlist: Any,
    library_pin_orders: Mapping[str, list[str]] | None,
    *,
    expected_nets: dict[str, str] | None,
) -> dict[str, Any]:
    """The ``power_connectivity`` block for a
    ``reference.form: "gate-level-verilog"`` compare (issue #1952).

    **What "PG connectivity" means here**: per-standard-cell-instance
    *pin-to-net* verification. For every abstracted cell instance in the
    layout netlist, every pin the PDK library declares as power/ground must
    reach the net it is supposed to reach -- the net the caller declared
    (``expected_nets``), or, absent a declaration, the same net every other
    instance's same-named pin reaches. It is deliberately **not** a
    geometric rail/grid continuity check: that is a different question,
    answered today by ``klt ring-check`` (is this ring a closed annulus?)
    and ``klt power`` (does the grid carry the current?), and answering it
    here would need the routed geometry this compare never sees.

    **Why this is checkable at all when the reference is signal-only.** The
    reference Verilog carries no power connectivity, so there is nothing to
    compare *against* -- which is exactly why ``docs/cli/lvs.md`` documents
    this form as signal-only. But the check does not need the reference's
    connectivity: it needs the reference *library*'s pin data (which pins
    are power/ground -- :func:`_gate_level_power_pin_names` already derives
    that, structurally, for issue #1622's pruning) plus the invariant that a
    ``klt place-and-route`` block is single-power-domain by construction.
    The layout side already carries the real per-instance power connectivity
    (``klt extract --abstract-cells`` resolves and probes every declared pin,
    power pins included). So the missing half of "full LVS" for this form is
    recoverable from data ``run_lvs`` has already read, without a new verb,
    a new input file, or a PDK-specific power-pin table.

    Returns :func:`_power_connectivity_unchecked` when there is no derivable
    power-pin universe (a reference with no library-cell circuits at all) or
    when no instance in the layout carries a power/ground pin -- "no
    evidence" is never reported as a clean verdict, mirroring
    :func:`_is_power_only_circuit`'s own missing-evidence discipline.

    **``expected_nets`` naming a pin nothing was observed on is surfaced,
    not silently accepted (issue #1978).** ``expected_nets`` is validated
    only for shape by :func:`_parse_power_connectivity`, before any netlist
    is loaded -- it cannot yet know which pin names the layout/library pair
    will actually resolve. :func:`_power_connectivity_findings` only ever
    produces a finding for a pin name that appears in ``rows``, so an
    ``expected_nets`` key naming a pin :func:`_power_pin_connections` never
    resolved (a typo, or a PDK standard cell whose tie pin carries no
    in-cell label/LEF port at all) matches zero rows and produces zero
    findings -- indistinguishable, without this field, from "checked and
    found correct". ``unchecked_expected_pins`` below names exactly those
    keys, so a caller can tell the two cases apart.

    **Where ``power_pins`` came from is reported, not left to be inferred
    (issue #2076).** ``power_pins_derivation`` names the rule
    :func:`_gate_level_power_pin_evidence` applied, the library masters the
    reference instantiates (the evidence it applied that rule to), and
    whether that evidence was corroborated by more than one master -- see
    :func:`_power_pins_derivation`.
    """

    from .lvs import (
        POWER_STATUS_MATCH,
        POWER_STATUS_MISMATCH,
        _gate_level_power_pin_evidence_detail,
    )

    power_pin_names, masters, corroborated, libraries = (
        _gate_level_power_pin_evidence_detail(reference_netlist, library_pin_orders)
    )
    derivation = _power_pins_derivation(masters, corroborated, libraries)
    if not power_pin_names:
        return _power_connectivity_unchecked(
            "no power/ground pin universe could be derived from the "
            "reference library's own pin-order data -- nothing establishes "
            "which of this design's pins are power/ground pins",
            derivation,
        )
    rows = _power_pin_connections(layout_netlist, power_pin_names)
    if not rows:
        return _power_connectivity_unchecked(
            "no layout-side subcircuit instance declares any of the "
            "reference library's power/ground pins "
            f"({', '.join(sorted(power_pin_names))}) -- the layout netlist "
            "carries no power connectivity to check",
            derivation,
        )
    findings = _power_connectivity_findings(rows, expected_nets)
    observed_pins = {row["pin"] for row in rows}
    return {
        "status": POWER_STATUS_MISMATCH if findings else POWER_STATUS_MATCH,
        "reason": None,
        "power_pins": sorted(observed_pins),
        "power_pins_derivation": derivation,
        "instance_count": len({(row["circuit"], row["instance"]) for row in rows}),
        "expected_nets": dict(sorted(expected_nets.items())) if expected_nets else None,
        "unchecked_expected_pins": (
            sorted(set(expected_nets) - observed_pins) if expected_nets else []
        ),
        "findings": findings,
        "finding_count": len(findings),
    }
