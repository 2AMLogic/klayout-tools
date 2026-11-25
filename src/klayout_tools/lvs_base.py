"""Stable LVS mismatch categories, report constants, and request errors."""

#: Stable mismatch-category ids (spike section 2b). Never renumbered/
#: repurposed once shipped -- the same contract guarantee a DRC rule id
#: carries.
CATEGORY_NET_UNMATCHED = "net.unmatched"
CATEGORY_NET_MERGED = "net.merged"
CATEGORY_NET_SPLIT = "net.split"
CATEGORY_DEVICE_UNMATCHED = "device.unmatched"
CATEGORY_DEVICE_CLASS = "device.class"
#: Issue #504: a layout-side and reference-side device class share a name
#: but declare a different terminal list (e.g. a deck's `*WithBulk` device
#: extractor's three-terminal resistor class vs. a plain-element reference's
#: two-terminal one) -- see `_device_class_arity_mismatch`.
CATEGORY_DEVICE_CLASS_ARITY = "device.class_arity"
#: Issue #2421: the reference netlist instantiates **two or more** device
#: classes that the layout-side deck derives from one and the same drawn
#: geometry and tells apart only by a whole-run `deck_options` key
#: (`ResistorDevice.flavour_option`/`CapacitorDevice.flavour_option` -- e.g.
#: gf180mcu's `poly_res`, whose `ppolyf_u_1k`/`_2k`/`_3k` are the *same*
#: drawn `Resistor`-marked poly segment at three sheet-rho interpretations).
#: Because the deck recognises exactly one of those names per run -- the
#: flavour is a property of the wafer's process option, not of the drawn
#: shape -- such a reference cannot match under *any* option value: whichever
#: one is selected, every instance of the other class(es) is compared against
#: a device class the extraction never produces. Reported as a pre-flight
#: diagnosis so the run does not look like an ordinary `device.class`/
#: `device.unmatched` mismatch that a different `deck_options` value might
#: fix (see `_device_class_family_findings`).
CATEGORY_DEVICE_CLASS_FAMILY_UNSATISFIABLE = "device.class_family_unsatisfiable"
#: Issue #4729: the reference names one class of an option-selected family
#: (e.g. gf180mcu `PPOLYF_U_3K`) other than the one `deck_options` selected for
#: this run (`ppolyf_u_1k` by default). Warning only; names both values.
CATEGORY_DEVICE_FLAVOUR_MISMATCH = "device.flavour_mismatch"
#: Issue #4729: parallel layout fingers (same class, L and terminals) left
#: unmerged because `options.combine_devices` is unset, whose widths sum to the
#: width of one reference device. One grouped error replaces the per-finger
#: `device.unmatched`/`device.property` entries.
CATEGORY_DEVICE_UNMERGED_FINGERS = "device.unmerged_fingers"
#: Issue #506: `request.reference.device_bulk` reconciled a reference device
#: class up to the layout side's terminal list before comparing -- the
#: disclosure that a match on that class rests on a caller assertion, not on
#: connectivity read from the reference netlist (see
#: `_apply_reference_device_bulk`).
CATEGORY_DEVICE_BULK_RECONCILED = "device.bulk_reconciled"
#: Issue #2813: `options.finfet_bulk` tied a FinFET layout's bulk net to a named
#: net before comparing (always a warning; see `lvs_finfet.apply_finfet_bulk`).
CATEGORY_DEVICE_FINFET_BULK_TIED = "device.finfet_bulk_tied"
#: Issue #1907: `reference.form: "subckt-call"` converted a resistor/capacitor
#: class whose plain-element value token is the literal `0` *placeholder*
#: (`netlist_normalize.py` has no PDK sheet-resistance/capacitance-per-area
#: table to compute a real one from), so that one parameter was excluded from
#: the compare -- the disclosure that the class's resistance/capacitance
#: dimension was never verified, and that pairing rests on topology and
#: geometry alone (see `_apply_reference_placeholder_values`).
CATEGORY_DEVICE_PLACEHOLDER_VALUE = "device.placeholder_value"
CATEGORY_DEVICE_PROPERTY = "device.property"
CATEGORY_DEVICE_CONNECTIVITY = "device.connectivity"
#: Issue #589: `options.parameter_tolerance` absorbed a matched device pair's
#: parameter difference -- the disclosure that a `"match"` verdict rests on a
#: caller-supplied *design* tolerance rather than on the two values actually
#: agreeing (see `_collect_tolerance_snaps`).
CATEGORY_DEVICE_PARAMETER_TOLERATED = "device.parameter_tolerated"
#: Issue #1928: `options.compare_parameters` disabled a device class
#: parameter via `klayout.db.DeviceClass.enable_parameter(name, False)`
#: before the comparer ran -- the disclosure that a `"match"` verdict rests
#: on a caller-scoped subset of that class's parameters, not the full set the
#: class declares (see `_apply_compare_parameters`).
CATEGORY_DEVICE_PARAMETER_EXCLUDED = "device.parameter_excluded"
#: Issue #2461: a resistor/capacitor device class took part in this compare
#: with one or more of its own declared parameters *never compared* -- KLayout's
#: `DeviceClassResistor` compares only its primary `R` (`L`/`W`/`A`/`P` are
#: secondary), and `DeviceClassCapacitor` only its primary `C` (`A`/`P` are
#: secondary), so the whole geometric sizing of such a class sits outside every
#: default compare. Nothing said so before this category existed: a reader of a
#: `subckt-call` report saw only `device.placeholder_value` (the *value* half)
#: and reasonably concluded the geometry had been compared and only the value
#: had not. Emitted once per participating class per run, never per instance,
#: and never for a parameter some *other* disclosure already names as excluded
#: for this run (`device.placeholder_value`/`device.parameter_excluded`) -- see
#: `_device_parameter_coverage`. Machine-checkable counterpart: the response's
#: `device_parameter_coverage` block, which is built from the same
#: determination.
CATEGORY_DEVICE_GEOMETRY_NOT_COMPARED = "device.geometry_not_compared"
CATEGORY_DEVICE_IMPLAUSIBLE_DIMENSIONS = "device.implausible_dimensions"
CATEGORY_DEVICE_BODY_UNVERIFIED = "device.body_unverified"
CATEGORY_DEVICE_COMBINE_INCOMPLETE = "device.combine_incomplete"
#: Issue #4165: `options.compare_timeout_s` expired; `status` is
#: `"inconclusive"` and this is the only `mismatches[]` entry.
CATEGORY_COMPARE_TIMEOUT = "compare.timeout"
#: Issues #1497/#2374: KLayout's native `Netlist.combine_devices()` can
#: return **normally** with a combined device's parameters inconsistent with
#: the parallel group it folded -- with no exception raised and no
#: `device.combine_incomplete` warning (the existing #1185/#466 retry only
#: catches KLayout's own `RuntimeError`, which never fires here: the call
#: succeeds, just with wrong numbers). Two shapes have been reported:
#:
#: * issue #1497 -- a capacitor's *primary* `C` left at a single pre-combine
#:   instance's own value instead of the summed total across its parallel
#:   group, while that same group's *secondary* `A`/`P` parameters summed
#:   correctly;
#: * issue #2374 -- a `DeviceClassBJT3Transistor` array's accumulation
#:   *inverted*: the emitter parameters the fold should sum (`AE`, `NE`)
#:   left at one instance's value, and the shared base/collector region
#:   parameters it should leave alone (`AB`/`PB`/`AC`/`PC`) summed instead.
#:
#: See `_correct_combine_parameters`.
CATEGORY_DEVICE_COMBINE_PARAMETER_CORRECTED = "device.combine_parameter_corrected"
#: Issue #3017: `options.combine_devices` folded a pure series chain of N>1
#: resistor primitives of a class whose deck row has a nonzero
#: `fixed_offset_ohm`, and the deferred correction (#559) charged that offset
#: once for the folded device -- so `(N-1) * fixed_offset_ohm` the drawn
#: chain physically carries was absorbed by the fold. Always `severity:
#: "warning"`. See `_series_fold_offset_absorbed_warnings`.
CATEGORY_DEVICE_SERIES_FOLD_OFFSET_ABSORBED = "device.series_fold_offset_absorbed"
CATEGORY_PIN_UNMATCHED = "pin.unmatched"
#: Issue #2845: `options.strict_top_level_pin_count` found the two compared
#: top circuits declaring a different number of pins. Always `severity:
#: "error"`; `details` carries both counts.
CATEGORY_PIN_COUNT = "pin.count"
CATEGORY_TOPOLOGY = "topology"
#: Issue #499: a `hints.same_nets` pairing the caller asserted with
#: `must_match=True` that the comparer refused to confirm -- see
#: `_build_mismatches`'s `same_nets_hints` handling.
CATEGORY_HINTS_REJECTED = "hints.rejected"
#: Issue #1085: `options.flatten_reference`/`options.flatten_layout`
#: collapsed that side's subcircuit-call hierarchy into its top circuit(s)
#: before comparing -- the disclosure that a verdict rests on a caller-opted
#: structural flatten rather than the netlist's original hierarchy (see
#: `_flatten_netlist_safely`).
CATEGORY_TOPOLOGY_FLATTENED = "topology.flattened"
#: Issues #4548/#5756: a hierarchy asymmetry between the compared sides --
#: flat layout vs hierarchical reference, or the inverse after the tool's own
#: flatten -- advisory naming the fix (see `_hierarchy_mismatch_warnings`).
CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH = "topology.hierarchy_mismatch"
#: Issue #1552: one of `options.combine_devices_per_circuit`'s own glob
#: patterns matched zero circuits on this side -- most often a typo'd
#: circuit name, or one written in the request's own source-file case
#: instead of the upper-cased form `NetlistSpiceReader` reads circuit names
#: back as (see `_resolve_combine_devices_per_circuit_targets`). Never
#: raised as an `LvsError`: a pattern legitimately naming a circuit that
#: exists on only one side (e.g. a reference-only lumped macro with no
#: layout-side counterpart yet) is not itself a mistake.
CATEGORY_COMBINE_DEVICES_PER_CIRCUIT_UNMATCHED = "combine_devices_per_circuit.unmatched"
#: Issue #1622 (layout side) / #2244 (reference side): a circuit every one of
#: whose declared pins is a power/ground pin of the reference standard-cell
#: library (a filler/tap-cell master, e.g. sky130's unconditionally-inserted
#: `tapvpwrvgnd_1` tap cell or a `fill_*` row-gap filler -- the
#: classification is derived from the library's own pin-order data, see
#: `_gate_level_power_pin_names`), plus every subcircuit instance of it, was
#: removed before comparing against a `reference.form: "gate-level-verilog"`
#: reference -- from the layout side, the reference side, or both, depending
#: on which side(s) actually instantiate it -- see
#: `_prune_power_only_circuits`.
CATEGORY_TOPOLOGY_POWER_ONLY_PRUNED = "topology.power_only_pruned"
#: Issue #4296: interchangeable same-cell instances whose outputs are
#: one-terminal dangling nets on both sides (CTS dummy loads, unused flop
#: outputs) that the comparer cannot pair. Always ``severity: "warning"``;
#: ``status`` is still the engine's own verdict.
CATEGORY_TOPOLOGY_DANGLING_OUTPUT_AMBIGUOUS = "topology.dangling_output_ambiguous"
#: Issue #2021: a `reference.form: "gate-level-verilog"` reference declared a
#: port whose only Verilog-level connection was a plain `assign <port> =
#: ...;` alias (e.g. `assign dbg_uart_byte[i] = rx_byte[i];`, two port names
#: for one electrical node) -- `convert_gate_level_verilog` never resolves a
#: module's own declared port list the way it resolves an instance
#: connection, so that pin reads back as its own isolated, disconnected net.
#: The alias pin's net was joined onto its canonical target's net (`Circuit
#: .connect_pin`) before comparing, so it is not reported as
#: `pin.unmatched`/`net.unmatched` -- see `_apply_gate_level_port_aliases`.
CATEGORY_TOPOLOGY_REFERENCE_PORT_ALIAS_JOINED = "topology.reference_port_alias_joined"
#: Issue #2692: `options.anchor_top_level_pins` tied each top-level pin name
#: declared on *both* sides to its own net on the other side
#: (`NetlistComparer.same_nets(..., must_match=True)`, the same mechanism
#: `hints.same_nets` exposes by hand) before comparing -- the disclosure that
#: this run's verdict rests on name-anchored boundary pins, not on topology
#: alone. `NetlistComparer` pairs nets purely by topology, so without this a
#: topologically-symmetric swap between two boundary nets (two bits of a
#: black-box macro's bus, both wired straight through to a top-level pin) is
#: interchangeable with its neighbour and compares clean. See
#: `_apply_top_level_pin_anchors`; a pin pairing the comparer *refuses* is
#: reported as `hints.rejected` (carrying `details.anchored_top_level_pin`),
#: exactly as a hand-written `hints.same_nets` entry would be.
CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED = "topology.top_level_pins_anchored"

#: Issue #3014: the single ``severity: "warning"`` disclosure a run emits when
#: it applied any ``hints.constant_nets`` binding -- the caller's explicit
#: statement of which layout node carries logic-0 / logic-1. Distinct from
#: :data:`CATEGORY_TOPOLOGY_TOP_LEVEL_PINS_ANCHORED`: those anchors are
#: inferred from pin names, these are declared by the caller. A binding the
#: comparer refuses is reported as `hints.rejected` carrying
#: `details.declared_constant_net`.
CATEGORY_HINTS_CONSTANT_NETS_DECLARED = "hints.constant_nets_declared"
#: Issue #2653: `hints.short_nets` resolved a mask-option (metal-option/
#: probe-pad/e-fuse trim) reference's behavioural option construct to one
#: concrete drawn state before comparing -- every reference net group the
#: request named was joined into one net (a "drawn/shorted" link), and every
#: reference device the request named (both "drawn" and "cut" link devices --
#: neither has a fabricated counterpart in a correctly drawn layout) was
#: removed, before `NetlistComparer` is constructed. See
#: `_apply_short_nets_hint`.
CATEGORY_HINTS_SHORT_NETS_APPLIED = "hints.short_nets_applied"

#: Issue #2854: `layout.rename_declared_pins` found a promoted pin net
#: carrying 2+ declared pin names -- one conductor shorting two declared
#: interface pins, a layout defect the rename pass reports and never
#: repairs (the net keeps its joined name). One entry per offending net,
#: `severity: "error"` but diagnostic-only (it never moves `status` on its
#: own): the compare that already ran reports the mismatch this entry
#: *explains* -- the layout's joined interface net against the reference's
#: two separate pins. Mirrored from `klt extract`'s own
#: `pin_renames.findings[]` (`code: "declared_pin_short"`). See
#: `_declared_pin_short_mismatches`.
CATEGORY_PIN_DECLARED_SHORT = "pin.declared_short"

#: Issue #2952: a reference-side ``X`` card calls a subcircuit that is
#: defined nowhere the reader could see (no ``.subckt`` in the file or its
#: includes), that no reference conversion turned into a device, and that has
#: no same-named circuit on the layout side to be compared against by name.
#: ``NetlistSpiceReader`` does not raise on such a call -- it synthesises an
#: empty, pin-only placeholder circuit -- so the instance carries no devices
#: and, once flattened (``options.flatten_reference``), vanishes without
#: trace: a zero-device layout then reported ``status: "match"`` against a
#: reference whose only device was discarded. One ``severity: "error"`` entry
#: per offending instance (reachable from the compared reference top), and
#: ``status`` is forced to ``"mismatch"``. See
#: ``_unresolved_reference_subckt_calls``.
CATEGORY_REFERENCE_UNRESOLVED_SUBCKT_CALL = "reference.unresolved_subckt_call"

#: Issue #1952: ``power_connectivity.status`` values. Deliberately a
#: *separate* verdict from the report's top-level ``status``, which stays
#: exactly what it has always been -- ``NetlistComparer.compare()``'s own
#: signal-connectivity result. A caller that wants full LVS on a
#: ``reference.form: "gate-level-verilog"`` compare gates on both. See
#: :func:`_power_connectivity_report` and
#: ``docs/design/pg-connectivity-check-decision.md``.
POWER_STATUS_MATCH = "match"
POWER_STATUS_MISMATCH = "mismatch"
POWER_STATUS_UNCHECKED = "unchecked"

#: Issue #1983: ``body_verification.status`` values -- the machine-checkable
#: counterpart of the ``device.body_unverified`` ``mismatches[]`` warning
#: (issue #281). Deliberately a *separate* verdict from the report's top-level
#: ``status``: it is a coverage statement about the layout's MOS bodies, not a
#: compare failure, so it never changes ``status`` (see
#: :func:`_body_verification_report`).
#:
#: - ``"verified"``: every MOS body terminal the check inspected (issue
#:   #5749's ``inspected_count``) resolved to a real, drawn/labelled net.
#: - ``"unverified"``: at least one inspected body terminal was compared
#:   against a deck-synthesized net (the ``device.body_unverified`` condition).
#: - ``"unchecked"``: this run could not answer the question at all -- no
#:   ``layout.deck`` was given (the pre-extracted ``layout.netlist`` form), or
#:   no MOS device was eligible to inspect (``inspected_count`` 0, issue
#:   #5749). Never means "verified": the reason says which case it is.
BODY_STATUS_VERIFIED = "verified"
BODY_STATUS_UNVERIFIED = "unverified"
BODY_STATUS_UNCHECKED = "unchecked"

#: ``power_connectivity.findings[].rule``: one standard-cell power/ground pin
#: name reaches more than one distinct net across the design's instances --
#: the single-power-domain invariant a ``klt place-and-route`` block is built
#: on, violated. One finding per offending pin name (never per instance): the
#: check reports the *disagreement*, and deliberately does not nominate a
#: winner, because with two instances disagreeing there is no majority to
#: appeal to. Declare ``options.power_connectivity.expected_nets`` to get the
#: stronger, absolute form (:data:`RULE_POWER_UNEXPECTED_PIN_NET`) instead.
RULE_POWER_INCONSISTENT_PIN_NET = "power.inconsistent_pin_net"

#: ``power_connectivity.findings[].rule``: the caller declared which net this
#: power/ground pin name must reach (``options.power_connectivity.
#: expected_nets``) and at least one instance's pin reaches a different one.
#: Strictly stronger than :data:`RULE_POWER_INCONSISTENT_PIN_NET` -- it also
#: catches a design where *every* instance is miswired the same way, which no
#: amount of cross-instance agreement can.
RULE_POWER_UNEXPECTED_PIN_NET = "power.unexpected_pin_net"

#: ``power_connectivity.findings[].rule``: a standard-cell instance's
#: power/ground pin resolved to no net at all -- the pin never landed on
#: routed conductor (the extraction could not probe it to a net). Reported
#: separately from the two net-identity rules above because the defect is
#: different in kind: not "connected to the wrong rail" but "connected to
#: nothing".
RULE_POWER_UNCONNECTED_PIN = "power.unconnected_pin"

#: ``power_connectivity.power_pins_derivation.rule`` (issue #2076): the one
#: rule :func:`_gate_level_power_pin_evidence` applies -- a pin name is
#: admitted to the power/ground universe only when **every** library cell the
#: reference instantiates declares it and **no** reference circuit carries it.
#: Stated in the report as a stable identifier so a record reader can tell
#: which derivation a ``power_pins`` list came from without reading this
#: module; a future rule would be a new value here, never a silent change of
#: meaning for this one.
POWER_PINS_RULE_EVERY_INSTANTIATED_MASTER = "declared-by-every-instantiated-master"

#: ``power_connectivity.power_pins_derivation.rule`` (issue #2707): the rule
#: applied when the instantiated masters come from **more than one** pin-order
#: source library (a standard-cell library plus a macro library, #2693). A pin
#: name is admitted when every instantiated master *of one library* declares
#: it and no reference circuit carries it; the universe is the union of those
#: per-library intersections. A hard macro declares its own supply names
#: (``vccd1``/``vssd1``), so a single cross-library intersection would always
#: be empty. A single-library reference keeps
#: :data:`POWER_PINS_RULE_EVERY_INSTANTIATED_MASTER`, with its meaning unchanged.
POWER_PINS_RULE_EVERY_MASTER_PER_LIBRARY = (
    "declared-by-every-instantiated-master-per-library"
)

#: How many individual instances each ``power_connectivity.findings[].nets[]``
#: group names before truncating (``instances_truncated: true``). A real
#: routed block has hundreds of standard-cell instances on one rail, and a
#: finding that dumped all of them would bury the few that actually differ;
#: the group's own ``instance_count`` is always the exact, untruncated total.
_POWER_INSTANCE_SAMPLE_LIMIT = 10

#: Third ``status`` value (issue #1370), alongside ``"match"``/
#: ``"mismatch"``: the compare the caller asked for could not be performed,
#: so no verdict about the design was reached. Emitted only when
#: ``options.combine_devices`` was requested and
#: :func:`_combine_devices_safely` exhausted its retry budget on at least one
#: side -- see :func:`run_lvs`'s symmetric-degrade block. Mirrors ``klt
#: equiv``'s own ``"inconclusive"`` vocabulary and its dedicated exit code
#: (``cli/equiv_cmd.py``'s ``EXIT_INCONCLUSIVE = 4``, ``docs/cli/equiv.md``'s
#: "Timeout and the inconclusive verdict"): a run that could not reach a
#: trustworthy verdict must never be reported under one of the two verdict
#: values a caller gates on.
STATUS_INCONCLUSIVE = "inconclusive"


#: Parameter-name -> reported-property-name map, mirroring ``extract.py``'s
#: own ``w_um``/``l_um`` convention for the two parameters every consumer
#: cares about; every other declared parameter is reported under its own
#: lower-cased name (see this module's docstring: no unit suffix is assumed
#: beyond the two names ``extract.py`` already documents in micrometres).
_PARAM_DISPLAY_NAMES = {"W": "w_um", "L": "l_um"}

#: A parameter difference below this (absolute-or-relative) threshold is
#: floating-point round-trip noise (e.g. a SPICE writer's decimal
#: truncation), not a real design difference -- mirrors ``extract.py``'s own
#: ``_PARAM_PRECISION_UM`` rounding intent, applied here as a compare-time
#: tolerance instead of an output rounding.
_PARAM_ABS_EPSILON = 1e-9
_PARAM_REL_EPSILON = 1e-6

#: Upper bound on ``options.parameter_tolerance`` (issue #589), exclusive. A
#: relative tolerance of ``1.0`` would call any two same-signed values within
#: a factor of the larger equal -- a rubber stamp, not a design tolerance --
#: so it is rejected as a malformed request rather than silently honoured.
_MAX_PARAMETER_TOLERANCE = 1.0

#: How many *extra* ``NetlistComparer.compare()`` passes
#: ``options.parameter_tolerance`` may spend (issue #589). Each pass snaps the
#: in-tolerance parameter differences the previous pass exposed and re-compares;
#: the loop stops as soon as a pass produces no new snap, so this cap only
#: bounds a pathological input, it is not the normal path (one extra pass
#: resolves every case this module's own fixtures produce).
_MAX_TOLERANCE_PASSES = 4

#: Description carried by a ``net.unmatched`` entry that is pure collateral
#: from a single unmatched device pair already reported as
#: ``device.property`` (issue #282, see ``_degraded_param_pair``).
_COLLATERAL_NET_DESCRIPTION = (
    "net has no counterpart on the other side, but only because the one "
    "device pair reported as 'device.property' failed to pair -- no other "
    "device or subcircuit touches this net, so it is collateral, not an "
    "independent connectivity defect"
)


class LvsError(Exception):
    """Raised when an LVS run cannot even be attempted: a missing/malformed
    request file, an unresolvable/unreadable layout or reference netlist, an
    unknown extraction deck, or an unsupported engine.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. Distinct from a documented ``status: "mismatch"`` response --
    that is a trustworthy verdict, not a failure to run (see this module's
    docstring).
    """
