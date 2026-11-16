"""Prose ``warnings[]`` builders for ``klt extract`` (issue #5196).

Pure functions moved out of ``run_extract``: each takes already-detected
findings and returns the message lines to append to ``warnings[]``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def merged_net_label_warnings(
    merged_net_labels: Sequence[dict[str, Any]],
) -> list[str]:
    """Prose ``warnings[]`` line per multi-label merged net (issue #470)."""
    warnings: list[str] = []
    for merged_entry in merged_net_labels:
        labels_str = ", ".join(merged_entry["labels"])
        warnings.append(
            f"net '{merged_entry['net']}' merges "
            f"{len(merged_entry['labels'])} distinct labels ({labels_str}) "
            "onto one net -- KLayout joins multiple labels found on the "
            "same electrical net (as '|' here and in the written netlist, "
            "matching NetlistSpiceWriter's own node-name escaping); this "
            "usually means two differently-named nets were shorted "
            "together in the layout -- see docs/cli/extract.md's "
            "'Merged net labels' section."
        )
    return warnings


def unbiased_pmos_body_warnings(unbiased_pmos_body_nets: Sequence[Any]) -> list[str]:
    """Aggregate warning for PMOS bodies on anonymous unbiased nets (issue #555)."""
    warnings: list[str] = []
    if unbiased_pmos_body_nets:
        device_word = "device" if len(unbiased_pmos_body_nets) == 1 else "devices"
        warnings.append(
            f"{len(unbiased_pmos_body_nets)} PMOS {device_word} tie their "
            "body to an anonymous net with no DC bias path -- no drawn (or "
            f"derivable) well tie connects this PMOS body to a real supply "
            "rail on this layout, so it is left floating rather than tied "
            "to a real supply rail; resimulating this netlist directly "
            "will not reproduce the schematic-level PMOS body bias -- see "
            "unbiased_pmos_body_nets[] for the full list. See "
            "docs/cli/extract.md's 'Parasitic (RC) extraction' section."
        )
    return warnings


def pdk_dropped_param_warnings(
    model_bindings: Mapping[str, Any] | None, device_counts: Mapping[str, int]
) -> list[str]:
    """One warning per bound class whose target drops measured params (issue #695)."""
    warnings: list[str] = []
    if model_bindings is not None:
        for class_name in sorted(model_bindings):
            binding = model_bindings[class_name]
            if not binding.dropped_params:
                continue
            count = device_counts.get(class_name, 0)
            if count == 0:
                continue
            device_word = "device" if count == 1 else "devices"
            params_str = "/".join(binding.dropped_params)
            # `binding.subckt` is empty for a "bipolar" binding -- the real
            # subcircuit name is resolved per device, from `binding.variants`
            # (see `_select_bipolar_variant`), not fixed for the whole class
            # -- so name the *target* generically rather than print an empty
            # quoted string.
            target = f"'{binding.subckt}'" if binding.subckt else "its bound target"
            warnings.append(
                f"--pdk binds {count} '{class_name}' {device_word} onto "
                f"{target}, which has no parameter for the extractor's "
                f"measured {params_str} -- these values are "
                "dropped from the written netlist (rerun without --pdk to "
                "recover them from the bare device-class card form). See "
                "docs/cli/extract.md's 'SPICE model binding' section."
            )
    return warnings


def single_terminal_net_warnings(
    single_terminal_nets: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Aggregate warnings for single-terminal nets (issues #596, #599)."""
    warnings: list[str] = []
    single_terminal_gate_count = sum(
        1 for entry in single_terminal_nets if entry["terminal_kind"] == "gate"
    )
    single_terminal_other_count = len(single_terminal_nets) - single_terminal_gate_count
    if single_terminal_gate_count:
        net_word = "net" if single_terminal_gate_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_gate_count} {net_word} connect to exactly "
            "one device terminal -- a MOS gate -- and are not a declared "
            "pin: these gates have no DC path from anywhere else in the "
            "netlist, so they are almost certainly unconnected inputs "
            "rather than legitimate floating nodes; resimulating this "
            "netlist directly will hit a singular matrix on these nets -- "
            "see single_terminal_nets[] for the full list."
        )
    if single_terminal_other_count:
        net_word = "net" if single_terminal_other_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_other_count} {net_word} connect to exactly "
            "one device terminal -- source/drain/body/resistor-equivalent, "
            "not a gate -- and are not a declared pin; this can be a "
            "legitimate single-terminal tie (e.g. an intentionally "
            "unterminated dummy's diffusion tie), but confirm these nets "
            "have no other intended connectivity -- see "
            "single_terminal_nets[] for the full list."
        )
    return warnings


def ignored_layer_warnings(
    ignored_layers: Sequence[Mapping[str, Any]], deck_name: str
) -> list[str]:
    """Aggregate warning for layers outside the connectivity graph (issue #666)."""
    warnings: list[str] = []
    if ignored_layers:
        layer_word = "layer" if len(ignored_layers) == 1 else "layers"
        be_word = "is" if len(ignored_layers) == 1 else "are"
        total_shapes = sum(entry["shapes"] for entry in ignored_layers)
        shape_word = "shape" if total_shapes == 1 else "shapes"
        layers_str = ", ".join(
            f"{entry['layer']}/{entry['datatype']}" for entry in ignored_layers
        )
        warnings.append(
            f"{len(ignored_layers)} {layer_word} ({layers_str}) carrying "
            f"{total_shapes} {shape_word} {be_word} outside '{deck_name}' "
            "deck's connectivity graph -- this geometry is invisible to "
            "extraction, so a net routed only through it extracts as "
            "multiple disconnected nets instead of one, which will "
            "silently mismatch a downstream `klt lvs` reference netlist -- "
            "see ignored_layers[] for the full per-layer shape counts. See "
            "docs/cli/extract.md's 'ignored_layers' field documentation."
        )
    return warnings


#: How many concrete ``<instance>: <pins> -> <net>`` examples
#: :func:`_tied_abstract_pin_warning` spells out before falling back to a bare
#: count. A block whose pin binding has genuinely gone wrong produces one
#: entry per affected instance (126 of them in issue #1366's reported repro),
#: which is a diagnostic, not a message a caller should have to read in full.
_ABSTRACT_TIED_PIN_WARNING_EXAMPLES = 5

#: The share of one abstracted instance's *declared* pins that must land on a
#: **single** net before :func:`_collapsed_abstract_pin_warning` escalates the
#: ordinary ":func:`_tied_abstract_pin_warning` ties exist" report into its own
#: louder ``warnings[]`` entry -- issue #2695 (the deferred half of #2658).
#:
#: Compared **strictly** (``>``), so the natural "exactly half the pins" shapes
#: a real library draws on purpose stay unescalated: sky130's four-pin
#: `decap`/`tap` cells declare ``VPWR``/``VPB`` and ``VGND``/``VNB`` and tie
#: each pair to its own rail, i.e. a dominant net carrying 2 of 4 pins == 0.5.
#: The reported collapse this escalation exists for is ~95% (#2658: 109
#: declared pins, nearly all on one parent net).
_ABSTRACT_COLLAPSED_PIN_RATIO = 0.5

#: How many separately declared pins a single net must carry before the
#: majority-share test above is even applied -- issue #2695.
#:
#: The ratio alone cannot separate a collapse from the smallest legal ties: a
#: deliberate two-pin tie cell (``tests/test_extract.py::
#: test_abstract_cells_warns_when_one_instance_ties_two_declared_pins``) is a
#: 2-of-2 == 100% dominant net by construction, and a three-pin
#: ``VPWR``/``VPB``/``VGND`` fill cell is 2-of-3 == 67%, both entirely legal
#: and both louder-than-useful if escalated. Four or more *separately
#: declared* pins sharing one net, while being a majority of the macro's port
#: list, is not a shape a correct library cell draws -- it is the signature of
#: pins falling through onto one wide parent net.
_ABSTRACT_COLLAPSED_PIN_MIN_PINS = 4


def _tied_abstract_pin_warning(
    tied: list[tuple[str, str, str, list[str], int]],
) -> list[str]:
    """A single ``warnings[]`` entry naming abstracted-cell instances that
    resolved two or more of their *separately declared* pins onto one net --
    or ``[]`` when there are none (issue #1366).

    ``tied`` is ``[(instance name, cell name, net name, [pin names],
    <declared pin count for that cell type>), ...]`` in
    :func:`_wire_abstract_cells`'s own deterministic instance/pin order. The
    declared-pin count is unused here (this entry reports the *existence* of
    ties, at any ratio) and carried for
    :func:`_collapsed_abstract_pin_warning`, which escalates the subset of
    these entries whose tied share of the macro's declared pins crosses
    :data:`_ABSTRACT_COLLAPSED_PIN_RATIO` (issue #2695).

    **Why warn rather than fail.** Tying two declared pins together is legal
    -- a deliberately tied-off input is the standard case -- so this cannot
    be an error. But on a ``--abstract-cells`` black box it is also
    *unverifiable* by the caller: the cell's interior was erased by
    construction, there are no devices left to cross-check against, and the
    extraction summary otherwise reports a completely healthy run (``status:
    extracted``, the expected pin count, a device count of 0). That is
    exactly how issue #1366's pin-to-net binding regression stayed silent
    all the way to a downstream ``klt lvs`` verdict that read as a design
    fault: both impossible bindings it reports collapse to this one
    observable shape -- a ground-role pin resolved onto the power net leaves
    that instance's ``VDD`` and ``VSS`` pins on one net, and an output
    resolved onto one of its own instance's inputs leaves that instance's
    output and input pins on one net.

    **Why it is still a warning after issue #2658, and not escalated above
    a ratio.** That issue reports the same observable shape at extreme
    scale -- a 109-pin hard macro with ~104 pins on one net -- and proposes
    (as an alternative to fixing the binding) raising this to an
    application error once the tied-pin ratio crosses some threshold. The
    binding fault behind it is fixed at source instead
    (:func:`lef_layer_probe_roles`: a LEF port is now probed on its own
    declared metal before the bottom-up fallback that collapsed it), so the
    escalation would now fire almost exclusively on the *legal* case this
    docstring opens with -- and a hard failure there needs an opt-out flag
    (``--allow-tied-pins`` or a deck setting) that nothing else in the
    extraction contract has a use for. The escalation is therefore
    deliberately deferred, not dropped: it is tracked as issue #2695, as
    defence-in-depth for a collapse this fix cannot reach (a run without
    ``--pdk``, a PDK with no KLayout layer map, or a LEF whose port layers
    the map does not name), where the role-free bottom-up fallback still
    applies.

    Deliberately *not* a separate "two supply-role pins on one net" check
    (the first of the two self-checks issue #1366 proposes): an
    :class:`~klayout_tools.decks.ExtractionDeck` declares no power/ground
    net names -- supply nets get their names from the layout's own drawn
    labels like any other net -- so there is no deck-side notion of a
    "supply-role pin" to key that check on. This check subsumes it anyway,
    since a supply short is a two-pins-one-net collision by definition.
    """
    if not tied:
        return []
    examples = [
        f"{instance_name} (cell '{cell_name}'): pins "
        f"{', '.join(pin_names)} -> net '{net_name}'"
        for instance_name, cell_name, net_name, pin_names, _declared in tied[
            :_ABSTRACT_TIED_PIN_WARNING_EXAMPLES
        ]
    ]
    remainder = len(tied) - len(examples)
    if remainder > 0:
        examples.append(f"... and {remainder} more instance(s)")
    return [
        f"{len(tied)} --abstract-cells instance(s) resolved two or more of "
        "their separately declared pins onto the same net: "
        + "; ".join(examples)
        + ". This is legal for a deliberately tied-off pin, but an "
        "abstracted cell is a black box whose interior cannot be inspected "
        "to confirm that, and it is also the signature of a pin-to-net "
        "binding fault (a ground-role pin resolved onto the power net, or "
        "an output resolved onto one of its own instance's inputs) -- "
        "confirm these against the design's intent before trusting a "
        "downstream `klt lvs` verdict built on this netlist"
    ]


def _collapsed_abstract_pin_warning(
    tied: list[tuple[str, str, str, list[str], int]],
) -> list[str]:
    """A second, deliberately **louder** ``warnings[]`` entry for the subset
    of :func:`_tied_abstract_pin_warning`'s ties where *most* of one
    abstracted instance's declared pins collapsed onto a **single** net --
    or ``[]`` when none does (issue #2695, the deferred half of #2658).

    ``tied`` is :func:`_wire_abstract_cells`'s accumulator,
    ``[(instance name, cell name, net name, [pin names], <declared pin count
    for that cell type>), ...]``. An entry is escalated when its net carries
    both

    - at least :data:`_ABSTRACT_COLLAPSED_PIN_MIN_PINS` of that instance's
      separately declared pins, **and**
    - strictly more than :data:`_ABSTRACT_COLLAPSED_PIN_RATIO` of them.

    Both bounds are load-bearing, and both exist to keep this entry off the
    legal cases the plain tie warning is built to tolerate -- see each
    constant's own derivation. The ratio is per *net*, not per instance: it is
    the **dominant** net's share of the macro's port list, so a cell that ties
    ``VPWR``/``VPB`` and ``VGND``/``VNB`` to their own two rails is 2-of-N
    twice rather than 4-of-N once, and only a genuine single-net collapse
    (#2658's report: 109 declared pins, nearly all on one parent net) crosses
    the line.

    **Why a warning and not an error** (the decision issue #2695 exists to
    record). This stays in ``warnings[]``; there is deliberately no
    ``ExtractError`` path and no ``--allow-tied-pins`` opt-out:

    - Above the threshold the condition is *still legal*. Nothing in the
      extraction contract forbids a macro from tying most of its ports
      together, and an abstracted cell is unverifiable by construction (its
      interior was erased), so the extractor cannot distinguish a deliberate
      multi-way tie from a binding fault -- only the design's own intent can.
      Failing the run would therefore reject correct netlists, exactly the
      reasoning :func:`_tied_abstract_pin_warning` already records for the
      unconditioned case and that ``docs/cli/extract.md`` records for issue
      #2142's identical "make it an error" ask.
    - A hard failure would need an escape hatch the CLI does not have, so the
      strict half of the sketch costs a new flag plus its JSON-contract
      surface to gate a check that cannot be made sound anyway. A distinct
      warning string in the existing ``warnings: array<string>`` container
      buys the whole diagnostic value at no contract cost.

    So this function's job is purely to make the shape *unmissable* in a
    ``warnings[]`` list that a whole-block run legitimately fills with
    body-tie entries: it states the ratio and names the dominant net, which
    the aggregated tie entry does not.
    """
    collapsed = [
        (instance_name, cell_name, net_name, pin_names, declared)
        for instance_name, cell_name, net_name, pin_names, declared in tied
        if declared > 0
        and len(pin_names) >= _ABSTRACT_COLLAPSED_PIN_MIN_PINS
        and len(pin_names) / declared > _ABSTRACT_COLLAPSED_PIN_RATIO
    ]
    if not collapsed:
        return []
    examples = [
        f"{instance_name} (cell '{cell_name}'): {len(pin_names)} of "
        f"{declared} declared pins ({len(pin_names) / declared:.0%}) -> net "
        f"'{net_name}' (pins {', '.join(pin_names)})"
        for instance_name, cell_name, net_name, pin_names, declared in collapsed[
            :_ABSTRACT_TIED_PIN_WARNING_EXAMPLES
        ]
    ]
    remainder = len(collapsed) - len(examples)
    if remainder > 0:
        examples.append(f"... and {remainder} more instance(s)")
    return [
        f"MOST PINS ON ONE NET: {len(collapsed)} --abstract-cells instance(s) "
        "resolved a majority of their separately declared pins onto a single "
        "net: " + "; ".join(examples) + ". This is the many-pins-one-net "
        "collapse signature (issue #2658): when a pin's own access point "
        "lands on no conductor of its own layer, the cross-layer fallback can "
        "bind it to whatever wide parent shape happens to run under it, "
        "collapsing most of a black box's port list onto that one net. It is "
        "reported rather than failed because a deliberately tied-off pin is "
        "legal and an abstracted cell's interior cannot be inspected to "
        "confirm which this is -- so check the dominant net named above "
        "against the macro's own declared port list before trusting a "
        "downstream `klt lvs` verdict built on this netlist"
    ]


def isolated_substrate_global_warning(
    top_circuit: Any,
    deck: Any,
    substrate_pins_by_cell: Mapping[str, set[str]],
    report: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Aggregate warning for a LEF-merged substrate pin whose net carries no
    other connection (issue #5632).

    ``substrate_pins_by_cell`` is ``{<cell type>: <LEF-merged substrate-role
    pin names>}`` -- every ``--abstract-cells`` matched cell type's
    substrate-role pins (:func:`~klayout_tools.extract_abstract.
    _substrate_pin_names`), which ``_wire_abstract_cells`` binds to the
    deck's synthesized substrate global
    (:func:`~klayout_tools.extract_abstract._substrate_net`) without probing
    (issue #4571). ``report`` is that function's own ``abstracted_cells[]``
    accumulator, naming each cell type's instance count. The binding is
    deterministic per pin name -- every instance of one cell type binds its
    substrate pins the same way -- so the binding count is reconstructed
    from these two inputs rather than accumulated per instance, which is
    what keeps the wiring loop branch-free.

    Nothing downstream of the binding checks what else, if anything, is on
    that net: when the well/substrate tap cell is itself abstracted (its
    PDK LEF declares only ``VGND``/``VPWR``), the substrate global is an
    isolated node, and a PG-pin gate-level reference that ties each
    instance's body pin to the rails cannot match -- the failure lands in
    the topology compare as one ``net.unmatched`` plus a ``topology`` error
    on essentially every instance, with nothing pointing at the
    extract-side cause.

    Fires only when the substrate net carries *no* connection other than
    those merged pins -- no device terminal (a non-abstracted device body),
    no top-level pin, and no other subcircuit pin (a probe-resolved signal
    or a drawn tie). Runs at the end of ``_wire_abstract_cells``, after
    every matched instance has been wired, so the counts are final for this
    pass; the caller's own post-passes (``make_top_level_pins``, purge) do
    not add connectivity to this net either way.

    Always a warning: the run's status and exit code are unchanged -- this
    is the diagnostic whose absence made the regression look like the #3749
    fix's fault, not a defect the extractor can resolve on its own.
    """
    bindings_per_cell = {
        cell: pins for cell, pins in substrate_pins_by_cell.items() if pins
    }
    if not bindings_per_cell:
        return []
    net = top_circuit.net_by_name(deck.substrate_net)
    if net is None:
        return []
    instance_counts = {entry["cell"]: entry["instance_count"] for entry in report}
    total_bindings = sum(
        len(pins) * instance_counts[cell] for cell, pins in bindings_per_cell.items()
    )
    total_instances = sum(instance_counts[cell] for cell in bindings_per_cell)
    other_subcircuit_pins = sum(1 for _ in net.each_subcircuit_pin()) - total_bindings
    if (
        other_subcircuit_pins > 0
        or sum(1 for _ in net.each_terminal())
        or sum(1 for _ in net.each_pin())
    ):
        return []
    cells = sorted(bindings_per_cell)
    cell_word = "cell type" if len(cells) == 1 else "cell types"
    instance_word = "instance" if total_instances == 1 else "instances"
    return [
        f"--abstract-cells bound {total_bindings} substrate-role pin(s) "
        f"({total_instances} {instance_word} across {len(cells)} {cell_word}: "
        f"{', '.join(cells)}) onto the deck's synthesized substrate global "
        f"'{deck.substrate_net}', and that net carries no other connection "
        "in the emitted netlist -- no device terminal and no other "
        "subcircuit pin -- so it is an isolated node: the one cell that "
        "physically ties substrate to ground is abstracted, and its LEF "
        "declares no body pin. A downstream `klt lvs` compare against a "
        "PG-pin reference that ties body pins to the rails will not match "
        f"(topology errors on every body-pinned instance plus an unmatched "
        f"'{deck.substrate_net}'). Assert the tie in the request with "
        'hints.layout_short_nets {"nets": [["' + deck.substrate_net + '", '
        '"<ground net>"]]} -- disclosed as a warning, never silently '
        "assumed -- or draw and label a substrate tie in the cells. See "
        "docs/cli/extract.md's 'Cell-level (black-box + pins) abstraction' "
        "section."
    ]
