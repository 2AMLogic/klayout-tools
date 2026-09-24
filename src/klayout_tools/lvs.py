"""Compare an extracted/reference netlist pair headlessly and report
structured mismatches, mirroring ``klt drc``'s ``violations[]`` shape.

Pure library: :func:`run_lvs` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py`` /
``extract.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/lvs_cmd.py``).

This is phase 3 of Epic #153 (``klt lvs``/``klt extract``), the build carried
by the accepted spike, ``docs/design/lvs-extraction-spike.md`` -- read that
document first; it settles the engine choice (KLayout's own
``klayout.db.NetlistComparer``/``NetlistSpiceReader``) and the request/
response contract this module implements (its section 2b, ``klt lvs``).
Scope: **schematic-equivalent, topological compare only** -- this module does
not read ``matched_group_id`` (out of scope per the spike's section 4) and
does not do any layout-vs-layout geometric diffing.

Unlike ``klt extract``/``klt drc``, ``klt lvs`` takes a **request document**
(like ``klt sim``/``klt gen``), not positional file args -- it binds two
netlist inputs plus optional matching hints, richer than a flag line carries
cleanly.

Engine: ``klayout.db.NetlistComparer`` fed by a custom
``GenericNetlistCompareLogger`` subclass (see ``_make_compare_logger``) that
captures every raw compare event (net/device/pin/circuit mismatches,
parameter/class differences) into structured Python objects, post-processed
into the documented ``mismatches[]`` shape. The authoritative match/mismatch
verdict is always ``NetlistComparer.compare()``'s own boolean return value -- never
re-derived from how many mismatch entries this module manages to classify.
This is the module's central correctness invariant (the issue's own
complexity note: a comparator report that is too permissive could silently
report "match" on a real mismatch): ``status`` can only be ``"match"`` when
the engine itself says the netlists are equivalent, regardless of any gap in
this module's own event-to-category mapping.

A second engine, ``"netgen"`` (issue #343), wraps the open-flow LVS
comparator ``RTimothyEdwards/netgen`` as a subprocess (``netgen -batch
lvs``) in **netlist-vs-netlist mode only** -- the same layout/reference SPICE
netlists this module already resolves for the ``"klayout"`` engine, written
to temporary files for the subprocess to read. Per the accepted spike
(``docs/design/lvs-extraction-spike.md`` section 1, "netgen (contrast
candidate)"), netgen has no layout front-end of its own -- the open flow
pairs it with ``magic`` for extraction -- so this module deliberately does
**not** wire up a second extraction backend; it only tests whether the
``mismatches[]`` contract generalises to a second, independent comparator
implementation (comparator/contract independence), not whether a second
*extraction* engine agrees with `klt extract` (extraction independence,
explicitly out of scope). See ``_run_netgen_lvs``/``_parse_netgen_report``
for the invocation and the (empirically-verified against netgen 1.5.323,
built from source) report-parsing contract, and the same design doc's
2026-08-02 addendum for the invocation quirks and report-format findings
this issue's own acceptance criteria asked to be written up.

Net-merge/net-split classification (a known simplification): KLayout's
comparer log stream does not label a net mismatch as "merged" or "split" --
it only reports individual net/device mismatch events. This module
distinguishes them heuristically from the *pattern* of co-occurring events
(see ``_classify_net_mismatches``): a leftover, one-sided net on the
**layout** side (no reference counterpart) co-occurring with a
differently-named both-sided pairing is classified ``net.split`` (one
reference net's role divided across more layout nets than expected); the
mirror case on the **reference** side is ``net.merged``. A one-sided leftover
net with no co-occurring renamed pairing is the unambiguous ``net.unmatched``
case. This heuristic is verified against synthetic merge/split fixtures in
``tests/test_lvs.py`` but is not a formal proof for arbitrary multi-defect
inputs -- documented here as a known limitation, the same way ``extract.py``
documents its own curated-deck connectivity-fidelity limits.

"Co-occurring" is scoped to one **weakly-connected component** of the
compared netlists, not to the whole compare run (issue #1533, see
``_net_mismatch_pools``). Pooling every event of a run together made the
pattern global: composing an electrically unrelated, galvanically isolated
block into the same top circuit could turn another block's isolated
``net.unmatched`` into a ``net.split`` -- and, because the ``net.split``
branch does not apply the issue #282 ``explained_*_nets`` downgrade, an
already-tolerated ``severity: "warning"`` into an ``"error"`` -- with no
change to that block's own geometry or connectivity. The components are taken
over the two netlists' own device/subcircuit connectivity *glued along the
comparer's own net pairings*, which is what keeps a genuine split classified
as one: splitting a net severs its fragments' layout-side connection by
construction, but both fragments still reach the single reference net they
came from through the surrounding matched nets.

Minimal-cell parameter recovery (issue #282): the comparer pairs devices from
the *surrounding* net structure and only then compares parameters, so on a
cell small enough that a device's own terminals are that structure (a
two-device inverter with its own substrate/well nets), a parameter-only
defect degrades into one unmatched device per side plus a collateral
unmatched net for each net only those devices touched -- and no parameter
event at all. ``_degraded_param_pair`` recovers the intended
``device.property`` entry from exactly that pattern and downgrades the
collateral to ``severity: "warning"``; see its docstring for the (deliberately
narrow) conditions and ``docs/cli/lvs.md`` -> "Negative controls" for the
caller-facing statement of it.

Design-tolerance compares (issue #589): ``options.parameter_tolerance`` is an
opt-in *relative* tolerance for numeric device parameters, for the real case
where an extracted value (a deck's 5-6-significant-figure SPICE-model fit) and
a schematic reference's hand-rounded card differ by far less than any
manufacturing tolerance yet can never agree to ``_PARAM_REL_EPSILON``. It is
**not** implemented by widening that epsilon: ``status`` is always
``compare()``'s own boolean (see above), and ``compare()`` decides parameter
equality with its own, tighter, non-configurable tolerance *before* this
module's Python-side classification runs -- so a wider epsilon could only ever
suppress a ``device.property`` entry for a pair the engine had already called
mismatched, never move the verdict. Instead ``run_lvs`` snaps the
reference-side value of every in-tolerance parameter difference to its
layout-side counterpart (:func:`_collect_tolerance_snaps`, covering both the
clean ``match_devices_with_different_parameters`` pairing and
:func:`_degraded_param_pair`'s minimal-cell recovery) and runs a **second,
real** ``compare()`` on the now-identical-within-tolerance netlists. Every
snapped parameter is disclosed as a ``severity: "warning"``
``device.parameter_tolerated`` entry carrying both original values, so a
``"match"`` reached through an opted-in tolerance is never silently
indistinguishable from one where the numbers actually agreed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ._paths import _load_request_json, _resolve_relative
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape
from ._provenance import (
    INPUT_ROLE_LAYOUT,
    INPUT_ROLE_NETLIST,
    _content_hash,
    _klayout_version,
    _klt_version,
    build_provenance,
    explicit_deck_options,
    sha256_file,
)
from ._report_verify import (
    VOLATILE_PROVENANCE_PATHS,
    build_check_result,
    build_rerun_result,
    get_path,
    hash_check,
)
from ._report_verify import load_committed_report as _load_committed_report
from .decks import (
    InvalidDeckOptionError,
    UnknownExtractionDeckError,
    deck_source_path,
    get_extraction_deck,
)
from .extract import (
    ExtractError,
    apply_resistor_fixed_offset_corrections,
    extract_netlist_from_layout,
    spice_safe_net_name,
)
from .lvs_mismatch import (
    _apply_tolerance_snaps,
    _body_net_warnings,
    _body_unverified_counts,
    _build_mismatches,
    _build_net_correspondence,
    _collect_tolerance_snaps,
    _device_class_family_findings,
    _make_compare_logger,
    _mismatch,
    _parse_parameter_tolerance,
    _sort_key,
    _SupplyPinUniverse,
    _terminal_names,
    _tolerance_disclosure,
)
from .lvs_mismatch import _classify_net_mismatches as _classify_net_mismatches
from .lvs_mismatch import _net_mismatch_pools as _net_mismatch_pools
from .lvs_netgen import (
    _NETGEN_DEFAULT_TIMEOUT_S,
    _resolve_netgen_binary,
    _resolve_netgen_setup,
    _run_netgen_lvs,
)
from .lvs_supply import parse_supply_nets, supply_fragmentation_findings
from .netlist_capacitor_recovery import (
    custom_device_classes_for_deck,
    make_capacitor_class_recovery_reader,
    parse_capacitor_class_comments,
    resistor_classes_for_deck,
)
from .pdk import PdkNotFoundError, find_pdk
from .verilog_netlist import (
    VerilogNetlistError,
    collect_gate_level_port_aliases,
    convert_gate_level_verilog,
    parse_subckt_pin_orders,
)

if TYPE_CHECKING:
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``klayout`` (in-process ``NetlistComparer``) is the primary engine;
#: ``netgen`` (issue #343) is a second, independent comparator wrapped as a
#: subprocess in netlist-vs-netlist mode only -- see this module's docstring
#: for the scope boundary the accepted spike drew around it.
SUPPORTED_ENGINES = ("klayout", "netgen")

#: Accepted ``request.reference.form`` values (issue #280, extended by issue
#: #1336). ``"plain-element"`` (default) is the schematic-equivalent form
#: ``klt lvs`` has always required; ``"subckt-call"`` opts into converting a
#: PDK schematic flow's simulation-form netlist first; ``"gate-level-verilog"``
#: opts into converting a `klt place-and-route` `verilog_path` gate-level
#: Verilog netlist first (see ``docs/cli/lvs.md``).
_REFERENCE_FORMS = ("plain-element", "subckt-call", "gate-level-verilog")

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
#: Issue #506: `request.reference.device_bulk` reconciled a reference device
#: class up to the layout side's terminal list before comparing -- the
#: disclosure that a match on that class rests on a caller assertion, not on
#: connectivity read from the reference netlist (see
#: `_apply_reference_device_bulk`).
CATEGORY_DEVICE_BULK_RECONCILED = "device.bulk_reconciled"
#: Issue #1907: `reference.form: "subckt-call"` converted a resistor/capacitor
#: class whose plain-element value token is the literal `0` *placeholder*
#: (`netlist_normalize.py` has no PDK sheet-resistance/capacitance-per-area
#: table to compute a real one from), so that one parameter was excluded from
#: the compare -- the disclosure that the class's resistance/capacitance
#: dimension was never verified, and that pairing rests on topology and
#: geometry alone (see `_apply_reference_placeholder_values`).
CATEGORY_DEVICE_PLACEHOLDER_VALUE = "device.placeholder_value"
CATEGORY_DEVICE_PROPERTY = "device.property"
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
CATEGORY_DEVICE_BODY_UNVERIFIED = "device.body_unverified"
CATEGORY_DEVICE_COMBINE_INCOMPLETE = "device.combine_incomplete"
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
CATEGORY_PIN_UNMATCHED = "pin.unmatched"
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
#: ``status``, exactly as :data:`POWER_STATUS_MATCH` and friends are: a
#: ``"unverified"`` body is a coverage statement about how the layout's MOS
#: bodies were resolved, not a compare failure, so it never changes ``status``
#: (that non-blocking behaviour is unchanged from before this block existed).
#: See :func:`_body_verification_report`.
#:
#: - ``"verified"``: inline extraction ran and every MOS body terminal in the
#:   layout's top circuit resolved to a real, drawn/labelled net.
#: - ``"unverified"``: at least one MOS body terminal was compared against a
#:   deck-synthesized net instead (the ``device.body_unverified`` condition).
#: - ``"unchecked"``: this run could not answer the question at all -- the
#:   pre-extracted ``layout.netlist`` request form, which carries no deck and
#:   therefore no way to tell a drawn tie from a synthesized one. Never means
#:   "verified": the reason says which case it is.
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

#: How many individual instances each ``power_connectivity.findings[].nets[]``
#: group names before truncating (``instances_truncated: true``). A real
#: routed block has hundreds of standard-cell instances on one rail, and a
#: finding that dumped all of them would bury the few that actually differ;
#: the group's own ``instance_count`` is always the exact, untruncated total.
_POWER_INSTANCE_SAMPLE_LIMIT = 10

#: Substring KLayout's own ``Netlist.combine_devices()`` internal-consistency
#: ``RuntimeError`` always carries (issue #466) -- e.g. "Internal error:
#: Terminal still connected after removing device in device combination:
#: name=, circuit=<top>, terminal=E in Netlist.combine_devices". Narrows the
#: ``except RuntimeError`` in :func:`_combine_devices_safely` to *this*
#: KLayout-internal invariant violation (a partial-match device group -- N
#: real + M dummy instances sharing two of three terminals, only the N real
#: ones also matching the third) rather than swallowing an unrelated
#: ``RuntimeError`` some other code path might raise.
_COMBINE_DEVICES_ERROR_MARKER = "Netlist.combine_devices"

#: The identifier fields KLayout embeds in the message of the error
#: :data:`_COMBINE_DEVICES_ERROR_MARKER` matches -- e.g. "``... device
#: combination: name=, circuit=<top>, terminal=E in
#: Netlist.combine_devices``" (issue #1370). ``name`` is the failing
#: device's own name (frequently empty: the device is mid-removal when the
#: invariant trips, so KLayout has nothing to print), ``circuit`` is the
#: circuit that device lives in, and ``terminal`` is the terminal that was
#: still connected. Parsed -- rather than passed through as a raw string --
#: so a ``device.combine_incomplete`` entry carries the machine-readable
#: ``circuit``/``device``/``net`` identifiers every other ``mismatches[]``
#: entry carries, instead of only prose a caller has to regex itself.
_COMBINE_DEVICES_ERROR_FIELDS_RE = re.compile(
    r"name=(?P<device>[^,]*),\s*circuit=(?P<circuit>[^,]*),\s*"
    r"terminal=(?P<terminal>[^\s,]*)"
)

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

#: Bounded retry budget for :func:`_combine_devices_safely` (issue #1185).
#:
#: KLayout's own ``Netlist.combine_devices()``/``Circuit.combine_devices()``
#: groups combination candidates internally in a C++ ``std::map`` keyed on
#: raw ``db::Net *`` pointer values (see ``Circuit::combine_parallel_devices``
#: in KLayout's ``dbCircuit.cc``), and then repeats its parallel/serial
#: combination passes to a fixed point (``Circuit::combine_devices()``'s own
#: ``while (any) { ... }`` loop). Neither the grouping order nor that
#: fixed-point iteration is exposed to, or overridable from, Python -- there
#: is no argument, hook, or per-device-class ordering control on
#: ``combine_devices()``, and the primitives that actually perform a merge
#: (``join_device``/``join_terminals``) are internal-only, never exposed via
#: the ``klayout.db`` GSI bindings (verified against ``klayout==0.30.10``).
#: Because the grouping key depends on each ``db::Net`` object's *process*
#: heap address (which varies run to run, e.g. via ASLR) rather than any
#: property of the netlist's content, two runs against the byte-identical
#: layout GDS + reference netlist can walk a "partial-match device group"
#: (instances sharing only some, not all, of their matching terminals) in a
#: different candidate order and land on a different outcome -- one run
#: combines cleanly, the next hits the internal-consistency ``RuntimeError``
#: this module already degrades gracefully (issue #466). This is a genuine
#: KLayout-internal limitation this module cannot deterministically avoid
#: (confirmed empirically: neither ``PYTHONHASHSEED`` pinning nor
#: reconstructing the reported partial-match shape from Python reproduces or
#: eliminates it on demand -- see the "combine_devices() partial-match
#: RuntimeError" section of ``tests/test_lvs.py`` for the prior, unsuccessful
#: reproduction attempts).
#:
#: What *is* controllable from Python: each retry below runs against an
#: independent ``Netlist.dup()`` copy, so it samples a fresh instance of
#: KLayout's address-dependent ordering rather than reusing whatever ordering
#: the first attempt happened to get. Retrying trades a bounded amount of
#: extra work (at most this many ``combine_devices()`` calls, each on its own
#: full netlist copy) for a large cut in the *observed* flake rate: at the
#: issue's own reported ~1-in-5 (20%) single-attempt failure rate, 5
#: independent attempts fail together only ~0.2**5 = 0.0032% of the time.
#: This does not make the outcome *provably* deterministic -- it cannot, per
#: the paragraph above -- but it converts a caller-visible ~20% flake rate
#: into one small enough that a stable `klt lvs` "match" is once again a
#: practical automation gate. See ``docs/cli/lvs.md``'s
#: `options.combine_devices` and `device.combine_incomplete` sections for the
#: caller-facing disclosure.
_COMBINE_DEVICES_MAX_ATTEMPTS = 5

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


_REQUIRED_REQUEST_FIELDS = ("layout", "reference")


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt lvs`` request JSON file.

    Raises :class:`LvsError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``layout``, ``reference``).
    Does not require a ``schema`` field, matching ``klt sim``'s
    ``load_request`` (user-authored input, never emitted by this tool).
    """
    request = _load_request_json(request_path, LvsError)
    return _validate_request_shape(request, "request file")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared ``layout``/``reference`` shape check for a JSON-decoded
    request, however it was sourced (file, inline JSON, stdin). ``source``
    is folded into the "must be a JSON object" error for context.
    """
    return _shared_validate_request_shape(
        data,
        source,
        error_cls=LvsError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve the ``klt lvs`` CLI ``request`` argument into a request dict
    plus the directory relative paths inside it should resolve against.

    ``value`` is one of three forms (see docs/cli/lvs.md):

    - ``"-"`` -- read the request JSON document from stdin. Relative paths
      inside it resolve against the current working directory, since there
      is no request *file* to anchor them to.
    - a path to an existing, readable file -- read and parse that file
      (delegates to :func:`load_request`, unchanged). Relative paths
      resolve against the file's own directory, exactly as before.
    - anything else -- parsed as an inline JSON object string. This mirrors
      ``klt gen --params``'s ``load_params_arg`` (``gen.py``): an existing
      file always wins first, so this only applies once ``os.path.isfile``
      has already said no. Relative paths resolve against the current
      working directory, same as the stdin form.

    Raises :class:`LvsError` for any read/parse/shape failure -- the same
    exception type :func:`load_request` raises, so callers (``run_lvs``,
    ``cli/lvs_cmd.py``) do not need to distinguish the three forms.
    """
    return _shared_load_request_arg(
        value,
        error_cls=LvsError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


def run_lvs(request: str) -> dict[str, Any]:
    """Run the netlist compare declared by ``request``.

    ``request`` accepts the same three forms every ``klt lvs`` request
    argument does (see :func:`load_request_arg` / docs/cli/lvs.md): a path
    to a request JSON file, ``"-"`` to read the request from stdin, or an
    inline JSON object string. Relative paths *inside* the request document
    (``layout.file``, ``reference.netlist``, etc.) resolve against the
    request file's own directory for the file form, or against the current
    working directory for the stdin/inline forms -- there is no request
    file to anchor them to in that case.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/lvs.md`` / ``docs/design/lvs-extraction-spike.md`` section
    2b). Raises :class:`LvsError` for anything that prevents a trustworthy
    verdict from being produced at all (bad request, unresolvable layout/
    reference input, unknown deck, unsupported engine, engine error) --
    a documented ``status: "mismatch"`` is a successful run, not an error
    (see this module's docstring).

    ``device_classes`` (issue #221) echoes the layout-side
    :attr:`~klayout_tools.decks.ExtractionDeck.device_classes` -- what that
    deck can structurally recognise, not what this particular compare found
    -- whenever a ``layout.deck`` is given: always for ``layout.file``
    (inline extraction, where the deck is required), and also for the
    pre-extracted ``layout.netlist`` shape when a ``layout.deck`` is supplied
    alongside it (issue #585). ``null`` only when no ``layout.deck`` was given
    (the bare ``layout.netlist`` form).

    The response echoes back enough of the request to reconstruct the
    compare it describes (issue #1205): ``top`` and ``reference_top`` are
    each side's own resolved top circuit (they differ by construction for an
    LVS negative control -- a ``<cell>_shorted`` layout compared against the
    intact ``<cell>``'s reference netlist), and the ``options`` block echoes
    every compare-shaping option as resolved (``combine_devices``,
    ``flatten_layout``, ``flatten_reference``, ``netgen_setup``,
    ``parameter_tolerance``). That is what lets ``klt lvs --check <report>
    --rerun`` re-run *this* compare rather than a differently-shaped one
    whose difference it would then report as drift -- see
    :func:`_reconstruct_lvs_request`.

    The response also carries the shared ``provenance`` block (see
    :func:`klayout_tools._provenance.build_provenance`); its ``deck`` is the
    layout-side extraction deck (``null`` only when no ``layout.deck`` was
    given, matching ``device_classes``), and ``pdk`` is ``null`` (LVS is
    topological and resolves no PDK).

    ``layout.deck_options`` (issue #600) is the JSON-request-document
    counterpart of ``klt extract --deck-option``: a ``{key: value}`` object
    of string pairs selecting a caller-visible flavour of a shared-geometry
    resistor family (e.g. gf180mcu's ``poly_res``), honored for both layout
    shapes wherever ``layout.deck`` is meaningful -- see
    :func:`~klayout_tools.decks.get_extraction_deck`'s own ``deck_options``
    docstring for the resolution mechanism. Omitting it resolves every deck
    exactly as before this field existed. An unrecognised key/value is a
    clean :class:`LvsError`, not a traceback; giving it without
    ``layout.deck`` is likewise a clean :class:`LvsError` (there is no deck
    to apply it to). The resolved mapping is echoed under
    ``provenance.deck.options`` when non-empty, matching ``klt extract``'s
    own shape.

    Same inline-extraction condition also gates ``device.body_unverified``
    (issue #281, see :func:`_body_net_warnings`): non-blocking
    ``severity: "warning"`` ``mismatches[]`` entries noting that some MOS
    body terminals were compared against a deck-synthesized net rather than
    a real schematic one -- never emitted for the pre-extracted
    ``layout.netlist`` form, and never affecting ``status``.

    ``body_verification`` (issue #1983) is that same disclosure in
    machine-checkable form: a top-level block, always present, whose
    ``status`` is ``"verified"``/``"unverified"``/``"unchecked"``
    (:data:`BODY_STATUS_VERIFIED` and friends) -- see
    :func:`_body_verification_report`. Both renderings come from one
    determination, so they cannot disagree. ``status`` is unchanged by it:
    a layout with unverified bodies still matches when the compare matches.
    The block exists because a ``mismatches[]`` warning is not gradeable --
    a downstream consumer (``klt signoff``, a committed evidence record)
    had to string-match a category inside an array of ordinary compare
    findings to ask the question, so in practice nothing asked, and a
    record carrying the warning was indistinguishable from a clean one.

    ``request.reference.device_bulk`` (issue #506) is the reconciliation
    counterpart of that disclosure: it normalises a named reference device
    class up to the layout side's terminal list before comparing (see
    :func:`_apply_reference_device_bulk`), and every class it reconciles
    yields its own ``severity: "warning"``
    ``device.bulk_reconciled`` ``mismatches[]`` entry, so a ``"match"``
    reached through the hook is never silently indistinguishable from one
    reached independently.

    ``request.reference.form: "subckt-call"`` carries a third instance of the
    same discipline (issue #1907): the conversion has no PDK sheet-resistance/
    capacitance-per-area data, so a converted resistor/capacitor card's
    positional value is a literal ``0`` placeholder -- and ``R``/``C`` being
    those classes' *primary*, compared parameter, leaving it in the compare
    stops ``NetlistComparer`` pairing the class at all. That one parameter is
    therefore excluded from the compare on both sides (see
    :func:`_apply_reference_placeholder_values`) and every excluded class
    yields its own ``severity: "warning"`` ``device.placeholder_value``
    ``mismatches[]`` entry.

    ``options.parameter_tolerance`` (issue #589, ``"engine": "klayout"``
    only) is the same discipline applied to device *parameters*: an opt-in
    relative tolerance that lets a compare reach ``status: "match"`` when the
    only remaining difference is a numeric device parameter within it (see
    this module's docstring for the two-pass snap-and-recompare mechanism it
    is implemented with, and why widening this module's own float-noise
    epsilon could not have worked). Omitting it leaves every verdict and
    every ``mismatches[]`` entry exactly as before. The effective value is
    echoed as the response's ``parameter_tolerance`` field (``null`` when
    omitted), and every parameter it absorbs yields its own
    ``severity: "warning"`` ``device.parameter_tolerated`` entry.

    ``options.flatten_reference`` / ``options.flatten_layout`` (issue #1085)
    are the fix for the flat-vs-hierarchical seam ``klt extract``'s
    always-flat extraction creates: a hierarchical reference netlist (one
    leaf ``.subckt`` plus N instance calls of it -- the shape a macro built
    by tiling one verified leaf cell naturally takes) can never structurally
    match a flat layout-side netlist, because ``NetlistComparer`` compares
    circuit-by-circuit, and the flat side simply has no subcircuit-call
    circuit to pair against the reference's. Both options call KLayout's own
    ``Netlist.flatten()`` in-process -- ``options.flatten_reference`` on the
    reference netlist (right after it is read, before circuit selection),
    ``options.flatten_layout`` on the layout netlist (right after it is
    resolved -- meaningful for the pre-extracted ``layout.netlist`` shape,
    which can itself be hierarchical; a no-op for inline ``layout.file``
    extraction, which is already flat). Both default ``false``, so omitting
    them leaves every verdict and every ``mismatches[]`` entry exactly as
    before. Each side that is actually flattened (its circuit count changes)
    yields its own ``severity: "warning"`` ``topology.flattened`` entry, so a
    ``"match"`` reached after flattening is never silently indistinguishable
    from one reached against the netlist's original hierarchy. See
    :func:`_flatten_netlist_safely`.

    ``options.combine_devices`` (issue #261) accepts a bool **or**, since
    issue #1370, a list of device-class names restricting combining to those
    classes -- the escape hatch for a netlist where KLayout's own
    ``combine_devices()`` trips its internal-consistency invariant
    deterministically on one class's partial-match group (see
    :func:`_parse_combine_devices`). Naming a class present on neither side is
    a clean :class:`LvsError`, never a silent no-op.

    That option is also the sole source of this command's third ``status``
    value, ``"inconclusive"`` (:data:`STATUS_INCONCLUSIVE`, issue #1370).
    When :func:`_combine_devices_safely` exhausts its retry budget on either
    side, both netlists are rolled back to ``Netlist.dup()`` snapshots taken
    before combining (a **symmetric degrade** -- never a partially-folded
    layout compared against a fully-folded reference), and a resulting
    ``"mismatch"`` is reported as ``"inconclusive"`` instead: the compare the
    caller asked for did not run, so its outcome is not a statement about the
    design. A ``"match"`` reached that way is left alone -- the engine's
    verdict is always authoritative here, and an uncombined compare that
    still matched is strictly stronger evidence than a combined one. The
    ``device.combine_incomplete`` warning is present in ``mismatches[]``
    either way, now carrying the failing attempt's own
    ``circuit``/``device``/``net`` identifiers.

    ``options.combine_devices_max_attempts`` (issue #1412) is the
    caller-configurable retry budget behind that degrade: how many
    independent ``Netlist.dup()`` attempts :func:`_combine_devices_safely`
    makes per side before falling back to ``device.combine_incomplete`` /
    ``"inconclusive"``. Defaults to :data:`_COMBINE_DEVICES_MAX_ATTEMPTS`
    (``5``, unchanged from before this option existed) when omitted. A caller
    working against a large/complex netlist that observes retry exhaustion
    more often than that default's own derivation predicts can raise this to
    trade runtime for a lower observed exhaustion rate; a caller iterating on
    a small netlist can lower it for faster feedback. Ignored when
    ``options.combine_devices`` is falsy. See ``docs/cli/lvs.md``'s
    ``options.combine_devices`` section for the documented exhaustion-rate
    caveat this knob exists to let a caller work around.

    ``options.combine_devices_per_circuit`` (issue #1552) is a per-macro
    alternative to the single, whole-request ``options.combine_devices``
    above: a ``{<circuit-name-glob>: <bool>}`` mapping applied via
    ``Circuit.combine_devices()`` to each side's own matching circuits,
    *before* either side's optional ``flatten_layout``/``flatten_reference``
    structural flatten runs -- so a composed design's own macros, each
    already independently verified under its own (possibly opposite)
    ``combine_devices`` setting, can keep that setting once composed into one
    top-level design, instead of the two macros' needs colliding into a
    single request-wide flag that cannot satisfy both. Mutually exclusive
    with a truthy ``options.combine_devices`` (a clean :class:`LvsError`).
    See :func:`_parse_combine_devices_per_circuit` and
    :func:`_combine_circuit_devices_safely`.

    ``options.compare_parameters`` (issue #1928) scopes which device-class
    parameters take part in the compare at all: ``{<device-class name>:
    [<parameter name>, ...]}``. Every other parameter that class declares is
    disabled via ``DeviceClass.enable_parameter(name, False)`` on both sides
    before the comparer runs -- the escape hatch for a single
    always-compared parameter neither side can state identically (e.g. a
    geometry-derived layout-side value a reference netlist's own device
    cards never carry), which ``options.parameter_tolerance`` cannot absorb
    (it is a *relative* tolerance and can never call a zero-vs-nonzero
    structural difference equal). ``"engine": "klayout"`` only. A device
    class or parameter name that does not resolve against either netlist is
    a clean :class:`LvsError`, never a silent no-op -- mirrors
    ``hints.same_nets``/``options.combine_devices``'s array form. Every
    excluded parameter is disclosed as a ``severity: "warning"``
    ``device.parameter_excluded`` entry, so a ``"match"`` reached this way
    is never silently indistinguishable from a full parameter compare. See
    :func:`_parse_compare_parameters` and :func:`_apply_compare_parameters`.
    """
    request, request_dir = load_request_arg(request)

    engine = request.get("engine", "klayout")
    if engine not in SUPPORTED_ENGINES:
        raise LvsError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    layout_spec = request["layout"]
    if not isinstance(layout_spec, dict):
        raise LvsError("request.layout must be a JSON object")
    reference_spec = request["reference"]
    if not isinstance(reference_spec, dict):
        raise LvsError("request.reference must be a JSON object")

    # `layout.deck_options` (issue #600) mirrors `klt extract --deck-option`'s
    # resolved `{key: value}` mapping, one layer down: this is a JSON object
    # already, not a `KEY=VALUE` CLI token to parse. Validated the same way
    # `declared_pins` is validated as a list -- a wrong-shaped value is a
    # clean request error, not a silent no-op or a traceback further down in
    # `get_extraction_deck`/`extract_netlist_from_layout`.
    deck_options = layout_spec.get("deck_options")
    if deck_options is not None:
        if not isinstance(deck_options, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in deck_options.items()
        ):
            raise LvsError(
                "request.layout.deck_options must be a JSON object mapping "
                "string keys to string values"
            )
        if deck_options and not layout_spec.get("deck"):
            # Meaningless without a deck to resolve against -- mirrors how
            # `top_cell_pins`/`declared_pins` are only honored alongside
            # inline extraction, except `deck_options` is also honored for
            # the pre-extracted `layout.netlist` + `layout.deck` shape
            # (issue #585), so the real requirement is `layout.deck`, not
            # `layout.file` specifically.
            raise LvsError("request.layout.deck_options requires request.layout.deck")

    options = request.get("options") or {}
    keep_extracted = bool(options.get("keep_extracted", False))
    # Issue #1370: `true`/`false` (the original shape) or a list of
    # device-class names restricting which classes are combined at all --
    # see `_parse_combine_devices`. `combine_devices` below is the resolved
    # value (echoed back verbatim in the response's `options` block);
    # `combine_device_classes` is the restriction (`None` for the bool
    # shape), and `combine_devices_enabled` is the plain "does combining run
    # at all" predicate every downstream branch already keyed on.
    combine_devices = _parse_combine_devices(options)
    combine_device_classes = (
        combine_devices if isinstance(combine_devices, list) else None
    )
    combine_devices_enabled = bool(combine_devices)
    # Issue #1412: caller-configurable retry budget for
    # `_combine_devices_safely`'s bounded-retry mitigation (issue #1185) --
    # previously a private `_COMBINE_DEVICES_MAX_ATTEMPTS` module constant
    # tuned against one small fixture, with no way for a caller working
    # against a much larger/complex netlist to raise it, or a caller
    # iterating on a small one to lower it. Parsed unconditionally (like
    # `flatten_layout`/`flatten_reference` above) so a malformed value is a
    # clean request error even when `combine_devices` is off, but only
    # consulted below when combining actually runs.
    combine_devices_max_attempts = _parse_combine_devices_max_attempts(options)
    # Issue #1552: per-subcircuit override of the whole-request
    # `combine_devices` boolean above -- lets a composed design give each of
    # its own macros its own combine_devices choice, honored simultaneously.
    # Mutually exclusive with a truthy `combine_devices` (bool `true` or a
    # non-empty device-class list): `combine_devices_per_circuit` already
    # lets an unmatched circuit fall back to a chosen default (name it with a
    # catch-all glob, e.g. `"*"`), so there is no combination of the two
    # options that is not ambiguous about which setting wins for a matched
    # circuit. An explicit `combine_devices: false` alongside it is a
    # harmless no-op (that is already every unmatched circuit's default), so
    # only a truthy value raises.
    combine_devices_per_circuit = _parse_combine_devices_per_circuit(options)
    if combine_devices_per_circuit is not None and combine_devices_enabled:
        raise LvsError(
            "request.options.combine_devices and "
            "request.options.combine_devices_per_circuit are mutually "
            "exclusive when combine_devices is truthy -- name every circuit "
            "combine_devices_per_circuit should combine explicitly (a "
            'catch-all `"*": true` glob included, if that is the intent) '
            "instead of also setting the whole-request combine_devices"
        )
    parameter_tolerance = _parse_parameter_tolerance(options)
    # Issue #1928: the resolved `{<device-class>: [<parameter>, ...]}`
    # mapping, or `None` when the option was omitted -- see
    # `_parse_compare_parameters`. Class/parameter names are validated once
    # each netlist is resolved (`_apply_compare_parameters`, further down),
    # the same two-stage split `options.combine_devices`'s list shape uses.
    compare_parameters = _parse_compare_parameters(options)
    # Issue #1952: the per-cell-instance power/ground pin-to-net check that
    # pairs with a signal-only `reference.form: "gate-level-verilog"`
    # compare -- see `_power_connectivity_report`. Parsed unconditionally
    # (like `combine_devices_max_attempts` above) so a malformed value is a
    # clean request error for every reference form, but only consulted for
    # `gate-level-verilog`, the one form whose reference structurally cannot
    # carry power connectivity of its own.
    (
        power_connectivity_enabled,
        power_connectivity_expected_nets,
        power_connectivity_echo,
    ) = _parse_power_connectivity(options)
    supply_nets = parse_supply_nets(options)
    # Issue #1085: opt-in, per-side structural flatten -- see
    # `_flatten_netlist_safely`'s docstring for the full rationale (`klt
    # extract` is always flat, so a hierarchical reference/pre-extracted
    # layout netlist otherwise can never structurally match it).
    flatten_reference = bool(options.get("flatten_reference", False))
    flatten_layout = bool(options.get("flatten_layout", False))
    # Issue #1205: echoed verbatim (not resolved against `request_dir`), the
    # same convention `layout`/`reference` are echoed under -- so the
    # response's `options` block round-trips back into a request document
    # exactly as it was given. `null` for every `"klayout"`-engine run (and
    # for a netgen run that let netgen use its own trivial default setup).
    netgen_setup_echo = options.get("netgen_setup")

    import klayout.db as kdb

    (
        layout_netlist,
        layout_echo,
        layout_hash_source,
        extracted_netlist_path,
        layout_net_label_positions,
    ) = _resolve_layout(
        layout_spec,
        request_dir,
        keep_extracted,
        combine_devices_enabled,
        deck_options,
    )
    # Inspect original scoped nets before any transform can discard a power
    # island or flatten independent child definitions into a shared scope.
    supply_findings = supply_fragmentation_findings(
        _select_circuit(layout_netlist, layout_spec.get("top"), "layout"),
        supply_nets,
        layout_net_label_positions,
    )

    # Issue #2027: which *kind* of artifact `layout_hash_source` is, for
    # `provenance.input.role`. `_resolve_layout` returns the original
    # GDS/OASIS stream for the `layout.file` (inline extraction) shape and
    # the caller-supplied SPICE file for the pre-extracted `layout.netlist`
    # shape -- byte-identical to what `klt drc` hashes in the first case, a
    # netlist in the second. `klt signoff`'s provenance cross-check compares
    # `input.content_hash` only within one role, so declaring this is what
    # stops a pre-extracted LVS report from "disagreeing" with the DRC
    # report of the very same design. `_resolve_layout` has already rejected
    # a spec with both keys (or neither), so this mirrors its own branch.
    layout_input_role = (
        INPUT_ROLE_NETLIST if "netlist" in layout_spec else INPUT_ROLE_LAYOUT
    )

    flatten_warnings: list[dict[str, Any]] = []
    # Issue #1622/#2244: disclosure for `_prune_power_only_circuits`, filled
    # in once `reference_netlist` is available below (only ever nonempty for
    # `reference.form: "gate-level-verilog"`).
    power_only_pruning_warnings: list[dict[str, Any]] = []
    # Issue #2021: disclosure for `_apply_gate_level_port_aliases`, filled in
    # once `reference_netlist` is available below (only ever nonempty for
    # `reference.form: "gate-level-verilog"`, and then only when the
    # reference declares an `assign`-aliased port).
    gate_level_port_alias_warnings: list[dict[str, Any]] = []
    # Issue #1552: `options.combine_devices_per_circuit`'s per-macro combine
    # choices must run *before* either side's optional structural flatten
    # (`options.flatten_layout`/`options.flatten_reference`, issue #1085)
    # collapses the very circuit boundaries this option scopes against --
    # see `_combine_circuit_devices_safely`'s docstring. Applies only to a
    # `layout.netlist` (pre-extracted, possibly hierarchical) shape's own
    # circuits; a `layout.file` inline extraction always produces a single
    # flat circuit (no boundary yet to scope against -- see #1085), so this
    # is a no-op there regardless of what the map names.
    combine_per_circuit_warnings: list[dict[str, Any]] = []
    # Issue #1557: the whole-netlist `Netlist.dup()` snapshot taken *before*
    # any per-circuit combining runs on this side -- the pre-combine input
    # `_correct_combine_parameters` needs below, mirroring the
    # whole-netlist `combine_devices` path's own `layout_snapshot`/
    # `reference_snapshot` (taken the same way, for the same reason, further
    # down). `layout_combined_circuits` collects the subset of
    # `layout_targets` that combined *cleanly* (see
    # `_combine_circuit_devices_safely`'s return contract) -- both are
    # populated below only when `combine_devices_per_circuit` is set, and
    # consumed once both sides have finished combining (after the net purge
    # below) and again once `layout_deck` is resolved further down (the
    # deferred resistor `fixed_offset_ohm` correction, issue #559/#585).
    layout_pre_combine_snapshot: kdb.Netlist | None = None
    layout_combined_circuits: list[str] = []
    if combine_devices_per_circuit is not None:
        layout_targets, layout_unmatched = _resolve_combine_devices_per_circuit_targets(
            combine_devices_per_circuit, layout_netlist
        )
        layout_pre_combine_snapshot = layout_netlist.dup()
        layout_warnings, layout_combined_circuits = _combine_circuit_devices_safely(
            layout_netlist,
            "layout",
            layout_targets,
            combine_devices_max_attempts,
        )
        combine_per_circuit_warnings.extend(layout_warnings)
        combine_per_circuit_warnings.extend(
            _unmatched_combine_devices_per_circuit_warnings(layout_unmatched, "layout")
        )

        # Issue #1497/#1557/#2374: the same post-combine parameter-
        # conservation correction the whole-netlist `combine_devices` path
        # applies (further down, gated on `combine_devices_enabled`), scoped
        # here to just the circuit(s) this side actually combined *cleanly*
        # -- a circuit left uncombined (named `False`, unmatched, or not
        # named at all) or whose combine was reported
        # `device.combine_incomplete` is never touched. Applied here,
        # immediately, rather than deferred alongside the reference side's
        # own correction below (where it would be more symmetric-looking):
        # `flatten_layout` runs immediately after this block, and once it
        # collapses this side's circuit boundaries away,
        # `_combine_parameter_expectations`'s per-circuit-name grouping key
        # can no longer line up a post-combine device's
        # (now-`layout_spec.get("top")`-named) circuit against the
        # pre-combine snapshot's original circuit name -- silently matching
        # nothing and leaving a corrupted parameter uncorrected (caught by
        # this fix's own end-to-end test after initially placing this call
        # after `flatten_layout`, alongside the reference side's).
        if layout_combined_circuits:
            layout_parameter_correction = _correct_combine_parameters(
                layout_pre_combine_snapshot,
                layout_netlist,
                "layout",
                circuit_names=layout_combined_circuits,
            )
            if layout_parameter_correction is not None:
                combine_per_circuit_warnings.append(layout_parameter_correction)

    if flatten_layout:
        layout_flatten_warning = _flatten_netlist_safely(layout_netlist, "layout")
        if layout_flatten_warning is not None:
            flatten_warnings.append(layout_flatten_warning)

    reference_netlist_path = _require_path(
        reference_spec, "netlist", "reference", request_dir
    )
    reference_echo = reference_spec["netlist"]
    reference_form = reference_spec.get("form", "plain-element")
    if reference_form not in _REFERENCE_FORMS:
        raise LvsError(
            f"request.reference.form must be one of "
            f"{', '.join(repr(f) for f in _REFERENCE_FORMS)}; got "
            f"{reference_form!r}"
        )
    # Issue #1952: the `power_connectivity` report block, replaced below for
    # a `gate-level-verilog` reference. Every other form's reference is
    # arbitrary SPICE that carries its own power nets and pins, so the
    # comparer already checks them as ordinary connectivity -- there is no
    # signal-only gap for this check to fill, and nothing licenses calling
    # any pin name a power pin (the same restriction that scopes
    # `_prune_power_only_circuits`). Emitted as an explicit
    # `"unchecked"` carrying a reason rather than omitted, so the question
    # "did this run verify power connectivity?" is answerable from any
    # `klt lvs` report on its own.
    power_connectivity: dict[str, Any] = _power_connectivity_unchecked(
        f"reference.form is {reference_form!r}, whose reference netlist "
        "carries its own power/ground pins and nets -- they take part in "
        "the ordinary compare, so this check (which exists to cover the "
        "signal-only 'gate-level-verilog' form) does not apply"
    )
    reference_device_map = reference_spec.get("device_map")
    if reference_device_map is not None and not isinstance(reference_device_map, dict):
        raise LvsError("request.reference.device_map must be a JSON object")
    reference_device_bulk = reference_spec.get("device_bulk")
    if reference_device_bulk is not None and not isinstance(
        reference_device_bulk, dict
    ):
        raise LvsError(
            "request.reference.device_bulk must be a JSON object mapping a "
            "device-class/model name to the reference net its implicit bulk "
            "terminal carries"
        )
    if reference_form == "gate-level-verilog" and not reference_spec.get("library"):
        raise LvsError(
            "request.reference.library is required with "
            'form: "gate-level-verilog" -- it names the standard-cell '
            "library (e.g. 'sky130_fd_sc_hd', 'gf180mcu_fd_sc_mcu9t5v0') "
            "whose own .spice/.cdl subcircuit declarations resolve each "
            "instantiated cell's pin order (see docs/cli/lvs.md, \"Netlist "
            'form")'
        )
    # Issue #1622: resolved here rather than inside `_read_reference_netlist`
    # so the *same* mapping serves both consumers with one read of the
    # library file -- the conversion's own `pin_order_lookup` and, below,
    # the power-pin universe `_prune_power_only_circuits` needs.
    reference_pin_orders: dict[str, list[str]] | None = None
    # Issue #1901: the PDK `_resolve_gate_level_pin_orders` resolves
    # internally to read the library's pin-order source -- reused here (not
    # re-resolved) purely so `provenance.pdk` can record it below, matching
    # `klt extract`/`klt sta`/`klt place-and-route`. Stays `None` for every
    # other `reference.form`, which resolves no PDK at all.
    reference_pdk_info: dict[str, Any] | None = None
    if reference_form == "gate-level-verilog":
        reference_pin_orders, reference_pdk_info = _resolve_gate_level_pin_orders(
            reference_spec.get("library"),
            reference_spec.get("pdk"),
            reference_spec.get("pdk_root"),
        )
    # Issue #1907: populated by the `form: "subckt-call"` conversion with every
    # resistor/capacitor device class it wrote the literal `0` placeholder
    # value onto -- consumed by `_apply_reference_placeholder_values` below,
    # empty for every other reference form.
    reference_placeholder_classes: dict[str, str] = {}
    # Issue #2021: populated by `_read_reference_netlist` only for
    # `form="gate-level-verilog"` -- `{<module name>: {<port>: <canonical
    # net>}}` for every declared port whose only Verilog-level connection is
    # a plain `assign` alias. Consumed by `_apply_gate_level_port_aliases`
    # right below, before anything else reads `reference_netlist`.
    reference_gate_level_port_aliases: dict[str, dict[str, str]] = {}
    # Issue #2136: the library-derived power-pin universe plus the layout
    # nets those pins land on, used below to mark a `net_correspondence[]`
    # entry that pairs a layout supply net with a reference net that is not
    # itself a supply pin. Derived only in the `gate-level-verilog` branch
    # (the one reference form whose netlist carries no power/ground pins at
    # all), and left `None` everywhere else -- which keeps every other
    # form's `net_correspondence[]` byte-identical to before.
    supply_universe: _SupplyPinUniverse | None = None
    reference_netlist = _read_reference_netlist(
        reference_netlist_path,
        form=reference_form,
        deck=reference_spec.get("deck"),
        device_map=reference_device_map,
        library=reference_spec.get("library"),
        pdk_variant=reference_spec.get("pdk"),
        pdk_root=reference_spec.get("pdk_root"),
        pin_orders=reference_pin_orders,
        placeholder_value_classes=reference_placeholder_classes,
        gate_level_port_aliases=reference_gate_level_port_aliases,
    )

    if reference_form == "gate-level-verilog":
        # Issue #2021: joins every `assign`-aliased reference port's net onto
        # its canonical target's net -- run first, before anything else below
        # reads `reference_netlist`'s topology (the power connectivity report,
        # the power-only prune, and the comparer itself all need the
        # corrected shape).
        gate_level_port_alias_warnings.extend(
            _apply_gate_level_port_aliases(
                reference_gate_level_port_aliases, reference_netlist
            )
        )
        # Issue #2136: derived here, before the power-only prune below, for
        # the same reason `_power_pin_connections` must run before it -- the
        # prune removes exactly the filler/tap instances whose power pins are
        # part of the evidence that the layout's rails are supply nets. Not
        # gated on `options.power_connectivity`: this is a disclosure about
        # what the *compare* did or did not verify, which a caller who turned
        # the separate power-connectivity check off needs at least as much.
        supply_universe = _supply_pin_universe(
            layout_netlist, reference_netlist, reference_pin_orders
        )
        # Issue #1952: the power/ground half of the compare, run *before*
        # the power-only prune below -- see `_power_pin_connections`'s
        # docstring for why that order is load-bearing (the prune removes
        # exactly the filler/tap instances whose power connectivity is the
        # only thing about them any check could verify).
        if power_connectivity_enabled:
            power_connectivity = _power_connectivity_report(
                layout_netlist,
                reference_netlist,
                reference_pin_orders,
                expected_nets=power_connectivity_expected_nets,
            )
        else:
            power_connectivity = _power_connectivity_unchecked(
                "disabled by options.power_connectivity: false"
            )
        # Issue #1622 (layout side) / #2244 (reference side, symmetric): a
        # `klt place-and-route` `verilog_path` reference never instantiates a
        # power-only cell (filler/tap) at all -- but a DEF-derived reference
        # Verilog (any other `write_verilog` consumer) can, with an empty
        # connection list per instance. See
        # `_prune_power_only_circuits`'s docstring for why both sides must
        # be pruned before `compare()` runs, rather than only suppress the
        # resulting mismatch report after the fact. Run right after
        # `reference_netlist` is read (the earliest point the
        # library-derived power-pin universe is available), and before the
        # `combine_devices_per_circuit`/flatten/`combine_devices` steps
        # below, none of which have anything to do with a power-only
        # circuit's own (device-less) content.
        power_only_pruning_warning = _prune_power_only_circuits(
            layout_netlist,
            reference_netlist,
            reference_pin_orders,
            layout_keep_name=layout_spec.get("top"),
            reference_keep_name=reference_spec.get("top"),
        )
        if power_only_pruning_warning is not None:
            power_only_pruning_warnings.append(power_only_pruning_warning)

    if combine_devices_per_circuit is not None:
        reference_targets, reference_unmatched = (
            _resolve_combine_devices_per_circuit_targets(
                combine_devices_per_circuit, reference_netlist
            )
        )
        # Issue #1557: mirrors `layout_pre_combine_snapshot`/
        # `layout_combined_circuits` above, taken here (immediately before
        # this side's own combine call) rather than up where the layout
        # side's snapshot was taken, so it reflects *this* netlist's own
        # pre-combine state.
        reference_pre_combine_snapshot = reference_netlist.dup()
        (
            reference_warnings,
            reference_combined_circuits,
        ) = _combine_circuit_devices_safely(
            reference_netlist,
            "reference",
            reference_targets,
            combine_devices_max_attempts,
        )
        combine_per_circuit_warnings.extend(reference_warnings)
        combine_per_circuit_warnings.extend(
            _unmatched_combine_devices_per_circuit_warnings(
                reference_unmatched, "reference"
            )
        )
        # Issue #500, scoped down from the whole-netlist combine's own purge
        # (see `_purge_emptied_nets`'s docstring): a combined circuit's own
        # interior nodes left with nothing attached after the fold are
        # cleaned up on both sides here, once, after both sides' per-circuit
        # combining has finished -- not per call above -- so a circuit
        # touched only on one side does not leave the other side's
        # `counts.nets.*` looking stale by comparison.
        _purge_emptied_nets(layout_netlist)
        _purge_emptied_nets(reference_netlist)

        # Issue #1497/#1557: the reference side's own capacitor `C`
        # sum-conservation correction -- the layout side's equivalent
        # already ran, further up, before `flatten_layout` could collapse
        # its circuit boundaries (see the comment there for why the two
        # sides cannot share this same call site). The reference side has
        # no such ordering hazard here: `flatten_reference` does not run
        # until after this whole block, so this is still scoped correctly.
        if reference_combined_circuits:
            reference_parameter_correction = _correct_combine_parameters(
                reference_pre_combine_snapshot,
                reference_netlist,
                "reference",
                circuit_names=reference_combined_circuits,
            )
            if reference_parameter_correction is not None:
                combine_per_circuit_warnings.append(reference_parameter_correction)

    if flatten_reference:
        reference_flatten_warning = _flatten_netlist_safely(
            reference_netlist, "reference"
        )
        if reference_flatten_warning is not None:
            flatten_warnings.append(reference_flatten_warning)

    layout_circuit = _select_circuit(layout_netlist, layout_spec.get("top"), "layout")
    reference_circuit = _select_circuit(
        reference_netlist, reference_spec.get("top"), "reference"
    )

    _prune_extra_top_circuits(layout_netlist, layout_circuit)
    _prune_extra_top_circuits(reference_netlist, reference_circuit)

    # What the layout-side deck can structurally recognise
    # (`ExtractionDeck.device_classes`, issue #221) -- `null` when no
    # `layout.deck` was supplied. `layout.deck` is meaningful for *both*
    # layout shapes: it is required for `layout.file` (it drives inline
    # extraction), and it is optional-but-honored for the pre-extracted
    # `layout.netlist` shape, where it does not trigger extraction but still
    # names the deck whose resistor `fixed_offset_ohm` the `combine_devices`
    # block below applies post-combine (issue #559/#585). Already validated by
    # `_resolve_layout` above (an unknown deck name or an invalid
    # `deck_options` entry would have raised `LvsError` before reaching this
    # point) *for the `layout.file` shape only* -- re-fetching it here with
    # the same `deck_options` cannot itself raise in that case. For the
    # pre-extracted `layout.netlist` shape, `_resolve_layout` never touches
    # the deck at all (no extraction happens), so this is the first -- and
    # only -- place a bad `deck` name or `deck_options` entry on that shape
    # surfaces; wrapped below the same way `extract_netlist_from_layout`
    # wraps it (issue #600). Resolved here (rather than just before
    # `device_classes` further down) so the `combine_devices` block below can
    # also use it to apply the deferred resistor `fixed_offset_ohm`
    # correction post-combine (issue #559).
    layout_deck_name = layout_spec.get("deck")
    try:
        layout_deck = (
            get_extraction_deck(layout_deck_name, deck_options)
            if layout_deck_name
            else None
        )
    except (UnknownExtractionDeckError, InvalidDeckOptionError) as exc:
        raise LvsError(str(exc)) from exc

    # Issue #2421: a pre-flight check on the *reference* netlist against the
    # deck's option-selected, shared-geometry device families (a
    # `ResistorDevice`/`CapacitorDevice` entry carrying `flavour_option` plus
    # two or more `flavours`, e.g. gf180mcu's `poly_res`). A reference that
    # names more than one class of such a family cannot match under *any*
    # `deck_options` value -- so the finding is collected here, from the
    # reference netlist as given and independent of which value this run
    # selected, and appended to `mismatches[]` further down (the same
    # collect-early/append-late shape `flatten_warnings` above uses). Runs for
    # both engines and both layout shapes: all it needs is a resolved
    # `layout.deck` and the reference netlist.
    family_findings = _device_class_family_findings(reference_netlist, layout_deck)

    if combine_devices_per_circuit is not None and layout_deck is not None:
        # Issue #559/#585/#1557: the same deferred resistor
        # `fixed_offset_ohm` correction the whole-netlist `combine_devices`
        # block below applies (mutually exclusive with
        # `combine_devices_per_circuit`, so only one of the two ever runs
        # for a given request) -- reached only once `layout_deck` is
        # resolved (immediately above), which is *after*
        # `_combine_circuit_devices_safely` already folded each targeted
        # circuit's devices further up. Applied netlist-wide, exactly like
        # the whole-netlist block does, rather than restricted to
        # `layout_combined_circuits`: the deferral this correction reverses
        # (`--defer-resistor-fixed-offset` at pre-extraction time, issue
        # #585) was applied once, globally, to the whole pre-extracted
        # netlist -- not per named circuit -- so a circuit
        # `combine_devices_per_circuit` left uncombined still carries
        # raw-body-only resistor primitives that need this same offset
        # added exactly once each, the same as a degraded
        # `Netlist.combine_devices()` run's leftover individual devices do
        # in the whole-netlist block below (see that block's own comment on
        # why it is "still applied after a symmetric degrade").
        apply_resistor_fixed_offset_corrections(layout_netlist, layout_deck)

    combine_warnings: list[dict[str, Any]] = []
    combine_incomplete = False
    if combine_devices_enabled:
        # Opt-in (issue #261): `Netlist.combine_devices()` merges devices
        # that a device class's own `combine_devices` logic recognises as
        # combinable (e.g. parallel/series MOSFETs with matching gate/S/D/B
        # connectivity) -- exactly the folded/multi-finger and split/
        # interleaved layout constructions standard to analog matching and
        # drive-strength splitting. It is a whole-`Netlist` method (not
        # scoped to one `Circuit`), so it is applied once per netlist here.
        # Applied symmetrically to *both* sides so a reference netlist that
        # already lumps a device is not penalized relative to one that
        # doesn't (and vice versa). Left opt-in, not unconditional: it would
        # also collapse genuinely-distinct parallel devices (e.g. a DAC
        # array's intentionally-separate legs) some callers want reported
        # individually -- see docs/cli/lvs.md's `options.combine_devices`
        # entry. Run after pruning (so it only ever touches the selected top
        # circuit's hierarchy) and before the comparer is constructed, so
        # every subsequent step (`same_circuits`, hints, `compare()`) sees
        # the already-combined device set.
        #
        # Wrapped per netlist (issue #466): KLayout's own `combine_devices()`
        # can raise an unhandled internal-consistency `RuntimeError` on a
        # partial-match device group -- N real (matching-relevant) instances
        # plus M dummy instances that all share two of three terminals, but
        # only the N real ones also share the third (e.g. a matched
        # bipolar/MOS array's flanking dummies). That is a `klayout.db`
        # behavior this module merely surfaces; letting it propagate as a
        # bare traceback would violate this module's own JSON-envelope
        # contract. `_combine_devices_safely` degrades gracefully instead:
        # whatever `combine_devices()` already merged before hitting the
        # error stays merged, the rest are left as individual devices, and a
        # `device.combine_incomplete` warning is added to `mismatches[]`.
        #
        # Issue #1185: `_combine_devices_safely` retries against independent
        # `Netlist.dup()` copies to counter `combine_devices()`'s run-to-run
        # nondeterminism (see its docstring and `_COMBINE_DEVICES_MAX_
        # ATTEMPTS`'s), adopting a clean copy's result via `Netlist.assign()`
        # the moment one succeeds. `assign()` replaces the netlist's circuits
        # in place, which invalidates any `kdb.Circuit` object a caller took
        # from it beforehand -- so `layout_circuit`/`reference_circuit`
        # (selected above, before this block, at line ~515) must be
        # re-fetched by name immediately after, before anything below uses
        # them again. Captured here, before the calls that may invalidate
        # them.
        layout_circuit_name = layout_circuit.name
        reference_circuit_name = reference_circuit.name

        # Issue #1370: a list-valued `options.combine_devices` restricts
        # combining to the named device classes. Validated against *both*
        # netlists' actual device classes first, so naming a class that
        # exists on neither side is a clean request error rather than a
        # silent no-op that looks like "combining ran and found nothing".
        if combine_device_classes is not None:
            _validate_combine_device_classes(
                combine_device_classes,
                {"layout": layout_netlist, "reference": reference_netlist},
            )

        # Issue #1370, symmetric degrade: snapshot both sides *before* either
        # combine runs. When a side exhausts its retry budget the compare the
        # caller asked for cannot be performed on that side, and the
        # status-quo behaviour (leave that side partially folded, leave the
        # other side fully folded) compares a partially-folded netlist against
        # a fully-folded one -- an unfair comparison whose every downstream
        # `device.property`/`device.unmatched` finding is cascade, not a real
        # design difference. Restoring *both* snapshots instead puts the two
        # sides back on equal terms (exactly the state
        # `options.combine_devices: false` would have produced), and the
        # verdict is reported as `status: "inconclusive"` further down rather
        # than as a design mismatch. Only paid for when combining is opted in.
        layout_snapshot = layout_netlist.dup()
        reference_snapshot = reference_netlist.dup()

        layout_warning = _combine_devices_safely(
            layout_netlist,
            "layout",
            combine_devices_max_attempts,
            device_classes=combine_device_classes,
        )
        if layout_warning is not None:
            combine_warnings.append(layout_warning)
        reference_warning = _combine_devices_safely(
            reference_netlist,
            "reference",
            combine_devices_max_attempts,
            device_classes=combine_device_classes,
        )
        if reference_warning is not None:
            combine_warnings.append(reference_warning)

        combine_incomplete = bool(combine_warnings)
        if combine_incomplete:
            # Asymmetric failure (one side combined cleanly, the other did
            # not) is handled by the same restore as the both-sides case: the
            # side that *did* combine is rolled back too, so neither side
            # carries a fold the other could not.
            layout_netlist.assign(layout_snapshot)
            reference_netlist.assign(reference_snapshot)

        layout_circuit = layout_netlist.circuit_by_name(layout_circuit_name)
        reference_circuit = reference_netlist.circuit_by_name(reference_circuit_name)

        # combine_devices() folds matched device arrays but leaves the
        # interior nets it emptied (0 terminals, 0 pins) behind in the
        # circuit -- e.g. the N-1 interior nodes of a series string it
        # collapsed into a single device. Left in place they inflate
        # counts.nets.* (computed off each_net() below) and surface in
        # mismatches[] as spurious net.unmatched findings no caller can act
        # on (issue #500). Purge them symmetrically on both sides -- mirroring
        # the symmetric combine above and running only when combine actually
        # ran -- so counts and mismatches reflect the post-combine topology,
        # not combine_devices()'s internal bookkeeping. Scoped to genuinely
        # empty nets only, so a real (if unused) top-level pin's net is never
        # dropped and counts.pins.* is unaffected.
        #
        # Skipped entirely after a symmetric degrade (issue #1370): nothing
        # was folded on either side, so there is no combine-emptied net to
        # purge, and skipping keeps the degraded run's `counts.nets.*`
        # byte-identical to what `options.combine_devices: false` reports for
        # the same inputs -- which is the whole point of the degrade.
        if not combine_incomplete:
            _purge_emptied_nets(layout_netlist)
            _purge_emptied_nets(reference_netlist)

            # Issues #1497/#2374: KLayout's own `combine_devices()` can leave
            # a combined device's parameters inconsistent with the parallel
            # group it folded -- silently, with no exception and no
            # `device.combine_incomplete` warning (that category only covers
            # the unrelated #1185/#466 `RuntimeError` case above): a
            # capacitor's `C` left at one instance's value (#1497), or a
            # bipolar array's whole accumulation inverted (#2374). Checked
            # and corrected in place against each side's own pre-combine
            # snapshot (already taken, for the #1370 symmetric-degrade
            # rollback, before either combine call ran), appended to
            # `combine_warnings` -- but only *after* `combine_incomplete` was
            # computed above, so a correction here never triggers that
            # rollback: the value is already fixed, there is nothing left to
            # roll back for.
            layout_parameter_correction = _correct_combine_parameters(
                layout_snapshot, layout_netlist, "layout"
            )
            if layout_parameter_correction is not None:
                combine_warnings.append(layout_parameter_correction)
            reference_parameter_correction = _correct_combine_parameters(
                reference_snapshot, reference_netlist, "reference"
            )
            if reference_parameter_correction is not None:
                combine_warnings.append(reference_parameter_correction)

        if layout_deck is not None:
            # Issue #559/#585: apply the deferred resistor `fixed_offset_ohm`
            # correction here, after `combine_devices()` has folded
            # series-connected drawn primitives into one device object.
            # Because `apply_resistor_fixed_offset_corrections` walks whatever
            # devices exist in `layout_netlist` right now, this adds the fixed
            # offset exactly once per surviving (possibly-folded) logical
            # device -- never once per original primitive -- fixing the
            # over-count KLayout's native series fold otherwise produces by
            # summing each primitive's already-corrected `R`.
            #
            # Reached by *both* layout shapes whenever a `layout.deck` is
            # present (issue #585):
            #   * `layout.file` (inline extraction): `_resolve_layout` deferred
            #     the correction by passing `apply_resistor_fixed_offset=False`
            #     to inline extraction, so the primitives here carry only their
            #     raw body `R` -- the offset is added once, here, post-combine.
            #   * `layout.netlist` (pre-extracted): honored only when the
            #     supplied SPICE was itself extracted with the correction
            #     *deferred* (`run_extract(..., apply_resistor_fixed_offset=
            #     False)`). Then the pre-extracted primitives likewise carry
            #     only raw body `R`, and this call is what applies the offset
            #     once per post-combine device. (A netlist extracted the
            #     default way already has the offset baked into each primitive;
            #     feeding that through `combine_devices` is the caller's own
            #     double-count to avoid -- there is no way to selectively
            #     un-sum an already-applied per-primitive offset here.)
            #
            # The device-class-name lookup inside
            # `apply_resistor_fixed_offset_corrections` is case-insensitive, so
            # it matches both the in-process (`res_high_po`) and
            # SpiceReader-uppercased (`RES_HIGH_PO`) class names identically
            # (issue #585). The reference netlist never receives this
            # correction: it is a layout-deck-specific geometric correction,
            # not a property of the schematic reference.
            #
            # Still applied after a symmetric degrade (issue #1370), unlike
            # the net purge above: a deferred extraction wrote raw body `R`
            # values that are simply *wrong* until the offset is added, and
            # with nothing folded this adds it exactly once per drawn
            # primitive -- which is precisely what a non-deferred extraction
            # would have produced. Skipping it here would leave the degraded
            # run reporting under-valued resistances.
            apply_resistor_fixed_offset_corrections(layout_netlist, layout_deck)

    bulk_warnings: list[dict[str, Any]] = []
    placeholder_warnings: list[dict[str, Any]] = []
    tolerance_warnings: list[dict[str, Any]] = []
    compare_parameter_warnings: list[dict[str, Any]] = []
    # Issue #1998: populated only for `engine == "klayout"` -- the `netgen`
    # branch below rejects any `request.hints` outright (no equivalent hook
    # in that engine's scope), so this stays empty for every netgen run.
    equivalent_pins_applied: dict[str, list[list[str]]] = {}

    if engine == "klayout":
        # Issue #506: normalise the reference side's device classes up to the
        # layout side's terminal list *before* the comparer is constructed, so
        # a deck's bulk-terminal device flavour (e.g. a `bulk_to_substrate`
        # resistor's three-terminal `RES_X`) can be compared against a
        # schematic-derived reference that does not model that terminal at
        # all. Runs after the `combine_devices()` step above so combining
        # still sees each side's own, unmodified device classes (the
        # status-quo behaviour), and returns the `severity: "warning"`
        # disclosure entries appended to `mismatches[]` further down.
        bulk_warnings = _apply_reference_device_bulk(
            reference_device_bulk,
            layout_netlist,
            reference_netlist,
        )

        # Issue #1907: same placement rationale as `_apply_reference_device_bulk`
        # just above -- a `form: "subckt-call"` conversion's placeholder `0`
        # value has to be taken out of the comparison *before* the comparer is
        # constructed, since `NetlistComparer` reads each device class's
        # `equal_parameters` when it builds its device-equivalence seeding.
        # Runs after `combine_devices()` for the same reason too (combining
        # still sees each side's unmodified classes), and returns the
        # `severity: "warning"` disclosure entries appended further down.
        placeholder_warnings = _apply_reference_placeholder_values(
            reference_placeholder_classes,
            layout_netlist,
            reference_netlist,
        )

        # Issue #1928: same placement rationale once more -- `enable_parameter`
        # has to run before the comparer is constructed for the same reason
        # `equal_parameters` does just above, and after `combine_devices()`
        # so combining still sees each side's unmodified classes. Returns
        # the `severity: "warning"` disclosure entries appended further down.
        compare_parameter_warnings = _apply_compare_parameters(
            compare_parameters,
            layout_netlist,
            reference_netlist,
        )

        logger = _make_compare_logger(layout_circuit, reference_circuit)
        comparer = kdb.NetlistComparer(logger)
        # `_select_circuit` + `_prune_extra_top_circuits` above already guarantee
        # `layout_circuit`/`reference_circuit` are each netlist's *sole*
        # remaining top circuit, so these two are unambiguously the pair the
        # request declared -- pin that pairing explicitly instead of leaving it
        # to `NetlistComparer`'s default name-based matching, which silently
        # degrades to a generic "could not be matched to a counterpart" finding
        # on both sides whenever `layout.top`/`reference.top` name different
        # circuits (issue #231). Safe unconditionally: there is no other
        # circuit either one could be confused with post-pruning.
        comparer.same_circuits(layout_circuit, reference_circuit)
        same_nets_hints, equivalent_pins_applied = _apply_hints(
            comparer, request.get("hints") or {}, layout_circuit, reference_circuit
        )

        # `logger` is already bound via the `NetlistComparer(logger)` constructor
        # above, so the 2-arg overload is used here (not the 3-arg one, which
        # would pass a second, redundant logger reference).
        compare_result = comparer.compare(layout_netlist, reference_netlist)

        if not compare_result and parameter_tolerance is not None:
            # Issue #589: `options.parameter_tolerance` is opted in and the
            # engine said "mismatch". Snap every reference-side parameter whose
            # difference from its layout-side counterpart is within the
            # requested relative tolerance, then run a *second, real*
            # `compare()` -- the only thing that can legitimately move
            # `status`, since this module never re-derives the verdict from its
            # own event classification (see the module docstring). Skipped
            # entirely when the option is omitted, so the default path is
            # byte-identical to before.
            for _pass in range(_MAX_TOLERANCE_PASSES):
                snaps = _collect_tolerance_snaps(logger, parameter_tolerance)
                if not snaps:
                    break
                _apply_tolerance_snaps(reference_netlist, snaps)
                tolerance_warnings.extend(
                    _tolerance_disclosure(snap, parameter_tolerance) for snap in snaps
                )
                logger = _make_compare_logger(layout_circuit, reference_circuit)
                comparer = kdb.NetlistComparer(logger)
                comparer.same_circuits(layout_circuit, reference_circuit)
                # Re-declared on the fresh comparer: `same_nets`/
                # `equivalent_pins` are comparer state, not netlist state, so
                # the second pass would otherwise silently drop the caller's
                # hints. Re-validation cannot raise here (the first call above
                # already accepted every hint against these same circuits).
                same_nets_hints, equivalent_pins_applied = _apply_hints(
                    comparer,
                    request.get("hints") or {},
                    layout_circuit,
                    reference_circuit,
                )
                compare_result = comparer.compare(layout_netlist, reference_netlist)
                if compare_result:
                    break

        mismatches = _build_mismatches(
            logger,
            layout_netlist,
            reference_netlist,
            same_nets_hints=same_nets_hints,
        )
        if not compare_result and not mismatches:
            # Safety net for the correctness invariant this module's docstring
            # states: `compare()` is always authoritative. If the engine says
            # "mismatch" but this module's own event classification produced
            # nothing (a gap in event coverage, not a clean run), never let the
            # response silently look like a match -- report a generic, honest
            # finding instead of dropping the verdict.
            #
            # Built through `_mismatch()` like every other classification site
            # (issue #1132 review): a hand-rolled dict literal here silently
            # drifts out of the entry shape whenever a field is added -- it
            # went missing `circuit`/`instance`/`subcircuit` entirely (absent
            # keys, not `null`), breaking the "every entry carries the key,
            # never omitted" invariant `_mismatch`'s docstring and
            # docs/cli/lvs.md's field table both state.
            mismatches = [
                _mismatch(
                    CATEGORY_TOPOLOGY,
                    "error",
                    "netlists do not match (no further detail available "
                    "from the comparer's event log)",
                    "both",
                )
            ]
        status = "match" if compare_result else "mismatch"
        engine_version = _engine_version()
        # Issue #2373: the `klayout` engine launches no subprocess, so the
        # field is present-and-null here rather than omitted -- the same
        # always-present-but-nullable convention the rest of `environment`
        # follows.
        netgen_binary = None
        net_correspondence = _build_net_correspondence(logger, supply_universe)
        counts = {
            "nets": {
                "layout": sum(1 for _ in layout_circuit.each_net()),
                "reference": sum(1 for _ in reference_circuit.each_net()),
                "matched": logger.matched_nets,
            },
            "devices": {
                "layout": sum(1 for _ in layout_circuit.each_device()),
                "reference": sum(1 for _ in reference_circuit.each_device()),
                "matched": logger.matched_devices,
            },
            "pins": {
                "layout": layout_circuit.pin_count(),
                "reference": reference_circuit.pin_count(),
                "matched": logger.matched_pins,
            },
        }
    else:
        # `engine == "netgen"` -- the only other `SUPPORTED_ENGINES` member.
        # Netlist-vs-netlist only (see this module's docstring): no magic
        # extraction backend, no per-net/per-device `hints` hook (netgen has
        # no equivalent to `same_nets`/`equivalent_pins` in this scope), and
        # -- unlike the `klayout` engine's in-process compare -- an external
        # subprocess whose own exit code is not trustworthy on its own (see
        # `_run_netgen_lvs`).
        if request.get("hints"):
            raise LvsError(
                "request.hints (same_nets/equivalent_pins) is only supported "
                "for engine 'klayout' -- the netgen engine has no equivalent "
                "hook in this issue's netlist-vs-netlist scope (see "
                'docs/cli/lvs.md, "Engine")'
            )
        if reference_device_bulk:
            # Issue #506: same boundary as `hints` above -- the reconciliation
            # is a `klayout.db`-side device-class normalisation applied to the
            # in-memory reference netlist, and netgen reads its own SPICE
            # files through its own device-class model.
            raise LvsError(
                "request.reference.device_bulk is only supported for engine "
                "'klayout' -- the netgen engine compares SPICE files through "
                "its own device model and has no equivalent hook (see "
                'docs/cli/lvs.md, "Engine")'
            )
        if parameter_tolerance is not None:
            # Issue #589: same boundary, for a concrete reason rather than an
            # arbitrary one. netgen's own per-property tolerances are declared
            # in its setup file as *absolute* per-device-class values
            # (`property {-circuit1 <class>} tolerance <name> <value>`), so a
            # single engine-neutral *relative* tolerance has no faithful
            # translation into one -- deriving per-class absolute values would
            # require this module to invent a device-by-device conversion the
            # caller never asked for. Erroring keeps an opted-in tolerance from
            # being silently ignored (the one outcome worse than not supporting
            # it), and `options.netgen_setup` already exposes netgen's native
            # tolerance vocabulary for callers on this engine.
            raise LvsError(
                "options.parameter_tolerance is only supported for engine "
                "'klayout' -- express a netgen-side tolerance with that "
                "engine's own setup file instead (options.netgen_setup, whose "
                "'property ... tolerance' entries are absolute per-device-class "
                'values, not a single relative one; see docs/cli/lvs.md, "Engine")'
            )
        if compare_parameters:
            # Issue #1928: same boundary again -- `DeviceClass.enable_parameter`
            # is a `klayout.db`-side hook this module applies to the in-process
            # `NetlistComparer`'s own device classes, and netgen has no
            # equivalent per-parameter compare-scoping hook of its own.
            raise LvsError(
                "options.compare_parameters is only supported for engine "
                "'klayout' -- the netgen engine has no equivalent "
                "per-parameter compare-scoping hook (see docs/cli/lvs.md, "
                '"Engine")'
            )
        setup_file = _resolve_netgen_setup(options, request_dir)
        timeout_s = float(options.get("netgen_timeout_s", _NETGEN_DEFAULT_TIMEOUT_S))
        # Issue #2373: resolve *which* netgen binary to run before launching
        # it (`options.netgen_binary` > `$KLT_NETGEN_BINARY` > `netgen` >
        # `netgen-lvs`), rather than hardcoding the name `netgen` -- the
        # resolved path is both what gets spawned and what
        # `environment.netgen_binary` records below.
        netgen_binary = _resolve_netgen_binary(options, request_dir)
        status, mismatches, engine_version = _run_netgen_lvs(
            layout_netlist=layout_netlist,
            layout_circuit=layout_circuit,
            reference_netlist=reference_netlist,
            reference_circuit=reference_circuit,
            setup_file=setup_file,
            timeout_s=timeout_s,
            binary=netgen_binary,
        )
        layout_net_count = sum(1 for _ in layout_circuit.each_net())
        reference_net_count = sum(1 for _ in reference_circuit.each_net())
        layout_device_count = sum(1 for _ in layout_circuit.each_device())
        reference_device_count = sum(1 for _ in reference_circuit.each_device())
        layout_pin_count = layout_circuit.pin_count()
        reference_pin_count = reference_circuit.pin_count()
        # Known limitation (see docs/cli/lvs.md, "Engine" -> netgen):
        # `_parse_netgen_report` classifies netgen's text report into
        # `mismatches[]`, but does not reconstruct a full per-net/per-device
        # correspondence the way the `klayout` engine's `NetlistComparer`
        # callbacks do. On a `"match"` verdict the matched count is exact by
        # construction (a unique match requires equal cardinality on both
        # sides); on `"mismatch"` it is intentionally left at the
        # conservative floor (`0`) rather than a fabricated estimate --
        # never overstating how much of the netlist was actually verified.
        # `net_correspondence` is `[]` for the same reason, which keeps the
        # documented `len(net_correspondence) == counts.nets.matched`
        # invariant intact for this engine too (both sides of that equation
        # are `0` together on a mismatch).
        matched_nets = layout_net_count if status == "match" else 0
        matched_devices = layout_device_count if status == "match" else 0
        matched_pins = layout_pin_count if status == "match" else 0
        net_correspondence = []
        counts = {
            "nets": {
                "layout": layout_net_count,
                "reference": reference_net_count,
                "matched": matched_nets,
            },
            "devices": {
                "layout": layout_device_count,
                "reference": reference_device_count,
                "matched": matched_devices,
            },
            "pins": {
                "layout": layout_pin_count,
                "reference": reference_pin_count,
                "matched": matched_pins,
            },
        }

    # `layout_deck_name`/`layout_deck` were already resolved above (before
    # the `combine_devices` block) -- reused here for `device_classes`
    # (issue #221), `null` when `layout.netlist` (pre-extracted) was given
    # instead of `layout.file` + `layout.deck`, since no deck is involved in
    # that shape.
    device_classes = (
        list(layout_deck.device_classes) if layout_deck is not None else None
    )

    # Issue #1983: the machine-checkable counterpart of the
    # `device.body_unverified` warning below, replaced with a real verdict
    # whenever inline extraction ran. Emitted as an explicit `"unchecked"`
    # carrying a reason rather than omitted, so "were this layout's device
    # bodies verifiably tied?" is answerable from any `klt lvs` report on its
    # own -- on the pre-extracted `layout.netlist` form the warning's absence
    # means "not checked", and on an inline extraction it means "checked and
    # clean", and nothing in the report used to tell those apart.
    body_verification: dict[str, Any] = _body_verification_unchecked(
        "no request.layout.deck was given (the pre-extracted "
        "request.layout.netlist form), so nothing establishes this layout's "
        "substrate/well-tap convention and no synthesized body net can be "
        "told apart from a real one -- this run verified nothing about the "
        "device bodies either way"
    )

    if layout_deck is not None:
        # Issue #281: MOS body terminals extracted onto deck-synthesized or
        # anonymous nets (never a real schematic net -- see
        # `_body_net_warnings`) are a property of the inline extraction --
        # which deck ran and what tie geometry this layout drew (per-device
        # on both arms since issue #2048) -- not of this particular compare
        # run's pairings. Appended (and the list re-sorted)
        # rather than folded into `_build_mismatches`, since these entries
        # never come from a `NetlistComparer` event and do not participate in
        # the `compare_result`/safety-net invariant above -- they are purely
        # additive, non-blocking notes.
        mismatches.extend(_body_net_warnings(layout_circuit, layout_deck))
        # Issue #1983: same determination, rendered as the gradeable block
        # -- see `_body_verification_report` for why one source of truth
        # backs both renderings.
        body_verification = _body_verification_report(layout_circuit, layout_deck)

    if combine_warnings:
        # Issue #466: same rationale as `_body_net_warnings` above -- these
        # never come from a `NetlistComparer` event either, so they are
        # appended (and the list re-sorted) rather than folded into
        # `_build_mismatches`. Unlike the inline-extraction-only body-net
        # warnings, this fires for any request (pre-extracted `layout.netlist` and
        # `"netgen"` engine included), since `combine_devices()` runs before
        # the engine branch above.
        mismatches.extend(combine_warnings)

    if combine_per_circuit_warnings:
        # Issue #1552: same rationale as `combine_warnings` just above --
        # `options.combine_devices_per_circuit` runs before `_select_circuit`
        # even, so these are request-side transforms too, not
        # `NetlistComparer` events.
        mismatches.extend(combine_per_circuit_warnings)

    if bulk_warnings:
        # Issue #506: same rationale again -- a `reference.device_bulk`
        # disclosure records a *request-side* normalisation applied before the
        # compare, not a `NetlistComparer` event, so it is appended here
        # rather than folded into `_build_mismatches`. Always
        # `severity: "warning"`: it never changes `status`, it only keeps a
        # match achieved through the hook from being indistinguishable from a
        # fully independent one.
        mismatches.extend(bulk_warnings)

    if placeholder_warnings:
        # Issue #1907: same rationale again -- a `device.placeholder_value`
        # disclosure records a *request-side* normalisation (one parameter of
        # a converted reference class excluded from the compare) applied
        # before the compare, not a `NetlistComparer` event, so it is appended
        # here rather than folded into `_build_mismatches`. Always
        # `severity: "warning"`: it never changes `status`, it only keeps a
        # match reached with that class's value dimension unverified from
        # being indistinguishable from one where the two values agreed.
        mismatches.extend(placeholder_warnings)

    if tolerance_warnings:
        # Issue #589: same rationale once more -- a `parameter_tolerance`
        # disclosure records a value this run deliberately absorbed *between*
        # the two `compare()` passes, not a `NetlistComparer` event, so it is
        # appended here rather than folded into `_build_mismatches`. Always
        # `severity: "warning"`: it never changes `status` (the second
        # `compare()` already did that, or didn't), it only keeps a match
        # reached through the caller's tolerance from being indistinguishable
        # from one where the two values actually agreed.
        mismatches.extend(tolerance_warnings)

    if compare_parameter_warnings:
        # Issue #1928: same rationale once more -- a `compare_parameters`
        # disclosure records a request-side compare-scoping choice applied
        # before the compare, not a `NetlistComparer` event, so it is
        # appended here rather than folded into `_build_mismatches`. Always
        # `severity: "warning"`: it never changes `status`, it only keeps a
        # match reached with a parameter scoped out from being
        # indistinguishable from one where every parameter actually agreed.
        mismatches.extend(compare_parameter_warnings)

    if flatten_warnings:
        # Issue #1085: same rationale as the disclosures above -- a
        # `flatten_reference`/`flatten_layout` structural flatten is a
        # request-side transform applied before the compare (indeed, before
        # `_select_circuit` even runs), not a `NetlistComparer` event, so it
        # is appended here rather than folded into `_build_mismatches`.
        # Collected once, up front (before the `combine_devices`/engine
        # branches below even run), so it fires for any request shape --
        # both engines, both layout shapes -- the same way `combine_warnings`
        # does.
        mismatches.extend(flatten_warnings)

    if power_only_pruning_warnings:
        # Issue #1622: same rationale as the disclosures above -- pruning a
        # power-only layout circuit is a request-side transform applied
        # before the compare (before even `combine_devices_per_circuit`/
        # `flatten_layout` above), not a `NetlistComparer` event, so it is
        # appended here rather than folded into `_build_mismatches`.
        mismatches.extend(power_only_pruning_warnings)

    # Issue #2421: same append-here rationale as the disclosures above --
    # this is a pre-flight determination about the reference netlist and the
    # deck, made before the compare ran, not a `NetlistComparer` event.
    # Unlike them it is `severity: "error"` (the reference really cannot
    # match), but like `device.class_arity` it is diagnostic only: it never
    # moves `status` on its own -- the compare that already ran reported the
    # mismatch this entry explains. Extended unconditionally (like
    # `supply_findings` below, unlike the `if`-guarded warnings above): the
    # list is empty on every run that has no such family to report.
    mismatches.extend(family_findings)

    if gate_level_port_alias_warnings:
        # Issue #2021: same rationale as the disclosures above -- joining an
        # `assign`-aliased reference port's net onto its canonical target is
        # a request-side transform applied before the compare (before even
        # the power-only prune above), not a `NetlistComparer` event, so it
        # is appended here rather than folded into `_build_mismatches`.
        mismatches.extend(gate_level_port_alias_warnings)

    mismatches.extend(supply_findings)
    mismatches.sort(key=_sort_key)

    if combine_incomplete and status == "mismatch":
        # Issue #1370: `options.combine_devices` was requested, at least one
        # side exhausted its retry budget, and both sides were rolled back to
        # their pre-combine state (the symmetric degrade above). The compare
        # that actually ran is therefore *not* the compare the caller asked
        # for -- a folded layout compared uncombined against a lumped
        # reference mismatches by construction, so this "mismatch" says
        # nothing about whether the design differs. Report it as
        # `"inconclusive"` instead, mirroring `klt equiv`'s own
        # never-report-an-unreachable-verdict rule (see
        # `STATUS_INCONCLUSIVE`).
        #
        # Deliberately only downgrades `"mismatch"`, never `"match"`: this
        # module never re-derives a verdict the engine did not reach (see the
        # module docstring), and an uncombined compare that still matched is
        # a real, strictly-stronger match -- the fold turned out to be
        # unnecessary. `mismatches[]` keeps the `device.combine_incomplete`
        # warning either way, so a caller can always see that combining did
        # not apply.
        status = STATUS_INCONCLUSIVE

    category_counts: dict[str, int] = {}
    # Issue #1132: `category_counts` alone collapses `error`/`warning`
    # entries of the same category into one number (e.g. `"topology": 8`
    # could be six warnings and two errors), so a caller cannot gate on
    # "any error present" without re-reading every `mismatches[]` entry.
    # `category_error_counts` mirrors `category_counts`'s shape (same keys,
    # sorted, additive) but counts only `severity: "error"` entries per
    # category -- a category with zero errors (e.g. an all-`warning`
    # `topology.flattened`) is simply absent, matching `category_counts`'s
    # own "no entries of this category at all" convention of omitting the
    # key rather than reporting 0.
    category_error_counts: dict[str, int] = {}
    for mismatch in mismatches:
        category_counts[mismatch["category"]] = (
            category_counts.get(mismatch["category"], 0) + 1
        )
        if mismatch["severity"] == "error":
            category_error_counts[mismatch["category"]] = (
                category_error_counts.get(mismatch["category"], 0) + 1
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "layout": layout_echo,
        "reference": reference_echo,
        "top": layout_circuit.name,
        # Issue #1205: the *reference* side's resolved top circuit name,
        # alongside the layout side's `top` above. The two are equal for the
        # ordinary compare, but differ by construction for an LVS negative
        # control (a deliberately-broken `<cell>_shorted` layout compared
        # against the intact `<cell>`'s reference netlist). Recording only
        # one of them made such a report unreconstructable by `--check
        # --rerun`, which applied the single `top` to both sides and failed
        # with "top cell/subcircuit not found in reference netlist" -- see
        # `_reconstruct_lvs_request`.
        "reference_top": reference_circuit.name,
        # Issue #589: the effective `options.parameter_tolerance`, echoed so a
        # consumer reading only the response can tell whether a `"match"` was
        # reached under a caller-supplied design tolerance at all. `null` when
        # the option was omitted (today's exact-compare behaviour).
        "parameter_tolerance": parameter_tolerance,
        # Issue #1205: every request option that shapes *what was compared*,
        # echoed as resolved, so a committed report round-trips back into the
        # request it came from (`_reconstruct_lvs_request`) instead of being
        # re-run as a differently-shaped compare whose difference is then
        # reported as drift. Keys are always present with their effective
        # values (never omitted), matching `parameter_tolerance`'s own
        # always-present discipline -- `parameter_tolerance` is repeated here
        # so this block is a complete request-side view, while the top-level
        # field above stays exactly where it has always been.
        #
        # Deliberately *not* echoed here: `options.keep_extracted` (an
        # output-side flag -- it only controls whether the intermediate
        # netlist is also written to disk, and is already visible as
        # `environment.extracted_netlist`) and `options.netgen_timeout_s` (a
        # runtime guard, not a compare input). Neither can change a verdict.
        #
        # `combine_devices` echoes the *resolved* request value, so it is a
        # boolean for the bool shape and the (normalised) list of device-class
        # names for issue #1370's list shape -- the same round-trip discipline
        # every other key here follows.
        "options": {
            "combine_devices": combine_devices,
            # Issue #1552: `null` when the option was omitted, else the
            # resolved `{<circuit-name-glob>: <bool>}` mapping -- the same
            # always-present-but-nullable convention `netgen_setup` already
            # follows, since (unlike `combine_devices`) there is no
            # meaningful default value to echo instead of the option being
            # absent.
            "combine_devices_per_circuit": combine_devices_per_circuit,
            "flatten_layout": flatten_layout,
            "flatten_reference": flatten_reference,
            "netgen_setup": netgen_setup_echo,
            "parameter_tolerance": parameter_tolerance,
            # Issue #1928: `null` when the option was omitted, else the
            # resolved `{<device-class>: [<parameter>, ...]}` mapping -- the
            # same always-present-but-nullable convention
            # `combine_devices_per_circuit`/`netgen_setup` already follow.
            "compare_parameters": compare_parameters,
            # Issue #1952: `null` when the option was omitted (the check
            # still runs -- omitting it is the default-on case), else the
            # caller's own `true`/`false`/`{"expected_nets": {...}}` value
            # verbatim. Same always-present-but-nullable convention
            # `combine_devices_per_circuit`/`netgen_setup` follow; echoed
            # unresolved so a committed report round-trips back into the
            # request document it came from.
            "power_connectivity": power_connectivity_echo,
            "supply_nets": supply_nets,
        },
        # Issue #1998: every `hints.equivalent_pins` grouping actually passed
        # to `NetlistComparer.equivalent_pins()` for this run, keyed by
        # (reference-side) subcircuit name -- unlike `hints.same_nets`,
        # which the comparer can refuse (surfaced as a `hints.rejected`
        # mismatch entry, see `_build_mismatches`), a swappable-pin group has
        # no rejection outcome to report, so without this field a caller
        # reading only the response has no way to tell whether an
        # `equivalent_pins` hint changed the verdict at all. `null` when
        # `request.hints.equivalent_pins` was omitted (or resolved to no
        # groups), matching `options.compare_parameters`'
        # always-present-but-nullable convention for an optional dict-shaped
        # echo -- never a spuriously present empty `{}`.
        "hints_applied": (equivalent_pins_applied if equivalent_pins_applied else None),
        "status": status,
        # Issue #1952: the power/ground half of a `reference.form:
        # "gate-level-verilog"` compare, reported as its own verdict beside
        # `status` rather than folded into it. `status` is, and stays,
        # exactly `NetlistComparer.compare()`'s own signal-connectivity
        # result (see this module's docstring) -- so a caller that wants
        # full LVS on a digital block gates on
        # `status == "match" and power_connectivity["status"] == "match"`.
        # See `_power_connectivity_report` and
        # `docs/design/pg-connectivity-check-decision.md`.
        "power_connectivity": power_connectivity,
        # Issue #1983: whether this layout's MOS body terminals were resolved
        # from real drawn/labelled geometry or from a deck-synthesized net --
        # the gradeable form of the `device.body_unverified` warning, which
        # was previously only discoverable by string-matching a `category`
        # inside `mismatches[]`. Reported beside `status`, never folded into
        # it: `status` is, and stays, exactly the comparer's own result, so a
        # layout with unverified bodies still reports `status: "match"` when
        # the compare matched. See `_body_verification_report`.
        "body_verification": body_verification,
        "mismatch_count": len(mismatches),
        "error_count": sum(category_error_counts.values()),
        "category_counts": dict(sorted(category_counts.items())),
        "category_error_counts": dict(sorted(category_error_counts.items())),
        "counts": counts,
        "device_classes": device_classes,
        "environment": {
            "engine": engine,
            "engine_version": engine_version,
            # Issue #2373: which netgen executable actually produced this
            # verdict -- the absolute path `_resolve_netgen_binary` settled
            # on, so a committed report distinguishes a from-source `netgen`
            # from Debian/Ubuntu's `netgen-lvs` (and from an explicitly
            # named build). Always `null` for `"engine": "klayout"`, which
            # launches no subprocess.
            "netgen_binary": netgen_binary,
            "layout_sha256": sha256_file(layout_hash_source),
            "reference_sha256": sha256_file(reference_netlist_path),
            "extracted_netlist": extracted_netlist_path,
        },
        "provenance": build_provenance(
            deck_name=layout_deck_name,
            deck_path=(
                deck_source_path(layout_deck_name) if layout_deck_name else None
            ),
            # Issue #1901: populated only for a `gate-level-verilog`
            # reference whose `reference.pdk`/`reference.pdk_root` resolved
            # a PDK (see `reference_pdk_info` above) -- `None` for a plain
            # SPICE-vs-SPICE reference, which genuinely involves no PDK.
            pdk=reference_pdk_info,
            # Issue #1969: pin the layout side under `provenance.input`, the
            # shared block's own field, for *both* engines (this call is
            # reached after the `klayout`/`netgen` branch converges, and
            # `layout_hash_source` is resolved before it). This deliberately
            # reverses issue #331's original call: `environment.layout_sha256`
            # above records the same digest of the same file (both go through
            # `sha256_file`), but only under an LVS-specific key no generic
            # consumer reads. `klt signoff --manifest`'s T1 item-4 staleness
            # gate reads `provenance.input.content_hash` generically across
            # every kind, so leaving this `null` made *every* content-hash-
            # pinned "LVS clean" citation render `stale_evidence` -- a pinned
            # hash can never match `None`. `environment.layout_sha256` is a
            # bare hex digest and stays exactly as it was (a report-shape
            # contract of its own); `provenance.input.content_hash` is the
            # `sha256:`-prefixed form, so the two are redundant in content
            # but not interchangeable in shape.
            input_path=layout_hash_source,
            # Issue #2027: and say *what* that hash is of -- `"layout"` for
            # the `layout.file` shape (the original stream, the same bytes
            # `klt drc` hashes), `"netlist"` for the pre-extracted
            # `layout.netlist` shape. Without this discriminator `klt
            # signoff` compared a pre-extracted run's SPICE digest against a
            # DRC report's layout digest and refused to aggregate a
            # perfectly consistent pair (the repo's own `examples/signoff/`
            # pair reproduced it).
            input_role=layout_input_role,
            # Issue #600: echo the resolved `layout.deck_options` mapping
            # under `provenance.deck.options`, matching `klt extract`'s
            # shape exactly (`_deck_block` omits the key entirely when
            # `deck_options` is `None`/empty).
            deck_options=deck_options,
            # Issue #2394: ...and, identically to `klt extract`, record the
            # *fully resolved* option set (the deck's own defaults for every
            # key `layout.deck_options` did not pin) plus `options_explicit`/
            # `options_hash`. `layout_deck_name` is `None` for the
            # pre-extracted `layout.netlist` shape, where `_deck_block`
            # returns `None` and this has no effect.
            resolve_deck_options=True,
            include_klayout_version_mismatch=True,
        ),
        "mismatches": mismatches,
        "net_correspondence": net_correspondence,
    }


# --------------------------------------------------------------------------- #
# --check / --rerun: verify a previously committed report (issue #1106)
# --------------------------------------------------------------------------- #


#: `provenance` fields `check_lvs_report()` treats as a non-fatal *advisory*
#: rather than a hash-integrity failure (issue #1373). `klt_version` and
#: `klayout_version` are included because they answer the question `--check`
#: was previously silent on: "did the engine itself change between the
#: committed run and now". `provenance.pdk.version` is deliberately left out
#: -- unlike the tool build, a PDK release routinely differs by design
#: between machines/CI runners without implying anything about *this*
#: report's reproducibility, so folding it in here would make the advisory
#: noisy rather than informative. This mirrors -- but is a distinct read
#: path from -- `_report_verify.VOLATILE_PROVENANCE_PATHS`, which excludes
#: all three fields from `--rerun`'s drift diff (#1106); that exclusion is
#: unchanged by this constant.
_VERSION_ADVISORY_FIELDS: tuple[tuple[str, Callable[[], str | None]], ...] = (
    ("klt_version", _klt_version),
    ("klayout_version", _klayout_version),
)


def _version_drift_advisories(committed: dict[str, Any]) -> list[dict[str, Any]]:
    """Non-fatal advisories (issue #1373) naming any `provenance.<field>` in
    `_VERSION_ADVISORY_FIELDS` that differs between `committed` and the
    engine currently running `--check`. A missing value on either side (an
    older report predating these fields, or an unresolvable current
    version) is "unknown", not "drift" -- it is silently skipped rather than
    reported, so `--check` never crashes or false-positives on a report
    format gap.
    """
    advisories = []
    for field, current_getter in _VERSION_ADVISORY_FIELDS:
        report_value = get_path(committed, ("provenance", field))
        current_value = current_getter()
        if report_value is None or current_value is None:
            continue
        if report_value == current_value:
            continue
        advisories.append(
            {
                "field": f"provenance.{field}",
                "report": report_value,
                "current": current_value,
            }
        )
    return advisories


def check_lvs_report(report_path: str) -> dict[str, Any]:
    """``klt lvs --check`` (cheap mode, issue #1106): verify a previously
    committed ``klt lvs --format json`` report at ``report_path`` still
    reproduces, without re-running the compare engine at all.

    Reconciles all three hashes LVS's own envelope carries (unlike ``klt
    drc``'s single input hash -- see this module's ``environment``/
    ``provenance.deck`` docstrings): re-hashes ``committed["layout"]`` and
    ``committed["reference"]`` (the paths exactly as echoed back by the
    original run) via :func:`klayout_tools._provenance.sha256_file` and
    compares each against ``environment.layout_sha256``/
    ``environment.reference_sha256``; when the committed report used an
    extraction deck (``provenance.deck`` is non-``null``), also re-hashes
    that deck's source (:func:`~klayout_tools.decks.deck_source_path`) and
    compares against ``provenance.deck.content_hash``. Each mismatch names
    which of the (up to three) inputs moved -- never a single pass/fail bit.

    **Known limitation**: ``committed["layout"]``/``["reference"]`` are
    echoed *exactly as given* in the original request document (see
    :func:`run_lvs`'s docstring on ``layout_echo``/``reference_echo``),
    which may have been relative to that request *file's own directory* --
    not necessarily the current working directory. This re-hashes them
    relative to the current working directory (the same convention ``klt
    drc --check``'s ``file`` field uses); if the original request used
    request-file-relative paths, invoke ``--check`` from that same
    directory, or commit reports whose ``layout``/``reference`` are already
    absolute paths.

    Raises :class:`LvsError` for a missing/unparseable committed report --
    never a traceback.

    The result additionally carries an ``advisories`` list (issue #1373):
    entries naming a ``provenance.klt_version``/``provenance.klayout_version``
    that differs between the committed report and the engine currently
    running ``--check`` -- see :func:`_version_drift_advisories`. This is
    purely informational: it never affects ``status`` (still driven only by
    the hash ``checks`` above) or the caller's exit code, and is never folded
    into ``checks``' ``[OK]``/``[DRIFTED]`` list, so a scripted gate grepping
    that list for a failure marker does not start matching on it.
    """
    committed = _load_committed_report(report_path, LvsError)
    checks = [
        hash_check(
            "environment.layout_sha256",
            get_path(committed, ("environment", "layout_sha256")),
            sha256_file(committed.get("layout")),
        ),
        hash_check(
            "environment.reference_sha256",
            get_path(committed, ("environment", "reference_sha256")),
            sha256_file(committed.get("reference")),
        ),
    ]
    deck = get_path(committed, ("provenance", "deck"))
    if isinstance(deck, dict) and deck.get("name") is not None:
        checks.append(
            hash_check(
                "provenance.deck.content_hash",
                deck.get("content_hash"),
                _content_hash(deck_source_path(deck["name"])),
            )
        )
    result = build_check_result(report_path=report_path, checks=checks)
    result["advisories"] = _version_drift_advisories(committed)
    return result


def _reconstruct_lvs_request(committed: dict[str, Any]) -> dict[str, Any]:
    """Best-effort reconstruction of a ``klt lvs`` request document from a
    previously committed report, for :func:`rerun_lvs_report`.

    Reconstructs the fields the response actually echoes: ``engine``,
    ``layout`` (``file``+``deck``[+``deck_options``] when
    ``provenance.deck`` is populated, else ``netlist`` -- the pre-extracted
    ``layout.netlist`` shape is unambiguous *without* a deck, since only
    ``layout.file`` and the ``layout.netlist``+``layout.deck`` combo, issue
    #585, populate ``provenance.deck`` at all), ``reference.netlist``,
    each side's own resolved top (``layout.top`` from ``report["top"]``,
    ``reference.top`` from ``report["reference_top"]``), and the whole
    compare-shaping ``options`` block (``combine_devices``/
    ``flatten_layout``/``flatten_reference``/``netgen_setup``/
    ``parameter_tolerance``/``compare_parameters``, from
    ``report["options"]``).

    Issue #1205: the last two used to be lossy. The response recorded a
    single ``top`` applied to *both* sides here, so a report whose request
    set a ``reference.top`` different from the layout top -- an LVS negative
    control (``<cell>_shorted`` layout vs. the intact ``<cell>``'s reference
    netlist) has two different tops by construction -- could not be
    reconstructed at all and errored out with "top cell/subcircuit not found
    in reference netlist". And ``options`` was rebuilt from
    ``parameter_tolerance`` alone, so a report from a
    ``combine_devices: true`` request silently re-ran the compare *without*
    folding, then reported the resulting fresh mismatch list as drift. Both
    fields are now echoed by :func:`run_lvs` and consumed here.

    Reports predating those two fields (written before issue #1205) still
    reconstruct exactly as they used to: ``reference_top`` falls back to the
    committed ``top``, and ``options`` falls back to the top-level
    ``parameter_tolerance`` echo. :func:`rerun_lvs_report` additionally
    excludes both fields from the drift diff for such a report, since a
    field the committed report never carried cannot itself have drifted.

    **Known limitations** (still not reconstructible from the response
    alone, so silently omitted -- ``--rerun`` is best-effort, not a
    byte-exact replay): the ``layout.netlist``+``layout.deck`` combo (issue
    #585) is indistinguishable from plain ``layout.file`` inline extraction
    here -- both populate ``provenance.deck`` -- so this always reconstructs
    ``layout.file``; that combo will fail loudly (a clean
    :class:`LvsError` from the layout loader, not a silent wrong answer)
    since ``committed["layout"]`` is actually a SPICE netlist, not a
    layout stream, in that case. ``reference.form`` (a non-default
    ``"subckt-call"`` reference), ``reference.device_map``/``device_bulk``
    and ``layout.top_cell_pins``/``declared_pins``/``pin_source_cells`` are
    never echoed anywhere in the response and are always omitted
    (reconstructed as each option's own default). Use ``--check`` (cheap
    mode) instead when any of these apply.
    """
    deck = get_path(committed, ("provenance", "deck"))
    has_deck = isinstance(deck, dict) and deck.get("name") is not None
    top = committed.get("top")
    # Issue #1205: each side's own top. `reference_top` is additive -- a
    # report predating it only ever recorded the one `top`, which is exactly
    # the (symmetric-top) assumption this used to make unconditionally, so
    # falling back to it reconstructs such a report the same way as before.
    reference_top = committed.get("reference_top") or top

    layout_spec: dict[str, Any] = {"top": top} if top else {}
    if has_deck:
        layout_spec["file"] = committed.get("layout")
        layout_spec["deck"] = deck["name"]
        # Issue #2394: replay only the options the *caller* pinned, not the
        # resolved set `provenance.deck.options` now records -- re-pinning a
        # silently-defaulted key would replay over exactly the deck-default
        # change a rerun exists to surface as drift.
        deck_options = explicit_deck_options(deck)
        if deck_options:
            layout_spec["deck_options"] = deck_options
    else:
        layout_spec["netlist"] = committed.get("layout")

    reference_spec: dict[str, Any] = {"netlist": committed.get("reference")}
    if reference_top:
        reference_spec["top"] = reference_top

    request: dict[str, Any] = {
        "engine": committed.get("engine", "klayout"),
        "layout": layout_spec,
        "reference": reference_spec,
    }

    echoed = committed.get("options")
    echoed = echoed if isinstance(echoed, dict) else {}
    options: dict[str, Any] = {}
    for flag in ("combine_devices", "flatten_layout", "flatten_reference"):
        # Only re-assert an option that was actually on: a `false` entry is
        # every option's own default, and omitting it keeps the
        # reconstructed request document as close to the original as the
        # echo allows.
        value = echoed.get(flag)
        if not value:
            continue
        # Issue #1370: `combine_devices` may be a list of device-class names,
        # not just `true` -- re-assert it verbatim so `--rerun` reproduces the
        # *restricted* combine the committed report was produced under,
        # rather than an unrestricted one whose difference it would then
        # report as drift.
        options[flag] = list(value) if isinstance(value, list) else True
    # Issue #1552: re-assert the per-circuit map verbatim, same rationale as
    # the list-shaped `combine_devices` re-assertion just above -- a `null`/
    # missing entry (the option was never set) is left out entirely, not
    # reconstructed as `{}` (which `_parse_combine_devices_per_circuit` would
    # itself reject as invalid).
    combine_devices_per_circuit = echoed.get("combine_devices_per_circuit")
    if isinstance(combine_devices_per_circuit, dict) and combine_devices_per_circuit:
        options["combine_devices_per_circuit"] = dict(combine_devices_per_circuit)
    netgen_setup = echoed.get("netgen_setup")
    if netgen_setup is not None:
        options["netgen_setup"] = netgen_setup
    # `options.parameter_tolerance` is echoed twice (top-level since issue
    # #589, inside `options` since #1205); prefer the block, fall back to the
    # top-level field so a report predating the block still round-trips.
    parameter_tolerance = echoed.get("parameter_tolerance")
    if parameter_tolerance is None:
        parameter_tolerance = committed.get("parameter_tolerance")
    if parameter_tolerance is not None:
        options["parameter_tolerance"] = parameter_tolerance
    # Issue #1928: re-assert the resolved `{<device-class>: [<parameter>,
    # ...]}` mapping verbatim, same rationale as `combine_devices_per_circuit`
    # just above -- a `null`/missing entry (the option was never set) is left
    # out entirely, not reconstructed as `{}` (which
    # `_parse_compare_parameters` would itself reject as invalid).
    compare_parameters = echoed.get("compare_parameters")
    if isinstance(compare_parameters, dict) and compare_parameters:
        options["compare_parameters"] = {
            key: list(value) for key, value in compare_parameters.items()
        }
    # Issue #1952: re-assert the caller's own `power_connectivity` value
    # verbatim when the committed report carried one. `None` (the option was
    # omitted) is left out entirely rather than reconstructed as `true`:
    # omitting it is already the default-on case, so the reconstructed
    # request stays as close to the original as the echo allows -- the same
    # discipline the option re-assertions above follow.
    power_connectivity = echoed.get("power_connectivity")
    if isinstance(power_connectivity, bool):
        options["power_connectivity"] = power_connectivity
    elif isinstance(power_connectivity, dict):
        options["power_connectivity"] = dict(power_connectivity)
    # Includes []: explicitly disabling the finding must survive a replay.
    options.update(_supply_nets_replay_options(echoed))
    if options:
        request["options"] = options
    return request


def _supply_nets_replay_options(echoed: dict[str, Any]) -> dict[str, Any]:
    names = echoed.get("supply_nets")
    return {"supply_nets": list(names)} if isinstance(names, list) else {}


#: `rerun_lvs_report`'s own exclusion set (issue #1223), layered on top of
#: the shared `VOLATILE_PROVENANCE_PATHS`: `environment.extracted_netlist`
#: is populated only when the *original* request set
#: `options.keep_extracted: true`, and its value is
#: `<request_dir>/.klt/lvs/<top>.spice` -- a path anchored to the original
#: request document's own directory. `_reconstruct_lvs_request` deliberately
#: never re-asserts `keep_extracted` (it is an output-side flag that cannot
#: change a verdict, and re-running it would write files as a side effect),
#: and even if it did, the reconstructed request is passed to `run_lvs` as an
#: inline JSON string whose `request_dir` is the current working directory --
#: so a fresh path would still differ from the committed one whenever
#: `--rerun` runs from a different directory than the original request did.
#: This is verb-local (not folded into the shared `VOLATILE_PROVENANCE_PATHS`
#: in `_report_verify.py`): `klt drc --rerun` has no analogous output-path
#: field to exclude, so widening the shared constant would be correct for LVS
#: but dead configuration for DRC.
#:
#: `environment.netgen_binary` (issue #2373) is excluded for the same
#: host-local-path reason, plus one of its own:
#:
#: * It is an absolute path resolved against the *running host's* `PATH` --
#:   `/usr/local/bin/netgen` on a from-source host, `/usr/bin/netgen-lvs` on
#:   Debian/Ubuntu. Re-verifying a committed report on a second host is the
#:   whole point of `--check --rerun`, and that host resolving the same
#:   netgen at a different path (or under the other packaged name) is not a
#:   change in what was compared. The comparator's *semantic* identity stays
#:   diffed: `environment.engine_version` is netgen's own reported version
#:   and is deliberately **not** excluded, so an actually-different netgen
#:   build still surfaces as drift.
#: * Every `klt lvs` report committed before #2373 carries no
#:   `netgen_binary` key at all, and `diff_verdict_fields` compares the
#:   union of both sides' keys -- so without this exclusion every such
#:   report (both engines, including the `null`-vs-absent `"klayout"` case)
#:   would re-run as `drifted` on a field it never carried. That is the same
#:   "a field a report never carried cannot itself have drifted" rule
#:   `rerun_lvs_report` applies to the #1205/#1952/#1983 request-echo
#:   fields; an unconditional exclusion covers it without needing the
#:   committed-report probe, because the field is volatile going forward
#:   too.
_LVS_RERUN_EXCLUDE_PATHS: frozenset[tuple[str, ...]] = VOLATILE_PROVENANCE_PATHS | {
    ("environment", "extracted_netlist"),
    ("environment", "netgen_binary"),
}


def rerun_lvs_report(report_path: str) -> dict[str, Any]:
    """``klt lvs --check <report> --rerun`` (full mode, issue #1106):
    verify a previously committed ``klt lvs --format json`` report at
    ``report_path`` by best-effort re-running the compare it describes
    (:func:`_reconstruct_lvs_request` -- see its docstring for exactly
    what is and isn't reconstructible from the response alone) and diffing
    the fresh report against the committed one.

    Diffs via :func:`klayout_tools._report_verify.diff_verdict_fields`,
    excluding :data:`_LVS_RERUN_EXCLUDE_PATHS` --
    :data:`klayout_tools._report_verify.VOLATILE_PROVENANCE_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version`` --
    ``pdk`` is always ``null`` for LVS, so only the first two ever apply in
    practice) plus ``environment.extracted_netlist`` (issue #1223): that
    field is populated only when the *original* request set
    ``options.keep_extracted: true``, its value is an absolute path anchored
    to the original request document's own directory, and
    ``_reconstruct_lvs_request`` never re-asserts ``keep_extracted`` -- so a
    fresh rerun always reports it as ``null`` even when nothing else about
    the compare changed, which is a false drift, not a real one, on a
    report committed from a ``keep_extracted: true`` request; plus
    ``environment.netgen_binary`` (issue #2373), a host-local absolute path
    whose value legitimately differs between the committing and the
    verifying host without anything about the compare changing -- the
    comparator's semantic identity stays diffed via the *not*-excluded
    ``environment.engine_version``. ``status:
    "drifted"`` names every other changed field, including a changed
    ``status``/``mismatch_count``/``mismatches`` (the LVS-outcome-changed
    case) as well as changed ``environment.layout_sha256``/
    ``reference_sha256`` (the input-moved case ``--check`` also catches,
    redundantly but harmlessly here since this mode always re-hashes as a
    side effect of re-running).

    Additionally excludes any of the request-echo fields issue #1205 added
    (``reference_top``, ``options``) that the *committed* report predates:
    a field a report never carried cannot itself have drifted, so a report
    written before those fields existed keeps re-running exactly as it did
    before rather than reporting the current build's richer echo as drift.
    (Such a report is still only best-effort reconstructible -- it records
    no reference-side top and no ``options``, which is the very gap #1205
    closed going forward. Use cheap ``--check`` on reports that predate it
    when the original request used either.)

    Raises :class:`LvsError` for a missing/unparseable committed report, a
    report missing ``layout``/``reference`` to rerun, or any error the
    rerun itself raises -- never a traceback.
    """
    committed = _load_committed_report(report_path, LvsError)
    if committed.get("layout") is None or committed.get("reference") is None:
        raise LvsError(
            f"committed report has no 'layout'/'reference' field to rerun: "
            f"{report_path}"
        )
    request = _reconstruct_lvs_request(committed)
    fresh = run_lvs(json.dumps(request))
    exclude = set(_LVS_RERUN_EXCLUDE_PATHS)
    exclude.update(
        (field,)
        for field in (
            "reference_top",
            "options",
            "power_connectivity",
            # Issue #1983: same rule -- a report committed before the
            # `body_verification` block existed cannot have drifted in it.
            "body_verification",
        )
        if field not in committed
    )
    # Issue #1952: the same "a field the committed report never carried
    # cannot itself have drifted" rule, one level down -- a report committed
    # after issue #1205 added the `options` echo but before this option
    # existed has an `options` block without this key, and the current
    # build's richer echo is not drift.
    committed_options = committed.get("options")
    for option in ("power_connectivity", "supply_nets"):
        if not isinstance(committed_options, dict) or option not in committed_options:
            exclude.add(("options", option))
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=frozenset(exclude),
    )


# --------------------------------------------------------------------------- #
# Request-side resolution: layout (inline extraction or pre-extracted), reference
# --------------------------------------------------------------------------- #


def _require_path(spec: dict[str, Any], field: str, side: str, request_dir: str) -> str:
    value = spec.get(field)
    if value is None:
        raise LvsError(f"request.{side}.{field} is required")
    resolved = _resolve_relative(value, request_dir)
    if not os.path.isfile(resolved):
        raise LvsError(f"{side} {field} not found: {resolved}")
    return resolved


def _resolve_layout(
    layout_spec: dict[str, Any],
    request_dir: str,
    keep_extracted: bool,
    combine_devices: bool = False,
    deck_options: Mapping[str, str] | None = None,
) -> tuple[kdb.Netlist, str, str, str | None, dict[int, list[dict[str, Any]]]]:
    """Resolve ``request.layout`` to ``(netlist, echo, hash_source_path,
    extracted_netlist_path_or_none, net_label_positions)``. Original drawn
    labels are keyed by layout cluster for the supply-fragmentation check;
    pre-extracted SPICE has no such label map and returns an empty mapping.

    Two supported shapes (spike section 2b): ``{"file", "deck", "top"}`` runs
    inline extraction (composing ``extract.py``'s core function); ``{"netlist",
    "top"}`` reads a pre-extracted SPICE file directly. Exactly one of
    ``file``/``netlist`` must be given.

    ``deck_options`` (issue #600, ``request.layout.deck_options``): forwarded
    to :func:`~klayout_tools.extract.extract_netlist_from_layout` for the
    ``layout.file`` shape only -- it selects a caller-visible sheet-rho
    flavour of a shared-geometry resistor family exactly the way ``klt
    extract --deck-option`` does (see that function's own ``deck_options``
    docstring). Already validated by ``run_lvs`` (a JSON object of string
    pairs) before this call; an unrecognised key/value raises
    :class:`~klayout_tools.extract.ExtractError`, caught below and re-raised
    as :class:`LvsError` the same way an unknown deck name already is. The
    pre-extracted ``layout.netlist`` shape has no extraction step to forward
    ``deck_options`` into here -- ``run_lvs`` applies it directly to its own
    ``get_extraction_deck`` call instead (for ``device_classes`` and the
    deferred resistor ``fixed_offset_ohm`` correction).

    ``combine_devices`` (issue #559): when ``True`` (``options.combine_devices``
    in the caller's request), inline extraction defers each opted-in
    resistor device class's ``fixed_offset_ohm`` correction instead of
    applying it here -- ``run_lvs`` applies it itself, once, after
    ``Netlist.combine_devices()`` folds series-connected primitives into one
    device, so the fixed offset lands exactly once per logical device
    instead of once per drawn primitive. When ``False`` (the default,
    unchanged behavior), the correction is applied here as before -- there
    is no combine step for it to be over-counted by.

    For the inline-extraction (``layout.file``) shape this flag drives inline
    extraction's own deferral (``apply_resistor_fixed_offset=False``). For the
    pre-extracted (``layout.netlist``) shape there is no extraction to defer
    here, but the deferred-correction path in ``run_lvs`` still applies once
    ``layout.deck`` is supplied alongside ``layout.netlist`` (issue #585):
    ``run_lvs`` calls ``apply_resistor_fixed_offset_corrections`` post-combine
    for that shape too. That correction is only *correct* if the supplied
    SPICE was extracted with the offset deferred (``run_extract(...,
    apply_resistor_fixed_offset=False)``, or its CLI equivalent ``klt extract
    --defer-resistor-fixed-offset``, issue #588); a netlist extracted the
    default way
    already carries a per-primitive offset that cannot be selectively un-summed
    after the series fold.
    """
    import klayout.db as kdb

    has_file = "file" in layout_spec
    has_netlist = "netlist" in layout_spec
    if has_file and has_netlist:
        raise LvsError("request.layout must have exactly one of 'file' or 'netlist'")
    if not has_file and not has_netlist:
        raise LvsError("request.layout requires 'file' or 'netlist'")

    if has_file:
        layout_file = _resolve_relative(layout_spec["file"], request_dir)
        if not os.path.isfile(layout_file):
            raise LvsError(f"layout file not found: {layout_file}")
        deck_name = layout_spec.get("deck")
        if not deck_name:
            raise LvsError("request.layout.deck is required when layout.file is given")

        # Issue #291: `top_cell_pins` keeps nets named only by a label inside an
        # instanced sub-cell internal, instead of promoting them to top-level
        # pins the reference netlist would then have to declare as ports. LVS is
        # topological, so the emitted extraction warning is not surfaced here
        # (the flag is the fix); the pin counts simply match without polluting
        # the reference interface.
        top_cell_pins_only = bool(layout_spec.get("top_cell_pins", False))

        # Issue #514: `declared_pins` is the per-*net* analogue of
        # `top_cell_pins` above -- every promoted pin not named in this set
        # is demoted back to an internal net (it keeps its name). Naming an
        # internal node of a lumped schematic device (e.g. one tap of a
        # metal-option ladder) for documentation no longer promotes it to a
        # pin `options.combine_devices` cannot fold through.
        declared_pins_spec = layout_spec.get("declared_pins")
        declared_pins: frozenset[str] | None = None
        if declared_pins_spec is not None:
            if not isinstance(declared_pins_spec, list) or not all(
                isinstance(name, str) for name in declared_pins_spec
            ):
                raise LvsError(
                    "request.layout.declared_pins must be a list of net name strings"
                )
            declared_pins = frozenset(declared_pins_spec)
            if not declared_pins:
                raise LvsError(
                    "request.layout.declared_pins must not be empty when given "
                    "-- omit the field entirely to keep every named net promoted"
                )

        # Issue #1513: `pin_source_cells` is the *positional* counterpart to
        # `declared_pins` above -- a set of cell names whose own drawn
        # pin-name labels (anywhere in the hierarchy, at any depth) are
        # resolved to their real net by probing each label's own position,
        # not by matching a promoted net's string. See `klt extract
        # --pin-source-cells`'s own docstring (`extract.py`) for the full
        # rationale (a `klt gen-compose`d assembly with no governing
        # top-level DEF, where neither `top_cell_pins`/`declared_pins`'s
        # per-cell-depth/per-net-string matching can cleanly isolate the
        # design's own genuine top-level ports).
        pin_source_cells_spec = layout_spec.get("pin_source_cells")
        pin_source_cells: frozenset[str] | None = None
        if pin_source_cells_spec is not None:
            if not isinstance(pin_source_cells_spec, list) or not all(
                isinstance(name, str) for name in pin_source_cells_spec
            ):
                raise LvsError(
                    "request.layout.pin_source_cells must be a list of cell "
                    "name strings"
                )
            pin_source_cells = frozenset(pin_source_cells_spec)
            if not pin_source_cells:
                raise LvsError(
                    "request.layout.pin_source_cells must not be empty when "
                    "given -- omit the field entirely to skip this "
                    "reconciliation"
                )
        try:
            # LVS is topological -- no parasitics_deck, so the 5th return
            # (parasitic_nets) is always None here and is ignored. The 6th
            # return (black_box_regions, issue #293) still takes effect --
            # any reserved-annotation-layer region in the layout is excluded
            # from connectivity the same as `klt extract` -- but is not
            # surfaced in `klt lvs`'s own response, out of scope here. The 7th
            # return (dummy_devices_dropped, #295) is a report-only count
            # surfaced by `klt extract`; the compare only cares that dummy
            # gates never became devices, which the suppression already
            # guarantees. The 8th return (unmodelled_poly, #324) is likewise a
            # report-only structured view of `klt extract`'s own warnings,
            # not surfaced in `klt lvs`'s response. The 9th return
            # (voltage_domain_warnings, #552) is likewise a report-only
            # structured view of `klt extract`'s own warnings, not surfaced
            # in `klt lvs`'s response -- the compare itself is unaffected by
            # which voltage-domain model a MOS device happens to bind to.
            # The 10th return (abstracted_cells, #620) is `klt extract
            # --abstract-cells`'s own report; `klt lvs` never passes that
            # flag, so it is always an empty list here. The 11th return
            # (dead_metal, #676) is likewise a report-only structured view of
            # `klt extract`'s own warnings -- routing geometry on no net
            # cannot change which devices/nets the compare below sees. The
            # 12th return (mom_crosscheck, #798) is `klt extract
            # --mom-net`'s own report; `klt lvs` never passes that flag (LVS
            # is topological, parasitics-free), so it is always `None` here.
            # The 13th return preserves original labels for supply-
            # fragmentation findings, including a supply label joined
            # with another alias on the same physical net. The 14th return
            # (device_instance_paths, #1666) likewise feeds `klt extract`'s
            # own `devices[].instance_path`; `klt lvs` compares by device
            # class/parameter equivalence, not by originating GDS-level
            # instance, so it has no use here either.
            (
                netlist,
                top_cell_name,
                _dbu_um,
                _warnings,
                _parasitics,
                _black_box_regions,
                _dummy,
                _unmodelled_poly,
                _voltage_domain_warnings,
                _abstracted_cells,
                _dead_metal,
                _mom_crosscheck,
                _net_label_positions,
                _device_instance_paths,
            ) = extract_netlist_from_layout(
                layout_file,
                deck_name,
                top=layout_spec.get("top"),
                top_cell_pins_only=top_cell_pins_only,
                declared_pins=declared_pins,
                # Issue #1513: `request.layout.pin_source_cells` -- the
                # positional counterpart to `declared_pins` above. `None`
                # when the field was never given, unchanged from every
                # request that predates it.
                pin_source_cells=pin_source_cells,
                # Issue #559: defer the resistor `fixed_offset_ohm`
                # correction when `combine_devices` will run -- applying it
                # here, before the fold, would have KLayout's native series
                # combine sum each drawn primitive's already-corrected R,
                # over-counting the fixed offset once per primitive instead
                # of once per logical device. `run_lvs` applies it itself,
                # once, right after combining (see the `combine_devices`
                # block below).
                apply_resistor_fixed_offset=not combine_devices,
                # Issue #600: `request.layout.deck_options` -- selects a
                # caller-visible flavour of a shared-geometry device family
                # (e.g. gf180mcu's `poly_res`), mirroring `klt extract
                # --deck-option`. `None` when the field was never given,
                # unchanged from every request that predates it.
                deck_options=deck_options,
            )
        except ExtractError as exc:
            raise LvsError(str(exc)) from exc

        extracted_netlist_path: str | None = None
        if keep_extracted:
            extracted_netlist_path = os.path.join(
                request_dir, ".klt", "lvs", f"{top_cell_name}.spice"
            )
            os.makedirs(os.path.dirname(extracted_netlist_path), exist_ok=True)
            writer = kdb.NetlistSpiceWriter()
            writer.use_net_names = True
            try:
                netlist.write(
                    extracted_netlist_path,
                    writer,
                    f"extracted by klt lvs (deck {deck_name})",
                )
            except Exception as exc:
                raise LvsError(
                    f"could not write extracted netlist "
                    f"'{extracted_netlist_path}': {exc}"
                ) from exc

        return (
            netlist,
            layout_spec["file"],
            layout_file,
            extracted_netlist_path,
            _net_label_positions,
        )

    layout_netlist_path = _require_path(layout_spec, "netlist", "layout", request_dir)
    # Issue #1876: recover a capacitor's real device-class name across the
    # bare-`C`-card round trip issue #1558 introduced -- see
    # `netlist_capacitor_recovery.py`'s module docstring. Falls back to
    # KLayout's own generic capacitor class exactly as before whenever the
    # recovery comment is missing or malformed.
    try:
        with open(layout_netlist_path, encoding="utf-8", errors="replace") as handle:
            layout_netlist_text = handle.read()
    except OSError as exc:
        raise LvsError(
            f"could not parse layout netlist '{layout_netlist_path}': {exc}"
        ) from exc
    recovered_capacitor_classes = parse_capacitor_class_comments(layout_netlist_text)
    # Issue #1942: recognise a round-tripped `X ... PARAMS:` card naming one
    # of `layout.deck`'s own custom (`GenericDeviceExtractor`-shaped) device
    # classes -- e.g. a MoM capacitor -- as a device of that class instead
    # of letting it degrade into a mangled-name abstract circuit. A best-
    # effort lookup only: an unresolvable `deck_name`/`deck_options` here is
    # deliberately swallowed rather than raised -- `run_lvs` re-resolves
    # `layout.deck` itself right after this function returns (see its own
    # `layout_deck` comment) and raises the authoritative `LvsError` there,
    # so this must not duplicate (and potentially reorder) that validation.
    # `None`/empty when `layout.deck` was never given, unchanged from every
    # request that predates this parameter.
    custom_device_classes: dict[str, str] = {}
    resistor_classes: dict[str, str] = {}
    deck_name = layout_spec.get("deck")
    if deck_name:
        try:
            layout_deck_for_recovery = get_extraction_deck(deck_name, deck_options)
        except (UnknownExtractionDeckError, InvalidDeckOptionError):
            layout_deck_for_recovery = None
        if layout_deck_for_recovery is not None:
            custom_device_classes = custom_device_classes_for_deck(
                layout_deck_for_recovery
            )
            # Issue #1157: recognise a round-tripped bulk-bearing drawn-
            # resistor class's `X` card (the bare-mode card shape ngspice
            # can parse) as a real 3-terminal resistor device again -- the
            # exact recovery the MoM-capacitor table above established,
            # keyed off `layout.deck`'s own `resistors` table.
            resistor_classes = resistor_classes_for_deck(layout_deck_for_recovery)
    netlist = kdb.Netlist()
    reader = make_capacitor_class_recovery_reader(
        recovered_capacitor_classes,
        custom_device_classes=custom_device_classes,
        resistor_classes=resistor_classes,
    )
    try:
        netlist.read(layout_netlist_path, reader)
    except Exception as exc:
        raise LvsError(
            f"could not parse layout netlist '{layout_netlist_path}': {exc}"
        ) from exc

    return netlist, layout_spec["netlist"], layout_netlist_path, None, {}


def _read_reference_netlist(
    path: str,
    *,
    form: str = "plain-element",
    deck: str | None = None,
    device_map: dict[str, object] | None = None,
    library: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    pin_orders: dict[str, list[str]] | None = None,
    placeholder_value_classes: dict[str, str] | None = None,
    gate_level_port_aliases: dict[str, dict[str, str]] | None = None,
) -> kdb.Netlist:
    """Parse ``path`` via ``NetlistSpiceReader``, in the reference netlist's
    declared ``form`` (issue #280, extended by issue #1336).

    ``form="plain-element"`` (default) reads the file as-is -- the
    schematic-equivalent form ``klt lvs`` requires (see ``docs/cli/lvs.md``).
    Before reading, it scans for the *simulation* (subcircuit-call) form and,
    if a curated PDK device (e.g. ``sky130_fd_pr__nfet_01v8``, ``nfet_03v3``)
    is instantiated via an undefined ``X`` subcircuit call, raises a specific
    :class:`LvsError` naming the form mismatch -- instead of letting the
    reader silently degrade the netlist into the confusing
    ``net.merged``/``topology`` cascade this issue describes.

    ``form="subckt-call"`` converts the file from the simulation form to the
    plain-element form first (see
    :mod:`klayout_tools.netlist_normalize`), resolving device names through the
    curated :mod:`klayout_tools.pdk_models` table (via ``deck`` and/or
    ``device_map``), then reads the converted text.

    ``placeholder_value_classes`` (issue #1907) is an optional *output*
    collector, populated only for ``form="subckt-call"``: the converter's own
    :attr:`~klayout_tools.netlist_normalize.ReferenceConversion.placeholder_value_classes`
    map, naming every emitted resistor/capacitor device class whose
    positional value token is the literal ``0`` placeholder rather than a
    real resistance/capacitance. ``run_lvs`` hands it to
    :func:`_apply_reference_placeholder_values`; every other caller can
    ignore it (``None``, the default, records nothing and leaves behaviour
    byte-identical).

    ``gate_level_port_aliases`` (issue #2021) is the same kind of optional
    *output* collector, populated only for ``form="gate-level-verilog"``:
    :func:`~klayout_tools.verilog_netlist.collect_gate_level_port_aliases`'s
    own ``{<module name>: {<port>: <canonical net>}}`` mapping, naming every
    declared port whose only Verilog-level connection is a plain ``assign``
    (e.g. a port-to-port alias, two reference port names for one electrical
    node). ``run_lvs`` hands it to :func:`_apply_gate_level_port_aliases`,
    which joins each alias port's net onto its canonical target's net in the
    ``kdb.Netlist`` this function returns, *before* the comparer runs --
    empty (the default, and the common case: most gate-level references
    have no port aliasing at all) leaves behaviour byte-identical.

    ``form="gate-level-verilog"`` (issue #1336) treats ``path`` as a `klt
    place-and-route` `verilog_path` gate-level Verilog netlist instead of
    SPICE, and converts it to plain-element-shaped SPICE first (see
    :mod:`klayout_tools.verilog_netlist`). ``library`` (required for this
    form) names the standard-cell library whose real
    ``libs.ref/<library>/spice/<library>.spice`` (or ``.../cdl/<library>.cdl``)
    file resolves each instantiated cell's pin order; ``pdk_variant``/
    ``pdk_root`` are forwarded to :func:`klayout_tools.pdk.find_pdk` exactly
    like `klt extract`'s own ``--pdk``/``--pdk-root`` flags.

    ``pin_orders`` (issue #1622) lets a caller that already resolved that
    library file -- ``run_lvs`` does, because
    :func:`_gate_level_power_pin_names` needs the same mapping -- pass it in
    rather than have it resolved and re-read here. Purely an optimisation:
    ``None`` (every other caller, e.g. :mod:`klayout_tools.netlist_digest`)
    resolves it internally exactly as before, and the value is ignored for
    every form but ``"gate-level-verilog"``.

    Note: on genuinely malformed input, ``NetlistSpiceReader`` does not raise
    -- it prints a ``"Warning: Line ignored..."`` diagnostic (to the
    process's real stdout, a pre-existing KLayout engine behaviour this
    module does not attempt to suppress) and returns an empty netlist (zero
    circuits). That surfaces reliably as :class:`LvsError` a moment later, in
    :func:`_select_circuit`'s "no top circuit" check -- exercised directly in
    ``tests/test_lvs.py``.
    """
    import klayout.db as kdb

    from .netlist_normalize import (
        NormalizeError,
        convert_reference_netlist,
        detect_subckt_call_devices,
    )

    try:
        # `errors="replace"` so a non-SPICE binary reference (a mis-pointed
        # path) never crashes the detection scan on a decode error -- it flows
        # through to `NetlistSpiceReader`, which tolerates garbage and yields
        # an empty netlist that surfaces as the "no top circuit" LvsError, the
        # pre-existing behaviour this hook preserves.
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise LvsError(f"could not read reference netlist '{path}': {exc}") from exc

    read_path = path
    # Issue #1876: the recovery scan below runs over whichever text is
    # actually handed to `NetlistSpiceReader` -- the original file for the
    # default (already-plain-element) form, or the *converted* text for the
    # two conversion forms, since a converted reference is not expected to
    # carry `klt extract`'s own device-instance comments but could, in
    # principle, if a caller's own upstream tooling produced one that does.
    read_text = text
    tmp_path: str | None = None

    if form == "subckt-call":
        try:
            conversion = convert_reference_netlist(
                text, deck=deck, device_map=device_map
            )
        except NormalizeError as exc:
            raise LvsError(
                f"could not convert subckt-call reference netlist "
                f"'{path}' to plain-element form: {exc}"
            ) from exc
        converted = conversion.text
        if placeholder_value_classes is not None:
            placeholder_value_classes.update(conversion.placeholder_value_classes)
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".spice", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(converted)
            tmp_path = tmp.name
        read_path = tmp_path
        read_text = converted
    elif form == "gate-level-verilog":
        if pin_orders is None:
            pin_orders, _ = _resolve_gate_level_pin_orders(
                library, pdk_variant, pdk_root
            )
        try:
            converted = convert_gate_level_verilog(
                text, pin_order_lookup=pin_orders.get
            )
        except VerilogNetlistError as exc:
            raise LvsError(
                f"could not convert gate-level-verilog reference netlist "
                f"'{path}' to plain-element form: {exc}"
            ) from exc
        if gate_level_port_aliases is not None:
            # Issue #2021: re-parses the same `text` `convert_gate_level_
            # verilog` just parsed successfully -- cheap (a single flat
            # module) and pure, kept as a separate entry point so the SPICE
            # conversion's own return type stays untouched (see
            # `collect_gate_level_port_aliases`'s docstring).
            gate_level_port_aliases.update(collect_gate_level_port_aliases(text))
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".spice", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(converted)
            tmp_path = tmp.name
        read_path = tmp_path
        read_text = converted
    else:
        offending = detect_subckt_call_devices(text)
        if offending:
            raise LvsError(
                f"reference netlist '{path}' is in the simulation "
                f"(subcircuit-call) form: it instantiates curated PDK device "
                f"subcircuit(s) {', '.join(offending)} via undefined 'X' "
                "cards, which 'klt lvs' cannot compare against extracted "
                "plain-element devices (it would silently degrade into a "
                "net.merged/topology mismatch). Set request.reference.form to "
                '"subckt-call" to convert it automatically, or convert it to '
                "the plain-element form (M-card) before comparing -- see "
                'docs/cli/lvs.md, "Netlist form".'
            )

    netlist = kdb.Netlist()
    recovered_capacitor_classes = parse_capacitor_class_comments(read_text)
    # Issue #1942: the same round-tripped custom-device-class recognition
    # `_resolve_layout` wires for `layout.deck` above, keyed off
    # `reference.deck` instead -- a best-effort lookup: an unresolvable
    # `deck` name here is deliberately swallowed (not raised) since this
    # function's *existing* `deck`/`device_map` resolution (the
    # `form="subckt-call"` conversion above) already owns raising the
    # authoritative error for a bad `reference.deck`. `{}` when `deck` was
    # never given, unchanged from every request that predates this
    # parameter.
    custom_device_classes: dict[str, str] = {}
    resistor_classes: dict[str, str] = {}
    if deck:
        try:
            reference_deck_for_recovery = get_extraction_deck(deck)
        except (UnknownExtractionDeckError, InvalidDeckOptionError):
            reference_deck_for_recovery = None
        if reference_deck_for_recovery is not None:
            custom_device_classes = custom_device_classes_for_deck(
                reference_deck_for_recovery
            )
            # Issue #1157: the reference side gets the same drawn-resistor
            # `X`-card recovery the layout side wired above -- a bare-mode
            # extract output is a legitimate reference netlist too.
            resistor_classes = resistor_classes_for_deck(reference_deck_for_recovery)
    reader = make_capacitor_class_recovery_reader(
        recovered_capacitor_classes,
        custom_device_classes=custom_device_classes,
        resistor_classes=resistor_classes,
    )
    try:
        netlist.read(read_path, reader)
    except Exception as exc:
        raise LvsError(f"could not parse reference netlist '{path}': {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return netlist


#: `<library>`'s pin-order source, in resolution order (issue #1336) --
#: mirrors the real open_pdks/volare/ciel layout `klt pdk`'s own `libs_ref`
#: resolution already assumes (see `klayout_tools.pdk`'s module docstring).
#: `.cdl` is a fallback, not a second convention: gf180mcu ships both views
#: with byte-identical `.subckt` headers (verified against a real installed
#: `gf180mcuC` variant), and a library that ships only one of the two (e.g.
#: sky130, `.spice` only) still resolves.
_LIBRARY_PIN_ORDER_ASSETS: tuple[tuple[str, str], ...] = (
    ("spice", "spice"),
    ("cdl", "cdl"),
)


def _resolve_gate_level_pin_orders(
    library: str | None,
    pdk_variant: str | None,
    pdk_root: str | None,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """``({<cell>: [<pin>, ...]}, pdk_info)`` -- every standard cell's real,
    full PDK pin order (signal *and* power/ground), read from a resolved PDK
    install's own ``libs.ref/<library>/{spice,cdl}/<library>.{spice,cdl}``
    file (issue #1336) -- never a hardcoded pin-order table.

    Two consumers, one read of that file (issue #1622): ``.get`` on the pin-
    order mapping is the ``pin_order_lookup`` callback
    :func:`klayout_tools.verilog_netlist.convert_gate_level_verilog` wants,
    while :func:`_gate_level_power_pin_names` needs the whole mapping, to
    derive which of a cell's real PDK pins the conversion dropped.

    Resolves the PDK exactly like `klt extract --pdk`/`klt place-and-route`
    do (:func:`klayout_tools.pdk.find_pdk`, the same ``variant``/``root``
    resolution order documented in ``docs/cli/pdk.md``), then reads whichever
    of that library's ``spice/<library>.spice`` / ``cdl/<library>.cdl`` files
    exists first (see :data:`_LIBRARY_PIN_ORDER_ASSETS`). The resolved
    ``pdk_info`` (the same :func:`~klayout_tools.pdk.find_pdk`-shaped dict) is
    returned alongside the pin-order mapping (issue #1901) so ``run_lvs`` can
    record it in ``provenance.pdk`` without a second, redundant resolution.
    Raises :class:`LvsError` -- never lets a lower-level exception escape
    this command's JSON-envelope contract -- when the PDK does not resolve,
    the resolved variant ships no ``libs_ref`` asset at all, or neither file
    exists for ``library``.
    """
    if not library:
        raise LvsError(
            'request.reference.library is required with form: "gate-level-verilog"'
        )
    try:
        pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
    except PdkNotFoundError as exc:
        raise LvsError(str(exc)) from exc

    libs_ref = pdk_info["assets"]["libs_ref"]
    if libs_ref is None:
        raise LvsError(
            f"PDK variant '{pdk_info['variant']}' (root '{pdk_info['root']}') "
            "ships no 'libs.ref' asset -- cannot resolve "
            f"'{library}''s pin order"
        )

    lib_dir = os.path.join(libs_ref, library)
    tried: list[str] = []
    for subdir, extension in _LIBRARY_PIN_ORDER_ASSETS:
        candidate = os.path.join(lib_dir, subdir, f"{library}.{extension}")
        tried.append(candidate)
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8", errors="replace") as handle:
                    library_text = handle.read()
            except OSError as exc:
                raise LvsError(
                    f"could not read library pin-order source '{candidate}': {exc}"
                ) from exc
            return parse_subckt_pin_orders(library_text), pdk_info

    raise LvsError(
        f"library '{library}' has no pin-order source under PDK variant "
        f"'{pdk_info['variant']}' -- tried: {', '.join(tried)}"
    )


def _parse_combine_devices(options: Mapping[str, Any]) -> bool | list[str]:
    """Resolve ``options.combine_devices`` into either a bool (the original
    shape, unchanged) or a normalised list of device-class names (issue
    #1370).

    The list shape restricts combining to only the named device classes --
    the escape hatch for a netlist where KLayout's own
    ``Netlist.combine_devices()`` trips its internal-consistency invariant
    *deterministically* on one device class's partial-match group, leaving
    :func:`_combine_devices_safely`'s retry budget (issue #1185) with nothing
    to resample. Naming only the classes that actually need folding lets the
    compare the caller wanted run at all, instead of degrading the whole run.

    A wrong-shaped value is a clean request error rather than a silent
    coercion, mirroring how ``layout.declared_pins``/``layout.deck_options``
    are validated: ``bool(...)`` would happily turn ``"nfet"``, ``0``, or
    ``{}`` into a verdict-shaping flag the caller never meant. An empty list
    is rejected for the same reason ``declared_pins`` rejects one -- it reads
    as "combine nothing", which is what ``false`` already means, so it is far
    more likely a mistake than an intent.
    """
    value = options.get("combine_devices", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        if not value or not all(
            isinstance(name, str) and name.strip() for name in value
        ):
            raise LvsError(
                "options.combine_devices, when given as a list, must be a "
                "non-empty list of non-empty device-class name strings (e.g. "
                '["NMOS", "PMOS"]) -- use `false` to disable combining '
                "entirely"
            )
        return [name.strip() for name in value]
    raise LvsError(
        "options.combine_devices must be a boolean or a list of device-class "
        f"name strings; got {type(value).__name__}"
    )


def _parse_combine_devices_max_attempts(options: Mapping[str, Any]) -> int:
    """Resolve ``options.combine_devices_max_attempts`` into a positive int,
    defaulting to :data:`_COMBINE_DEVICES_MAX_ATTEMPTS` when omitted (issue
    #1412).

    Exposes :func:`_combine_devices_safely`'s retry budget -- previously a
    private module constant -- as a caller-configurable knob. That constant's
    own derivation (see its docstring) was tuned against one small fixture (a
    single multi-finger NMOS device, ~200 gate fingers, one device class);
    a caller re-measuring against a much larger, multi-class netlist has
    reported the retry-exhaustion fallback (``device.combine_incomplete``)
    firing far more often than that derivation would predict (see
    ``docs/cli/lvs.md``'s ``options.combine_devices`` section for the
    reported observation) -- this option lets such a caller raise the budget
    without a fork, trading runtime for a lower observed exhaustion rate, and
    lets a caller iterating on a small netlist lower it for faster feedback.

    Malformed input raises :class:`LvsError` rather than silently falling
    back to the default, matching this module's other request-side options
    (``options.parameter_tolerance``, ``options.combine_devices`` itself): a
    retry budget the caller believes is in force but is not would defeat the
    whole point of exposing it.
    """
    if "combine_devices_max_attempts" not in options:
        return _COMBINE_DEVICES_MAX_ATTEMPTS
    value = options["combine_devices_max_attempts"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise LvsError(
            "options.combine_devices_max_attempts must be a positive integer; "
            f"got {value!r}"
        )
    if value < 1:
        raise LvsError(
            f"options.combine_devices_max_attempts must be at least 1; got {value!r}"
        )
    return value


def _parse_combine_devices_per_circuit(
    options: Mapping[str, Any],
) -> dict[str, bool] | None:
    """Resolve ``options.combine_devices_per_circuit`` (issue #1552) into an
    ordered ``{<circuit-name-glob>: <bool>}`` mapping, or ``None`` when the
    key is absent.

    Lets a composed design give each of its own subcircuits its own
    ``combine_devices`` choice -- honored *simultaneously*, unlike the
    single whole-request ``options.combine_devices`` boolean/list this
    module has offered since issue #261, which cannot satisfy two macros
    with opposing needs once they are composed into one top-level design:
    one macro's drawn split/interleaved device legs must be re-lumped
    (``true``) to match a lumped schematic reference, while another macro's
    large group of nominally-identical parallel devices must stay
    individually reported (``false``) to avoid issue #1497's silent
    parameter-corruption risk at scale -- and a shared top-level rail
    connecting the two once composed is enough to make whole-netlist
    ``Netlist.combine_devices()`` treat devices from the *unrelated* macro
    as combine candidates too (see :func:`_combine_circuit_devices_safely`).

    Keys are ``fnmatch.fnmatchcase`` glob patterns matched against each
    side's own ``Circuit.name`` (case-sensitive -- the same convention
    ``extract_abstract.py``'s ``--abstract-cells`` glob matching uses).
    Circuit names read back through ``NetlistSpiceReader`` are upper-cased
    (e.g. a ``.subckt macroa`` declaration reads back as circuit name
    ``"MACROA"``), so a glob written in the source SPICE's own lower/mixed
    case will not match -- see
    :func:`_resolve_combine_devices_per_circuit_targets`. Applied in
    **declaration order** (Python/JSON object key order is preserved end to
    end): the first pattern that matches a given circuit name wins, so a
    caller can list specific circuit names ahead of a catch-all ``"*"`` to
    get "combine everything except these", or list only the circuits that
    need combining to get "combine nothing except these" (the default when
    no pattern matches a circuit at all).

    A wrong-shaped value is a clean request error, matching this module's
    other option-parsing convention (see :func:`_parse_combine_devices`):
    the value must be a non-empty JSON object whose keys are non-empty
    strings and whose values are booleans -- no per-circuit device-class
    restriction in this first increment. Mutually exclusive with a truthy
    ``options.combine_devices`` (enforced by ``run_lvs`` itself, not here,
    so the error names both option paths together).
    """
    if "combine_devices_per_circuit" not in options:
        return None
    value = options["combine_devices_per_circuit"]
    if not isinstance(value, dict) or not value:
        raise LvsError(
            "options.combine_devices_per_circuit must be a non-empty JSON "
            "object mapping a circuit-name glob (fnmatch pattern, matched "
            "case-sensitively) to a boolean"
        )
    result: dict[str, bool] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not key:
            raise LvsError(
                "options.combine_devices_per_circuit keys must be "
                "non-empty circuit-name glob strings"
            )
        if not isinstance(entry, bool):
            raise LvsError(
                f"options.combine_devices_per_circuit[{key!r}] must be a "
                f"boolean; got {type(entry).__name__}"
            )
        result[key] = entry
    return result


def _resolve_combine_devices_per_circuit_targets(
    per_circuit: Mapping[str, bool], netlist: kdb.Netlist
) -> tuple[list[str], list[str]]:
    """Resolve ``per_circuit``'s glob keys against ``netlist``'s own circuit
    names (issue #1552), returning ``(to_combine, unmatched_patterns)``.

    ``to_combine`` is the ordered list of this netlist's own circuit names
    whose first matching glob (in ``per_circuit``'s own declaration order --
    see :func:`_parse_combine_devices_per_circuit`) mapped to ``True``.
    ``unmatched_patterns`` is every glob in ``per_circuit`` that matched zero
    circuits on this netlist -- surfaced by the caller as a
    ``combine_devices_per_circuit.unmatched`` warning so a typo'd circuit
    name (most commonly: written in the request's own source-file case
    instead of the upper-cased form ``NetlistSpiceReader`` reads circuit
    names back as) silently restricting to nothing is visible, mirroring
    :func:`_validate_combine_device_classes`'s equivalent guard for
    ``options.combine_devices``'s list shape -- except a pattern legitimately
    naming a circuit that exists on only one side (e.g. a reference-only
    lumped macro with no layout-side counterpart yet) is not itself a
    mistake, so this only warns, it never raises.
    """
    to_combine: list[str] = []
    matched: dict[str, bool] = dict.fromkeys(per_circuit, False)
    for circuit in netlist.each_circuit():
        for pattern, enabled in per_circuit.items():
            if fnmatch.fnmatchcase(circuit.name, pattern):
                matched[pattern] = True
                if enabled:
                    to_combine.append(circuit.name)
                break
    unmatched_patterns = [pattern for pattern, hit in matched.items() if not hit]
    return to_combine, unmatched_patterns


def _unmatched_combine_devices_per_circuit_warnings(
    unmatched_patterns: Sequence[str], side: str
) -> list[dict[str, Any]]:
    """Build the ``combine_devices_per_circuit.unmatched`` warning entries
    for ``unmatched_patterns`` (issue #1552) -- see
    :func:`_resolve_combine_devices_per_circuit_targets`."""
    return [
        _mismatch(
            CATEGORY_COMBINE_DEVICES_PER_CIRCUIT_UNMATCHED,
            "warning",
            f"options.combine_devices_per_circuit pattern {pattern!r} "
            f"matched no circuit on the {side} netlist -- check the pattern "
            "against this side's own circuit names (NetlistSpiceReader "
            "upper-cases circuit names read back from SPICE, e.g. "
            '"macroa" -> "MACROA")',
            side,
            details={"pattern": pattern},
        )
        for pattern in unmatched_patterns
    ]


def _combine_circuit_devices_safely(
    netlist: kdb.Netlist,
    side: str,
    circuit_names: Sequence[str],
    max_attempts: int = _COMBINE_DEVICES_MAX_ATTEMPTS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Like :func:`_combine_devices_safely`, but combines only the named
    circuits, one at a time, via ``Circuit.combine_devices()`` rather than
    the whole-netlist ``Netlist.combine_devices()`` -- issue #1552's
    ``options.combine_devices_per_circuit``.

    Combining one circuit at a time, each scoped to its own
    ``Circuit.combine_devices()`` call, means devices in a circuit named
    ``False`` (or not named at all) can never be treated as combine
    candidates alongside devices in a *different* circuit just because a
    shared top-level rail happens to connect them once both are wired into
    one composed design -- the core failure mode #1552 reports against the
    whole-netlist ``Netlist.combine_devices()`` once
    ``options.flatten_layout``/``options.flatten_reference`` (issue #1085)
    collapses circuit boundaries away. This is why ``run_lvs`` calls this
    *before* either flatten runs, while circuit boundaries still exist to
    scope against.

    Each named circuit gets its own bounded retry against fresh
    ``Netlist.dup()`` copies of the *whole* netlist (issue #1185's
    nondeterminism mitigation, scoped down to run per-circuit instead of
    once for the whole netlist): a ``RuntimeError`` combining one circuit
    never discards another circuit's already-succeeded combine, and each
    circuit's own retry budget is independent. Unlike
    :func:`_combine_devices_safely`'s whole-netlist symmetric degrade
    (issue #1370), a circuit that exhausts its retry budget here is simply
    left uncombined and reported -- there is no "roll both sides back"
    behaviour here, because each circuit's own combine choice is already an
    independent, caller-declared decision, not a single request-wide flag
    both sides must agree on. Unlike :func:`_combine_devices_safely`, the
    caught ``RuntimeError`` is not narrowed to
    :data:`_COMBINE_DEVICES_ERROR_MARKER` (that marker text is
    ``Netlist.combine_devices()``'s own; whether ``Circuit.combine_devices()``
    raises byte-identical text is unconfirmed) -- any ``RuntimeError`` here
    is presumed to be this same KLayout-internal partial-match invariant,
    since nothing else in this narrowly-scoped call can raise one.

    Returns ``(warnings, combined_circuit_names)``: ``warnings`` is the list
    of ``device.combine_incomplete`` warnings (one per circuit that
    exhausted its retry budget) to append to ``mismatches[]`` -- empty when
    every named circuit combined cleanly (the common case).
    ``combined_circuit_names`` is the subset of ``circuit_names`` that
    combined *cleanly* (no retry-budget exhaustion) -- issue #1557: this is
    what ``run_lvs`` scopes its post-combine resistor
    ``fixed_offset_ohm``/capacitor ``C`` sum-conservation corrections to, so
    a circuit whose combine was reported incomplete is never "corrected"
    against a fold that never actually completed, mirroring the
    whole-netlist ``combine_devices`` path's own ``if not
    combine_incomplete:`` gating (see ``run_lvs``).
    """
    warnings: list[dict[str, Any]] = []
    combined_circuit_names: list[str] = []
    other_side = "reference" if side == "layout" else "layout"
    for name in circuit_names:
        for attempt in range(max_attempts):
            is_last_attempt = attempt == max_attempts - 1
            # Every attempt but the last runs against a fresh, independent
            # whole-netlist copy -- `netlist` itself is never mutated until
            # either a copy succeeds (assigned back below) or the last
            # attempt runs directly against it, mirroring
            # `_combine_devices_safely`'s own retry discipline.
            candidate = netlist if is_last_attempt else netlist.dup()
            circuit = candidate.circuit_by_name(name)
            if circuit is None:  # pragma: no cover -- defensive, shouldn't happen
                break
            try:
                circuit.combine_devices()
            except RuntimeError as exc:
                if not is_last_attempt:
                    continue
                attempts_disclosure = (
                    f" after {max_attempts} attempts against independent "
                    "netlist copies (issue #1185)"
                    if max_attempts > 1
                    else ""
                )
                warnings.append(
                    _mismatch(
                        CATEGORY_DEVICE_COMBINE_INCOMPLETE,
                        "warning",
                        "options.combine_devices_per_circuit could not fully "
                        f"combine devices in circuit {name!r} on the {side} "
                        f"netlist{attempts_disclosure}: KLayout's "
                        "Circuit.combine_devices() hit an internal-"
                        "consistency error on a partial-match device group "
                        "(instances sharing only some, not all, of their "
                        "matching terminals) and stopped -- devices already "
                        "combined in this circuit before the error remain "
                        f"combined, the rest were left uncombined ({exc})",
                        side,
                        circuit={side: name, other_side: None},
                        details={"klayout_error": str(exc)},
                    )
                )
            else:
                if candidate is not netlist:
                    netlist.assign(candidate)
                combined_circuit_names.append(name)
                break
    return warnings, combined_circuit_names


def _validate_combine_device_classes(
    class_names: list[str],
    netlists: Mapping[str, kdb.Netlist],
) -> None:
    """Reject a list-valued ``options.combine_devices`` naming a device class
    that exists on **neither** side (issue #1370).

    Without this, a typo (``"nfet_o1v8"`` for ``"nfet_01v8"``) silently
    restricts combining to nothing at all and the run reports a full
    ``device.unmatched`` cascade that looks exactly like a real design error
    -- the "silently no-op" failure mode this option exists to avoid. Matched
    case-insensitively against every device class registered on either
    netlist, because ``NetlistSpiceReader`` upper-cases class names read back
    from SPICE (``RES_HIGH_PO``) while an in-process extraction deck writes
    them as declared (``res_high_po``) -- the same case-insensitive
    convention :func:`~klayout_tools.extract.apply_resistor_fixed_offset_corrections`
    already uses (issue #585).

    Only "on neither side" is an error: a class present on just one side is
    legitimate (a layout-only parasitic flavour, a reference-only lumped
    model), and restricting to it there is exactly the asymmetry a caller
    reaching for this option may be trying to express.
    """
    known: dict[str, str] = {}
    for netlist in netlists.values():
        for device_class in netlist.each_device_class():
            known.setdefault(device_class.name.lower(), device_class.name)
    unknown = [name for name in class_names if name.lower() not in known]
    if not unknown:
        return
    available = ", ".join(sorted(known.values())) or "(none)"
    raise LvsError(
        "options.combine_devices names device class(es) present in neither "
        f"the layout nor the reference netlist: {', '.join(sorted(unknown))} "
        f"-- device classes available across both sides: {available}"
    )


def _restrict_device_combination(netlist: kdb.Netlist, class_names: list[str]) -> None:
    """Disable device combination on every device class of ``netlist`` that
    is *not* named in ``class_names`` (issue #1370).

    ``klayout.db.Netlist.combine_devices()`` takes no arguments and offers no
    per-class filter (verified against ``klayout==0.30.10``), but each
    ``DeviceClass`` carries the two predicates its combination passes consult
    -- ``supports_parallel_combination`` and ``supports_serial_combination``.
    Clearing both on the other classes makes KLayout's own pass skip them, so
    only the named classes are folded. Matched case-insensitively for the
    same ``NetlistSpiceReader``-upper-cases-class-names reason
    :func:`_validate_combine_device_classes` documents.

    Deliberately one-way: both predicates are **write-only** in KLayout's
    Python bindings (reading either raises ``AttributeError: ... is not
    readable``), so their prior values cannot be saved and restored. That is
    safe here because the only consumers of these predicates are
    ``Circuit::combine_devices()`` -- which is exactly what this is scoping --
    and the device *extractor*, which has already finished by the time
    ``run_lvs`` reaches this point. ``NetlistComparer`` never reads them, and
    every netlist this touches is a per-run, in-memory object discarded when
    ``run_lvs`` returns. ``run_lvs`` additionally holds an untouched
    ``Netlist.dup()`` snapshot of each side taken *before* combining, which is
    what the symmetric degrade restores, so even the predicates come back
    unmodified on that path.
    """
    wanted = {name.lower() for name in class_names}
    for device_class in netlist.each_device_class():
        if device_class.name.lower() in wanted:
            continue
        device_class.supports_parallel_combination = False
        device_class.supports_serial_combination = False


def _combine_failure_identifiers(
    netlist: kdb.Netlist, side: str, message: str
) -> dict[str, Any]:
    """Turn KLayout's own ``combine_devices()`` internal-consistency error
    *message* into the machine-readable ``circuit``/``device``/``net``
    ``mismatches[]`` fields (issue #1370), resolving each against ``netlist``
    where the message alone is not enough.

    The message embeds three fields --
    ``name=<device>, circuit=<circuit>, terminal=<terminal>`` (see
    :data:`_COMBINE_DEVICES_ERROR_FIELDS_RE`) -- which are parsed and then
    resolved as far as the (partially combined) netlist allows:

    * ``circuit`` -- taken straight from the message, on ``side``'s key.
    * ``device`` -- the named device, when KLayout printed a name *and* that
      device is still present in the named circuit; its ``class`` is that
      device's device-class name.
    * ``net`` -- the net wired to the reported terminal of that device.

    KLayout frequently reports an **empty** ``name=`` (the device is mid-
    removal when the invariant trips, so there is nothing to print). In that
    case the device's own name is unrecoverable, but the *class* usually is
    not: if exactly one device class present in the named circuit declares a
    terminal with the reported name, that class is the failing group's class,
    and it is reported as ``device.class`` with both name keys ``null`` (the
    same shape ``device.body_unverified`` already uses for a class-level,
    instance-less finding). Anything that cannot be resolved this way stays
    ``null`` -- never guessed.

    Returns the ``_mismatch`` keyword arguments as a dict, always including
    a ``details`` block with the parsed terminal name and KLayout's raw
    message, so a caller has the unparsed original too.
    """
    match = _COMBINE_DEVICES_ERROR_FIELDS_RE.search(message)
    device_name = (match.group("device").strip() if match else "") or None
    circuit_name = (match.group("circuit").strip() if match else "") or None
    terminal_name = (match.group("terminal").strip() if match else "") or None

    other = "reference" if side == "layout" else "layout"
    circuit_field: dict[str, Any] | None = None
    device_field: dict[str, Any] | None = None
    net_field: dict[str, Any] | None = None

    circuit = netlist.circuit_by_name(circuit_name) if circuit_name else None
    if circuit_name:
        circuit_field = {side: circuit_name, other: None}

    device = None
    if circuit is not None and device_name:
        device = circuit.device_by_name(device_name)
    if device is not None:
        device_class = device.device_class()
        device_field = {
            side: device.expanded_name(),
            other: None,
            "class": device_class.name,
        }
        if terminal_name:
            net = _net_for_terminal_or_none(device, terminal_name)
            if net is not None:
                net_field = {side: net.expanded_name(), other: None}
    elif circuit is not None and terminal_name:
        class_names = _device_classes_declaring_terminal(circuit, terminal_name)
        if len(class_names) == 1:
            device_field = {side: None, other: None, "class": class_names[0]}

    return {
        "circuit": circuit_field,
        "device": device_field,
        "net": net_field,
        "details": {"terminal": terminal_name, "klayout_error": message},
    }


def _net_for_terminal_or_none(device: Any, terminal_name: str) -> Any:
    """``device.net_for_terminal(terminal_name)``, or ``None`` when this
    device's class declares no such terminal (KLayout raises rather than
    returning ``nil`` for an unknown terminal name)."""
    try:
        return device.net_for_terminal(terminal_name)
    except Exception:  # pragma: no cover -- defensive: unknown terminal name
        return None


def _device_classes_declaring_terminal(
    circuit: kdb.Circuit, terminal_name: str
) -> list[str]:
    """The distinct device-class names used by devices in ``circuit`` that
    declare a terminal called ``terminal_name`` -- the fallback identifier
    :func:`_combine_failure_identifiers` uses when KLayout reported an empty
    device ``name=``. Sorted for determinism."""
    names: set[str] = set()
    for device in circuit.each_device():
        device_class = device.device_class()
        if any(
            terminal.name == terminal_name
            for terminal in device_class.terminal_definitions()
        ):
            names.add(device_class.name)
    return sorted(names)


def _combine_devices_safely(
    netlist: kdb.Netlist,
    side: str,
    max_attempts: int = _COMBINE_DEVICES_MAX_ATTEMPTS,
    *,
    device_classes: list[str] | None = None,
) -> dict[str, Any] | None:
    """Call ``netlist.combine_devices()``, retrying against independent
    netlist copies to counter its run-to-run nondeterminism, and degrading
    gracefully instead of letting KLayout's internal-consistency
    ``RuntimeError`` abort the whole ``klt lvs`` run when every attempt is
    unlucky (issues #466, #1185).

    KLayout's own ``Netlist.combine_devices()`` can raise::

        RuntimeError: Internal error: Terminal still connected after
        removing device in device combination: name=, circuit=<top>,
        terminal=E in Netlist.combine_devices

    on a *partial-match* device group: N real (matching-relevant) instances
    plus M dummy instances that all share two of three terminals (e.g. a
    bipolar device's base and collector, tied to an array's common well and
    substrate), but only the N real instances additionally share the third
    (e.g. an emitter bussed to one signal net) -- the M dummy instances each
    have their own, mutually distinct, third terminal. That is a
    ``klayout.db`` behavior this module merely surfaces, not a defect this
    module's own code introduces, so it is not this module's job to make the
    partial-match combine itself succeed -- only to keep an unhandled
    internal exception from breaking this command's JSON-envelope contract
    (``docs/json-contract.md``, CLAUDE.md's "JSON is the contract").

    **Why this retries (issue #1185):** the same layout GDS + reference
    netlist pair can flip between a clean combine and this exact
    ``RuntimeError`` across separate ``klt lvs`` invocations, even though
    nothing about the inputs changed. The root cause is internal to
    KLayout's C++ implementation, not this module or Python's own hashing
    (:data:`_COMBINE_DEVICES_MAX_ATTEMPTS`'s docstring has the full
    investigation) -- and it is not something this module can override or
    reorder from the Python API. What it *can* do is resample: each retry
    below runs ``combine_devices()`` against its own ``Netlist.dup()`` copy
    of the untouched, not-yet-combined netlist, so a failure on one attempt
    does not poison the next -- every attempt is an independent trial of
    KLayout's internal (address-dependent) ordering. The first attempt that
    completes without raising is adopted (via ``Netlist.assign()``) as this
    function's result; note this **replaces** ``netlist``'s circuits in
    place, so any ``kdb.Circuit`` reference a caller took from ``netlist``
    *before* calling this function is invalidated the moment a retry
    succeeds and must be re-fetched afterwards (e.g. via
    ``netlist.circuit_by_name(name)``) -- ``run_lvs`` does this immediately
    after both sides' calls to this function.

    **Why the first attempt runs in place (issue #2374):** a ``dup()`` copy is
    not merely an *independent* trial of KLayout's address-dependent ordering
    -- it is a *worse* one. A freshly allocated copy walks its nets in an
    order that bears no relation to the order they were created in, and on
    roughly a quarter of copies that lands ``combine_devices()`` in a
    different accumulation branch that returns **normally** with silently
    wrong combined parameters (issue #2374 measured ~24% of ``dup()`` copies
    of a ``DeviceClassBJT3Transistor`` array producing an *inverted*
    parameter accumulation, versus 1000/1000 correct runs against the
    netlist itself). So attempt 0 runs directly against ``netlist``, exactly
    as this module behaved before issue #1185 added the retry, and only the
    *retries* -- reached solely when attempt 0 raised the ``RuntimeError``
    above -- resample against independent copies. Those copies are taken from
    a pristine ``snapshot`` captured before attempt 0 ran, not from the
    partially-merged ``netlist``, so every retry is still a genuinely
    independent trial rather than a retry-from-partial-damage. (The residual
    exposure on the retry path is covered separately, and independently of
    *why* KLayout got it wrong, by :func:`_correct_combine_parameters`'s
    post-combine conservation check.)

    Because attempt 0 runs in place, ``netlist`` keeps whatever that attempt
    managed to merge before hitting the error when *every* attempt fails --
    byte-for-byte the same shape issue #466 originally shipped: this
    netlist's remaining, not-yet-combined devices are simply left as
    individual devices for the rest of this run, exactly as they would be
    with ``options.combine_devices: false``. (``run_lvs`` no longer *ships*
    that partially-combined state to the comparer -- issue #1370's symmetric
    degrade rolls both sides back to their pre-combine snapshots when this
    function reports a failure -- but this function's own contract is
    unchanged, so a direct caller still gets exactly the issue #466 shape.)

    ``device_classes`` (issue #1370) restricts combining to the named device
    classes, via :func:`_restrict_device_combination` applied to whichever
    netlist this attempt runs against. ``None`` (the default) combines every
    class, i.e. the plain ``options.combine_devices: true`` behaviour.
    Applied per attempt rather than once up front so a retry against a fresh
    ``Netlist.dup()`` copy -- which carries its own, unrestricted copies of
    the device classes -- is restricted the same way the first attempt was.
    The pristine ``snapshot`` the retries are copied from is captured
    *before* that restriction is applied to ``netlist``, so a retry restricts
    its own copy from the same unrestricted starting point attempt 0 had.

    Returns a ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "device.combine_incomplete"``) to append to the report when
    every attempt hit the error, or ``None`` when some attempt (usually the
    first) completed cleanly. That entry carries the failing attempt's own
    ``circuit``/``device``/``net`` identifiers wherever KLayout's message and
    the netlist can supply them (issue #1370, see
    :func:`_combine_failure_identifiers`) -- not just the raw exception text
    in ``description``. Only catches this one KLayout-internal error
    shape (matched narrowly on ``_COMBINE_DEVICES_ERROR_MARKER``, the
    ``"...in Netlist.combine_devices"`` suffix every instance of it
    carries) -- any other ``RuntimeError``, on any attempt, propagates
    unchanged immediately (no retry), so an unrelated failure is never
    silently swallowed or masked as this issue's shape.
    """
    # Issue #2374: captured before attempt 0 mutates `netlist` in place, so
    # every *retry* below copies from the pristine pre-combine state rather
    # than from a partially-merged one. Skipped entirely when there is no
    # retry budget to spend (`max_attempts == 1`), which keeps the
    # single-attempt path allocation-for-allocation identical to the
    # pre-#1185 behaviour.
    snapshot = netlist.dup() if max_attempts > 1 else None
    last_error = ""
    for attempt in range(max_attempts):
        # Attempt 0 runs directly against `netlist` -- the ordering KLayout
        # produces for a netlist walked in the order it was built, which is
        # the one that combines correctly (issue #2374). Only the retries,
        # reached solely after attempt 0 raised, resample against independent
        # copies of the pristine snapshot (issue #1185's mitigation).
        # `snapshot` is never `None` here: `attempt > 0` implies
        # `max_attempts > 1`, which is exactly the condition it was taken
        # under.
        candidate = netlist if attempt == 0 or snapshot is None else snapshot.dup()
        if device_classes is not None:
            _restrict_device_combination(candidate, device_classes)
        try:
            candidate.combine_devices()
        except RuntimeError as exc:
            if _COMBINE_DEVICES_ERROR_MARKER not in str(exc):
                raise
            last_error = str(exc)
            continue
        else:
            if candidate is not netlist:
                netlist.assign(candidate)
            return None

    # Every attempt hit KLayout's partial-match error. `netlist` still
    # carries attempt 0's own in-place partial merge (issue #466's shape);
    # the identifiers below are therefore resolved against `netlist`, the
    # state the caller is actually left holding.
    attempts_disclosure = (
        f" after {max_attempts} attempts against independent netlist "
        "copies (issue #1185)"
        if max_attempts > 1
        else ""
    )
    restriction_disclosure = (
        f" (restricted to device class(es) {', '.join(device_classes)})"
        if device_classes is not None
        else ""
    )
    return _mismatch(
        CATEGORY_DEVICE_COMBINE_INCOMPLETE,
        "warning",
        "options.combine_devices could not fully combine devices on "
        f"the {side} netlist{restriction_disclosure}"
        f"{attempts_disclosure}: KLayout's "
        "Netlist.combine_devices() hit an internal-consistency error "
        "on a partial-match device group (instances sharing only "
        "some, not all, of their matching terminals) and stopped -- "
        "devices it had already combined before the error remain "
        "combined, but the rest of this netlist's devices were left "
        f"uncombined ({last_error})",
        side,
        # Issue #1370: machine-readable identifiers for the failure, resolved
        # against the netlist the caller keeps rather than left `None`.
        **_combine_failure_identifiers(netlist, side, last_error),
    )


#: Issues #1497/#2374: per-device-class parameter-conservation rules for
#: KLayout's own **parallel** `combine_devices()` fold, as
#: `(device-class attribute names, summed parameters, preserved parameters)`.
#: Each rule says what a combined device's parameters must be, given the
#: pre-combine group it absorbed:
#:
#: * a *summed* parameter's post-combine value is the pre-combine group's
#:   total (it is a conserved quantity -- combining can only redistribute it
#:   across fewer surviving devices, never change it);
#: * a *preserved* parameter's post-combine value is the group's single
#:   shared value (it describes a region the parallel instances have in
#:   common, so it does not accumulate) -- checked only when every member of
#:   the group actually agrees on it, since nothing can be asserted about a
#:   group whose members differ.
#:
#: Capacitors (issue #1497): `C` is a simple per-device sum under parallel
#: combination (`CapacitorDeviceCombiner::parallel` in KLayout's own
#: `dbNetlistDeviceClasses.cc` -- see also `extract.py`'s
#: `_apply_device_parameter_corrections` docstring, which asserts the same
#: for `DeviceExtractorCapacitor`-produced devices). `A`/`P` are secondary
#: and likewise sum, but issue #1497 only ever observed `C` going wrong, so
#: they are deliberately left unasserted here.
#:
#: Bipolars (issue #2374): a parallel `DeviceClassBJT3Transistor` array folds
#: with the emitter parameters summed (`AE`, `PE`, `NE`) and the shared
#: base/collector region parameters left at the array's common value (`AB`,
#: `PB`, `AC`, `PC`) -- verified directly against `klayout==0.30.10` by
#: combining an N-instance parallel array and reading the surviving device
#: back. `DeviceClassBJT4Transistor` (the substrate-terminal flavour)
#: subclasses `DeviceClassBJT3Transistor`, declares the identical parameter
#: set, and folds identically; it is listed explicitly anyway so the rule
#: does not silently depend on that subclass relationship.
#:
#: Deliberately narrow -- a device class is listed here only when *every*
#: combination KLayout will perform on it is a parallel one with a known
#: accumulation rule:
#:
#: * resistors: a parallel combination is not a simple sum
#:   (`1/R = 1/R1 + 1/R2`) and a series one is, so neither `DeviceClass
#:   Resistor` nor `DeviceClassResistorWithBulk` belongs here;
#: * MOS transistors: `combine_devices()` folds them both in parallel (`W`
#:   sums) and in series (`L` sums), and a series fold's surviving device has
#:   terminal connectivity no pre-combine group shares, so a
#:   connectivity-keyed conservation rule cannot describe it.
_COMBINE_PARAMETER_CONSERVATION_RULES: tuple[
    tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        ("DeviceClassCapacitor", "DeviceClassCapacitorWithBulk"),
        ("C",),
        (),
    ),
    (
        ("DeviceClassBJT3Transistor", "DeviceClassBJT4Transistor"),
        ("AE", "PE", "NE"),
        ("AB", "PB", "AC", "PC"),
    ),
)


def _combine_conservation_rules(
    kdb_module: Any,
) -> list[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]]:
    """Resolve :data:`_COMBINE_PARAMETER_CONSERVATION_RULES`'s device-class
    *names* into the live `klayout.db` classes, once per correction pass."""
    return [
        (
            tuple(getattr(kdb_module, name) for name in class_names),
            summed,
            preserved,
        )
        for class_names, summed, preserved in _COMBINE_PARAMETER_CONSERVATION_RULES
    ]


def _combine_conservation_rule_for(
    device_class: Any,
    rules: Sequence[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The `(summed, preserved)` parameter names governing `device_class`,
    or `None` when no conservation rule covers it (the common case -- MOS
    transistors, resistors, diodes and every custom class)."""
    for classes, summed, preserved in rules:
        if isinstance(device_class, classes):
            return summed, preserved
    return None


def _combine_parameter_tolerance(expected: float) -> float:
    """Comparison tolerance for one conserved parameter.

    A relative tolerance, floored by a tiny absolute term for the
    ``expected == 0`` edge case -- *not* ``1e-15`` (a plausible
    femtofarad-scale `C` value in its own right, which would mask exactly the
    single-instance-vs-summed-total discrepancy this check exists to catch,
    e.g. `1e-15` vs. a summed `2e-15`).
    """
    return max(1e-21, abs(expected) * 1e-9)


def _combine_group_key(
    circuit_name: str, device: Any, device_class: Any
) -> tuple[Any, ...]:
    """A key identifying the group of parallel devices `device` combines
    with: the same circuit, the same device-class name, and the same set of
    (terminal name, connected net) pairs. A parallel fold only ever merges
    devices that agree on every one of those, so one key names exactly one
    combinable group -- on either side of the combine.

    Identifies each terminal's net by `Net.expanded_name()` -- always
    non-empty (KLayout synthesizes a name like `"$1"` for an internal,
    otherwise-unnamed net) and unaffected by `combine_devices()` itself
    (which only removes devices, never renames or removes nets, until
    `run_lvs`'s later `_purge_emptied_nets` step -- which runs *after* this
    correction, see `run_lvs`) -- rather than by net object identity or
    `Net.cluster_id` (an *L2N* net-region correlation id that is `0` for
    every net on a hand-built or `NetlistSpiceReader`-read netlist, not a
    stable per-net enumerator -- verified against `klayout==0.30.10`), since
    a pre-combine snapshot's net objects are never the same Python/C++
    objects as the post-combine netlist's own nets.
    """
    terminals = []
    for terminal in device_class.terminal_definitions():
        net = _net_for_terminal_or_none(device, terminal.name)
        terminals.append(
            (terminal.name, net.expanded_name() if net is not None else None)
        )
    return (circuit_name, device_class.name, tuple(sorted(terminals)))


def _combine_parameter_expectations(
    netlist: Any,
    rules: Sequence[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """What every conservation-governed device group in `netlist` says a
    combined device folded from it must look like.

    Keyed by :func:`_combine_group_key`, each value is
    ``{"summed": {name: total}, "common": {name: value | None}}`` -- the
    group's per-parameter total for the rule's *summed* parameters, and its
    single shared value for the rule's *preserved* parameters (``None`` when
    the group's members do not all agree on one, in which case nothing can be
    asserted about that parameter and it is skipped).
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            device_class = device.device_class()
            rule = _combine_conservation_rule_for(device_class, rules)
            if rule is None:
                continue
            summed_names, preserved_names = rule
            key = _combine_group_key(circuit.name, device, device_class)
            entry = groups.get(key)
            if entry is None:
                entry = groups[key] = {
                    "summed": {name: 0.0 for name in summed_names},
                    "common": {
                        name: device.parameter(name) for name in preserved_names
                    },
                }
            for name in summed_names:
                entry["summed"][name] += device.parameter(name)
            for name in preserved_names:
                common = entry["common"][name]
                if common is None:
                    continue
                value = device.parameter(name)
                if abs(value - common) > _combine_parameter_tolerance(common):
                    entry["common"][name] = None
    return groups


def _combine_group_survivors(
    netlist: Any,
    rules: Sequence[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]],
    circuit_names: Sequence[str] | None,
) -> list[tuple[tuple[Any, ...], str, Any, Any, tuple[str, ...], tuple[str, ...]]]:
    """Every conservation-governed device in `netlist` that is the **only**
    survivor under its :func:`_combine_group_key`, as
    ``(key, circuit name, device, device class, summed, preserved)``.

    A group with more than one survivor is dropped here rather than in the
    caller: it did not fold to a single device, so the group-total rule is
    not assertable against any of its members -- see
    :func:`_correct_combine_parameters`'s docstring.
    """
    found: list[tuple[tuple[Any, ...], str, Any, Any, tuple[str, ...], tuple[str, ...]]]
    found = []
    counts: dict[tuple[Any, ...], int] = {}
    for circuit in netlist.each_circuit():
        if circuit_names is not None and circuit.name not in circuit_names:
            continue
        for device in circuit.each_device():
            device_class = device.device_class()
            rule = _combine_conservation_rule_for(device_class, rules)
            if rule is None:
                continue
            key = _combine_group_key(circuit.name, device, device_class)
            counts[key] = counts.get(key, 0) + 1
            found.append((key, circuit.name, device, device_class, *rule))
    return [entry for entry in found if counts[entry[0]] == 1]


def _combine_expected_values(
    group: Mapping[str, Any],
    summed: Sequence[str],
    preserved: Sequence[str],
) -> list[tuple[str, float]]:
    """The `(parameter, expected value)` pairs a single-survivor device must
    satisfy: its group's total for each *summed* parameter, and its group's
    common value for each *preserved* parameter the group agrees on."""
    values = [(name, group["summed"][name]) for name in summed]
    values += [
        (name, group["common"][name])
        for name in preserved
        if group["common"][name] is not None
    ]
    return values


def _apply_combine_parameter_expectations(
    circuit_name: str,
    device: Any,
    device_class: Any,
    expectations: Sequence[tuple[str, float]],
) -> list[dict[str, Any]]:
    """Overwrite, in place, every parameter of `device` that disagrees with
    its expected value beyond floating-point tolerance, and describe each
    correction for the `device.combine_parameter_corrected` entry's
    ``details.corrected[]``."""
    corrected: list[dict[str, Any]] = []
    for name, expected_value in expectations:
        actual_value = device.parameter(name)
        if abs(actual_value - expected_value) <= _combine_parameter_tolerance(
            expected_value
        ):
            continue
        device.set_parameter(name, expected_value)
        corrected.append(
            {
                "circuit": circuit_name,
                "device": device.name or None,
                "class": device_class.name,
                "parameter": name,
                "before": actual_value,
                "after": expected_value,
            }
        )
    return corrected


def _correct_combine_parameters(
    pre_combine_netlist: Any,
    netlist: Any,
    side: str,
    *,
    circuit_names: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Repair a combined device's parameters left inconsistent by
    `Netlist.combine_devices()` (issues #1497, #2374).

    Every group of parallel devices sharing one (circuit, device class,
    terminal-net-connectivity) identity has a *known* post-combine parameter
    set, given only the group's pre-combine members -- see
    :data:`_COMBINE_PARAMETER_CONSERVATION_RULES` for the per-class rules and
    their provenance. A capacitor group's `C` is a conserved total (combining
    can only redistribute it across fewer surviving devices, never change
    it); a parallel bipolar array's emitter parameters (`AE`/`PE`/`NE`) are
    likewise totals, while its shared base/collector region parameters
    (`AB`/`PB`/`AC`/`PC`) stay at the array's single common value.

    This compares `netlist` (just combined, potentially in place) against
    `pre_combine_netlist` (the untouched snapshot taken before combining ran)
    and overwrites any post-combine parameter that disagrees with its group's
    rule beyond floating-point tolerance -- independent of *why* KLayout's
    own combine got it wrong, and regardless of whether that specific failure
    mode can be forced on demand (see `tests/test_lvs.py`'s combine-
    correction tests, which simulate both reported shapes via a
    monkeypatched `combine_devices()` rather than a live reproduction).

    Only groups that actually **folded to a single surviving device** are
    checked. A group KLayout left uncombined -- because combining was
    restricted away from its class by a list-valued
    ``options.combine_devices`` (issue #1370), or because it is a lone
    device to begin with -- keeps more than one post-combine device under its
    key, and asserting a group *total* against each of those individually
    would corrupt correct values rather than repair wrong ones. That
    single-survivor condition is exactly the shape both reported failures
    take (issue #1497's folded capacitor group, issue #2374's folded bipolar
    array), so nothing this check exists to catch is given up by it.

    Called only when `_combine_devices_safely` reported success (`None`) on
    both sides -- a hard combine failure already rolls both sides back to
    their pre-combine snapshots (issue #1370's symmetric degrade) before
    this would ever run, so this function never has to reconcile a
    combine-in-progress netlist against its own pre-combine snapshot.

    Returns a `severity: "warning"` `mismatches[]` entry
    (:data:`CATEGORY_DEVICE_COMBINE_PARAMETER_CORRECTED`) naming every
    corrected `(device, parameter)` pair, or `None` when every post-combine
    parameter already agreed with its pre-combine group (the expected, common
    case: this is a defensive invariant check applied unconditionally, not a
    response to a failure this module has observed directly against its own
    synthetic netlists -- see this module's docstring / issues #1497 and
    #2374 for the investigations).

    ``circuit_names`` (issue #1557): restricts the post-combine scan to just
    these circuit names -- ``options.combine_devices_per_circuit``'s own
    per-circuit combine, unlike the whole-netlist ``options.combine_devices``
    path (which always passes ``None``, the default, and scans every
    circuit), only wants this check applied to the circuit(s) it actually
    combined *cleanly*. A circuit this ran over unnecessarily would be a
    no-op anyway (its post-combine parameters already agree with their own
    pre-combine groups, since nothing touched them), but the explicit
    restriction keeps a circuit whose combine was reported incomplete
    untouched too, matching this function's own docstring contract ("called
    only when ... reported success") even though nothing here rolls an
    incomplete per-circuit combine back the way the whole-netlist symmetric
    degrade does.
    """
    import klayout.db as kdb_module

    rules = _combine_conservation_rules(kdb_module)
    expected = _combine_parameter_expectations(pre_combine_netlist, rules)
    survivors = _combine_group_survivors(netlist, rules, circuit_names)

    corrected: list[dict[str, Any]] = []
    for key, circuit_name, device, device_class, summed, preserved in survivors:
        group = expected.get(key)
        if group is None:
            # Defensive only: combining can only merge existing pre-combine
            # connectivity groups, never invent a new one, so every
            # post-combine key should already be present above.
            continue
        corrected += _apply_combine_parameter_expectations(
            circuit_name,
            device,
            device_class,
            _combine_expected_values(group, summed, preserved),
        )

    if not corrected:
        return None

    names = ", ".join(
        f"{entry['circuit']}/{entry['device'] or '<unnamed>'}.{entry['parameter']} "
        f"({entry['before']!r} -> {entry['after']!r})"
        for entry in corrected
    )
    return _mismatch(
        CATEGORY_DEVICE_COMBINE_PARAMETER_CORRECTED,
        "warning",
        "options.combine_devices produced one or more combined devices whose "
        "parameters did not agree with the parallel group KLayout's own "
        "Netlist.combine_devices() folded them from (a summed parameter not "
        "equal to the group's pre-combine total, or a shared-region "
        f"parameter not equal to the group's common value), on the {side} "
        "netlist -- corrected in place (see docs/cli/lvs.md, "
        f"'device.combine_parameter_corrected'): {names}",
        side,
        details={"corrected": corrected},
    )


def _flatten_netlist_safely(netlist: kdb.Netlist, side: str) -> dict[str, Any] | None:
    """Call ``netlist.flatten()``, in-process, on ``side``'s netlist (issue
    #1085's ``options.flatten_reference``/``options.flatten_layout``).

    ``klt extract`` always extracts a **flat** layout-side netlist (a single
    top circuit; see ``extract.py``'s own module docstring) -- so a
    hierarchical reference netlist (one leaf ``.subckt`` plus N instance
    calls of it, the shape a large regular macro's schematic naturally
    takes) can never structurally match it: ``NetlistComparer`` sees a flat
    top circuit with N x (devices per leaf) devices on one side and a top
    circuit with *zero* devices plus N subcircuit calls on the other, and
    reports a hard, undiagnosable ``topology`` mismatch on both sides (see
    this module's docstring, and ``docs/cli/lvs.md``'s "`layout.flatten` /
    `reference.flatten`" section) -- not a partial result a caller can act
    on.

    ``Netlist.flatten()`` is KLayout's own whole-netlist flatten: "After
    calling this method, only the top circuits will remain" -- every
    subcircuit-call instance is substituted in-place, in-process. Called
    here, before :func:`_select_circuit`, so a caller-declared ``top`` name
    still resolves normally afterwards whenever it already named a genuine
    top-level circuit (the common case -- the design's own top level, not
    an interior leaf that flatten would have inlined away).

    Symmetric and opt-in on **both** sides (``options.flatten_reference``
    for the reference netlist, ``options.flatten_layout`` for the layout
    netlist -- the pre-extracted ``layout.netlist`` shape can be
    hierarchical too, issue #1085's item 2), so the default (both flags
    omitted) is byte-identical to today's behaviour and a caller who
    genuinely wants a hierarchy-preserving compare is never silently
    flattened out from under them.

    Returns a ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "topology.flattened"``) disclosing how many circuits were
    collapsed, so a ``"match"`` reached after flattening is never silently
    indistinguishable from one reached against the netlist's original
    hierarchy -- or ``None`` when the netlist already had only its top
    circuit(s) (nothing to flatten, e.g. an already-flat ``klt extract``
    layout side), in which case there is nothing to disclose.
    """
    circuits_before = sum(1 for _ in netlist.each_circuit())
    netlist.flatten()
    circuits_after = sum(1 for _ in netlist.each_circuit())
    if circuits_before == circuits_after:
        return None
    return _mismatch(
        CATEGORY_TOPOLOGY_FLATTENED,
        "warning",
        f"options.flatten_{side} flattened the {side} netlist before "
        f"comparing: {circuits_before} circuit(s) were collapsed into "
        f"{circuits_after} top-level circuit(s), substituting every "
        "subcircuit-call instance in place -- this compare verified "
        "topology only after removing the original hierarchy boundaries on "
        f'this side (see docs/cli/lvs.md, "topology.flattened")',
        side,
    )


def _purge_emptied_nets(netlist: kdb.Netlist) -> None:
    """Remove nets that ``combine_devices()`` emptied -- nets left with no
    terminals, no pins, and no subcircuit pins after matched device arrays
    were folded (issue #500).

    ``Netlist.combine_devices()`` folds combinable device groups (e.g. a
    series string of N identical devices into one device) but leaves the
    N-1 interior nodes it disconnected behind in the circuit, each now with
    zero connections. Those nets are not part of the netlist's topology by
    any definition, yet they still inflate ``counts.nets.*`` (computed off
    ``each_net()``) and surface as spurious ``net.unmatched`` findings in
    ``mismatches[]`` that no caller can act on -- there is nothing to fix
    about a net with nothing attached to it. Dropping them makes both counts
    and mismatches honest, symmetric with the combine step itself: a caller
    reading ``counts.nets.layout`` to judge how far apart two netlists are
    sees the post-combine topology, not ``combine_devices()``'s internal
    bookkeeping.

    Deliberately narrower than KLayout's own ``Circuit.purge_nets()``: it
    only removes nets that are simultaneously terminal-less, pin-less, and
    subcircuit-pin-less, so a genuinely-unused *top-level pin*'s net (a net
    with zero terminals but a real pin attached) is never dropped -- that
    would silently change ``counts.pins.*`` and remove a pin the comparer
    must still see, which is outside this fix's scope. Applied per circuit
    across the whole netlist, mirroring ``combine_devices()``'s own
    netlist-wide scope so interior nodes emptied in subcircuits below the
    selected top are cleaned up too.
    """
    for circuit in netlist.each_circuit():
        emptied = [
            net
            for net in circuit.each_net()
            if net.terminal_count() == 0
            and net.pin_count() == 0
            and net.subcircuit_pin_count() == 0
        ]
        for net in emptied:
            circuit.remove_net(net)


def _select_circuit(netlist: kdb.Netlist, top: str | None, side: str) -> kdb.Circuit:
    """Pick the circuit to compare: ``top`` by name if given, else the
    netlist's sole top circuit (an ambiguous/missing choice is an
    :class:`LvsError`) -- the netlist-compare analogue of ``_layout.py``'s
    ``resolve_top_cell``."""
    if top is not None:
        circuit = netlist.circuit_by_name(top)
        if circuit is None:
            raise LvsError(f"top cell/subcircuit '{top}' not found in {side} netlist")
        return circuit

    top_circuits = list(netlist.top_circuits())
    if len(top_circuits) == 0:
        raise LvsError(f"{side} netlist has no top circuit")
    if len(top_circuits) > 1:
        names = ", ".join(sorted(circuit.name for circuit in top_circuits))
        raise LvsError(
            f"{side} netlist has {len(top_circuits)} top circuits ({names}); "
            "pass 'top' to select one"
        )
    return top_circuits[0]


def _prune_extra_top_circuits(netlist: kdb.Netlist, keep: kdb.Circuit) -> None:
    """Remove every top-level circuit other than ``keep`` from ``netlist``.

    A reference/layout netlist file may declare unrelated top-level circuits
    (e.g. a library SPICE file with several ``.subckt``s, only one of which
    is the design under comparison). Left in place, each would surface as
    its own spurious top-level circuit mismatch. Safe to prune: by
    definition, a *top* circuit is not referenced by anything else in the
    netlist, so removing one never affects ``keep``'s own hierarchy.

    Compares circuits by identity (``is not``) rather than ``cell_index``:
    a netlist read directly from SPICE/Verilog text (``kdb.NetlistSpiceReader``
    or this module's gate-level-verilog/subckt-call conversions) has no
    backing ``kdb.Layout``, so every circuit's ``cell_index`` reads back as 0
    -- making a ``cell_index`` comparison always false and silently pruning
    nothing (issue #1657).
    """
    for circuit in list(netlist.top_circuits()):
        if circuit is not keep:
            netlist.purge_circuit(circuit)


# --------------------------------------------------------------------------- #
# Hints: same_nets / equivalent_pins
# --------------------------------------------------------------------------- #


def _apply_hints(
    comparer: kdb.NetlistComparer,
    hints: dict[str, Any],
    layout_circuit: kdb.Circuit,
    reference_circuit: kdb.Circuit,
) -> tuple[list[tuple[str, str]], dict[str, list[list[str]]]]:
    """Wire ``request.hints`` into the comparer, per spike section 2b.

    ``same_nets``: ``[[layout_net_name, reference_net_name], ...]`` -- ties a
    named net in the layout's top circuit to a named net in the reference's
    top circuit (``NetlistComparer.same_nets(circuit_a, circuit_b, net_a,
    net_b, must_match=True)``). A hint naming a net that does not exist on
    the stated side is a malformed request (:class:`LvsError`), not a silent
    no-op -- a typo'd hint should be visible, not swallowed.

    ``equivalent_pins``: ``{"<subcircuit name>": [[pin_a, pin_b], ...], ...}``
    -- declares a group of swappable pins on the *reference*-side circuit of
    that name (``NetlistComparer.equivalent_pins`` only accepts circuits from
    the second netlist passed to ``compare()``, which is always the
    reference netlist in this module's ``compare(layout, reference)`` call
    order -- see ``run_lvs``).

    Returns a 2-tuple:

    - The declared ``same_nets`` pairs as ``(layout_net.expanded_name(),
      reference_net.expanded_name())`` tuples (issue #499) -- the caller
      passes this to :func:`_build_mismatches` so it can tell, after
      ``compare()`` runs, which of these hard assertions the comparer
      actually confirmed (``must_match=True`` is passed unconditionally
      above, so a hint the comparer disagrees with is a real finding, not a
      no-op).
    - The ``equivalent_pins`` groupings actually passed to
      ``NetlistComparer.equivalent_pins()``, keyed by subcircuit name,
      verbatim as given in the request (issue #1998) -- unlike
      ``same_nets``, a swappable-pin group has no "rejected" outcome to
      detect (it declares an equivalence the comparer either uses or has no
      occasion to use, never one it can refuse), so this dict is the only
      record that the hint was applied at all. A subcircuit name is only
      added once every one of its groups has resolved without error, so a
      request that fails validation partway through never leaves a partial
      entry in the returned dict (the caller never sees it either, since
      :class:`LvsError` propagates out of this function first).
    """
    same_nets_declared: list[tuple[str, str]] = []
    same_nets = hints.get("same_nets") or []
    for entry in same_nets:
        if not isinstance(entry, list) or len(entry) != 2:
            raise LvsError(
                "hints.same_nets entries must be [layout_net, reference_net]"
            )
        layout_name, reference_name = entry
        net_a = layout_circuit.net_by_name(layout_name)
        if net_a is None:
            raise LvsError(f"hints.same_nets: layout net '{layout_name}' not found")
        net_b = reference_circuit.net_by_name(reference_name)
        if net_b is None:
            raise LvsError(
                f"hints.same_nets: reference net '{reference_name}' not found"
            )
        comparer.same_nets(layout_circuit, reference_circuit, net_a, net_b, True)
        same_nets_declared.append((net_a.expanded_name(), net_b.expanded_name()))

    equivalent_pins = hints.get("equivalent_pins") or {}
    equivalent_pins_applied: dict[str, list[list[str]]] = {}
    for subcircuit_name, pin_groups in equivalent_pins.items():
        reference_netlist = reference_circuit.netlist()
        target_circuit = reference_netlist.circuit_by_name(subcircuit_name)
        if target_circuit is None:
            raise LvsError(
                f"hints.equivalent_pins: circuit '{subcircuit_name}' not found "
                "in reference netlist"
            )
        applied_groups: list[list[str]] = []
        for group in pin_groups:
            pin_ids = []
            for pin_name in group:
                pin = target_circuit.pin_by_name(pin_name)
                if pin is None:
                    raise LvsError(
                        f"hints.equivalent_pins: pin '{pin_name}' not found on "
                        f"circuit '{subcircuit_name}'"
                    )
                pin_ids.append(pin.id())
            comparer.equivalent_pins(target_circuit, pin_ids)
            applied_groups.append(list(group))
        # Only recorded once every group for this subcircuit resolved cleanly
        # (issue #1998) -- a `pin_by_name` miss above raises before this
        # assignment runs, so a malformed request never leaves a partial
        # entry behind for the caller to (mis)report as fully applied.
        equivalent_pins_applied[subcircuit_name] = applied_groups

    return same_nets_declared, equivalent_pins_applied


# --------------------------------------------------------------------------- #
# Reference-side device-class normalisation: reference.device_bulk (issue #506)
# --------------------------------------------------------------------------- #


def _find_device_class(netlist: Any, name: str) -> Any:
    """The device class named ``name`` in ``netlist``, matched exactly first
    and then case-insensitively -- ``NetlistSpiceReader`` upper-cases a
    ``.model``/element model name (``res_x`` -> ``RES_X``), so a request
    naturally written in the netlist's own lower-case spelling still
    resolves. ``None`` when no class matches."""
    exact = netlist.device_class_by_name(name)
    if exact is not None:
        return exact
    lowered = name.lower()
    for candidate in netlist.each_device_class():
        if candidate.name.lower() == lowered:
            return candidate
    return None


def _device_class_names(netlist: Any) -> list[str]:
    """Every device-class name registered on ``netlist``, sorted -- used to
    make an unresolvable ``reference.device_bulk`` key's error message
    actionable."""
    return sorted(device_class.name for device_class in netlist.each_device_class())


def _find_or_create_net(circuit: Any, name: str) -> tuple[Any, bool]:
    """``(net, created)`` for the net named ``name`` on ``circuit``, matched
    exactly first and then case-insensitively (same reason as
    :func:`_find_device_class`), creating it when the reference netlist does
    not model that node at all -- the ordinary case for a bulk terminal a
    schematic reference simply does not carry."""
    exact = circuit.net_by_name(name)
    if exact is not None:
        return exact, False
    lowered = name.lower()
    for candidate in circuit.each_net():
        if (candidate.name or "").lower() == lowered:
            return candidate, False
    return circuit.create_net(name), True


def _apply_reference_device_bulk(
    spec: Any,
    layout_netlist: Any,
    reference_netlist: Any,
) -> list[dict[str, Any]]:
    """Reconcile a reference device class that is one terminal short of the
    layout side's same-named class (issue #506, issue #504's option 1).

    ``spec`` is ``request.reference.device_bulk``:
    ``{"<device class / model name>": "<reference net name>"}``. For each
    entry, the reference-side class of that name is given the one terminal
    the layout-side class declares and it does not (typically the deck's
    bulk/well/collector terminal, e.g. the ``W`` of a
    ``bulk_to_substrate`` resistor flavour's three-terminal ``RES_X``), and
    every reference-side instance of that class has the new terminal tied to
    the named net -- created on the instance's own circuit when the reference
    netlist does not model that node at all.

    This is the *reconciliation* :data:`CATEGORY_DEVICE_CLASS_ARITY` (issue
    #505) deliberately stopped short of: with it, ``NetlistComparer`` can pair
    the two sides' devices and the run can legitimately report
    ``status: "match"``; without it, no request whose layout side uses a
    bulk-terminal device flavour against a schematic reference that does not
    model that terminal can ever match. Applied before the comparer is built
    (see ``run_lvs``), so the classes are already the same arity by the time
    ``compare()`` runs and ``_device_class_arity_mismatch`` no longer fires
    for the reconciled class.

    Returns one ``severity: "warning"``
    :data:`CATEGORY_DEVICE_BULK_RECONCILED` entry per reconciled class, which
    ``run_lvs`` appends to ``mismatches[]``. The disclosure is the point: the
    added terminal's connectivity is a caller *assertion*, not something read
    off the reference netlist, so a match reached this way is never silently
    indistinguishable from a fully independent one -- exactly the discipline
    :data:`CATEGORY_DEVICE_BODY_UNVERIFIED` (issue #281) applies to an
    unverified MOS body.

    Every malformed or inapplicable entry is an :class:`LvsError`, never a
    silent no-op -- the same convention ``hints.same_nets`` follows: a class
    name that resolves on neither side, a reference class that is not
    actually missing a terminal, and a class missing more than one terminal
    (this hook reconciles exactly one extra terminal per class, since the
    entry names exactly one net) all raise.
    """
    entries: list[dict[str, Any]] = []
    if not spec:
        return entries

    import klayout.db as kdb

    for model, net_name in spec.items():
        if not isinstance(net_name, str) or not net_name:
            raise LvsError(
                f"request.reference.device_bulk['{model}'] must be a non-empty "
                "reference net name"
            )

        reference_class = _find_device_class(reference_netlist, model)
        if reference_class is None:
            present = ", ".join(_device_class_names(reference_netlist)) or "none"
            raise LvsError(
                f"request.reference.device_bulk: device class '{model}' not "
                f"found in the reference netlist (classes present: {present})"
            )
        layout_class = _find_device_class(layout_netlist, model)
        if layout_class is None:
            present = ", ".join(_device_class_names(layout_netlist)) or "none"
            raise LvsError(
                f"request.reference.device_bulk: device class '{model}' not "
                f"found in the layout netlist (classes present: {present}) -- "
                "there is no layout-side terminal list to reconcile the "
                "reference class against"
            )

        layout_terminals = _terminal_names(layout_class)
        reference_terminals = _terminal_names(reference_class)
        missing = [
            terminal
            for terminal in layout_terminals
            if terminal not in reference_terminals
        ]
        if not missing:
            raise LvsError(
                f"request.reference.device_bulk: reference device class "
                f"'{reference_class.name}' already declares every terminal the "
                f"layout-side class does ({reference_terminals}) -- there is no "
                "implicit bulk terminal to reconcile; remove this entry"
            )
        if len(missing) > 1:
            raise LvsError(
                f"request.reference.device_bulk: reference device class "
                f"'{reference_class.name}' declares {reference_terminals} against "
                f"the layout side's {layout_terminals} -- {len(missing)} terminals "
                f"({missing}) apart. This hook reconciles exactly one extra "
                "(bulk/well/collector) terminal per class, since the entry names "
                "exactly one net"
            )

        terminal_name = missing[0]
        description = next(
            (
                terminal.description
                for terminal in layout_class.terminal_definitions()
                if terminal.name == terminal_name
            ),
            "",
        )
        reference_class.add_terminal(
            kdb.DeviceTerminalDefinition(terminal_name, description)
        )
        terminal_id = reference_class.terminal_id(terminal_name)

        connected = 0
        net_created = False
        for circuit in reference_netlist.each_circuit():
            devices = [
                device
                for device in circuit.each_device()
                if device.device_class() is reference_class
            ]
            if not devices:
                continue
            net, created = _find_or_create_net(circuit, net_name)
            net_created = net_created or created
            for device in devices:
                device.connect_terminal(terminal_id, net)
                connected += 1

        entries.append(
            _mismatch(
                CATEGORY_DEVICE_BULK_RECONCILED,
                "warning",
                f"request.reference.device_bulk reconciled reference device "
                f"class '{reference_class.name}' with the layout side: a "
                f"'{terminal_name}' terminal was added to the reference class "
                f"(layout: {layout_terminals}, reference was: "
                f"{reference_terminals}) and tied to reference net "
                f"'{net_name}' on {connected} device instance(s), "
                + (
                    "a net created for this compare"
                    if net_created
                    else "an existing reference net"
                )
                + " -- that terminal's connectivity was asserted by the "
                "request, not read from the reference netlist, so this "
                "dimension of the compare is not independently verified (see "
                "docs/cli/lvs.md, 'device.bulk_reconciled')",
                "reference",
                device={
                    "layout": None,
                    "reference": None,
                    "class": reference_class.name,
                },
                details={
                    "terminal": terminal_name,
                    "reference_net": net_name,
                    "reference_net_created": net_created,
                    "devices": connected,
                    "layout_terminals": layout_terminals,
                    "reference_terminals": reference_terminals,
                },
            )
        )

    return entries


#: Issue #1907: the *primary* (compared) parameter of each device family whose
#: `form: "subckt-call"` conversion writes a literal `0` placeholder into the
#: plain-element card's positional value slot -- `DeviceClassResistor`'s `R`
#: and `DeviceClassCapacitor`'s `C` (each class's only primary parameter;
#: `L`/`W`/`A`/`P` are secondary and are not compared by default either way).
#: Keyed by the family name `netlist_normalize.ReferenceConversion`
#: reports.
_PLACEHOLDER_VALUE_PARAMETER = {"resistor": "R", "capacitor": "C"}


def _apply_reference_placeholder_values(
    spec: dict[str, str],
    layout_netlist: Any,
    reference_netlist: Any,
) -> list[dict[str, Any]]:
    """Exclude a converted reference class's placeholder ``0`` value from the
    compare, so the class can still pair on topology (issue #1907).

    ``spec`` is the ``form: "subckt-call"`` conversion's own
    :attr:`~klayout_tools.netlist_normalize.ReferenceConversion.placeholder_value_classes`
    -- ``{"<device class>": "resistor"|"capacitor"}``, one entry per
    resistor/capacitor class the conversion emitted. Those cards carry the
    literal ``0`` placeholder in their positional value slot because
    :mod:`klayout_tools.netlist_normalize` has no PDK sheet-resistance /
    capacitance-per-area table to compute a real value from (deliberately --
    see that module's docstring).

    **Why this is not merely a cosmetic parameter difference.** ``R``
    (resp. ``C``) is the *primary*, compared parameter of KLayout's
    ``DeviceClassResistor``/``DeviceClassCapacitor``. ``NetlistComparer``
    uses primary-parameter equality to seed device correspondence, so a
    reference class whose every instance reads ``0`` against a layout side
    carrying real, geometry-computed values does not report a per-device
    parameter finding -- it fails to pair the class *at all*, collapsing into
    a wholesale ``device.unmatched``/``topology`` cascade over every instance
    and every net that touches one, even when each instance sits on its own
    distinct, unambiguous net pair. Excluding that one parameter (KLayout's
    own ``EqualDeviceParameters.ignore``, applied to **both** sides' class --
    the comparer consults each side's own class, so ignoring it on the
    reference alone changes nothing) lets topology do the pairing it always
    could have, exactly as an equivalent hand-written ``form:
    "plain-element"`` reference carrying the real values already does.

    **Scoped to the provable placeholder, never to a coincidental zero.**
    Only classes this conversion actually emitted are considered (a
    ``form: "plain-element"`` reference carries real values and never reaches
    here at all), and only when *every* reference-side instance of the class
    reads exactly ``0`` -- the invariant the conversion guarantees by
    construction. A class whose reference-side instances carry any nonzero
    value is left completely alone, so a genuine value defect on a mixed
    reference is still compared and still reported.

    Returns one ``severity: "warning"``
    :data:`CATEGORY_DEVICE_PLACEHOLDER_VALUE` entry per excluded class, which
    ``run_lvs`` appends to ``mismatches[]``. The disclosure is the point,
    exactly as for :func:`_apply_reference_device_bulk`: the resistance /
    capacitance dimension of that class was *not* verified by this compare,
    so a ``"match"`` reached this way is never silently indistinguishable
    from one where the two sides' values actually agreed.

    Never raises: unlike ``reference.device_bulk`` (a caller assertion, where
    an inapplicable entry is a request error), ``spec`` is derived
    internally, so a class that does not resolve on both sides -- or whose
    reference instances are not all placeholders -- is simply left alone and
    diagnosed by the ordinary compare.
    """
    entries: list[dict[str, Any]] = []
    if not spec:
        return entries

    import klayout.db as kdb

    for model, kind in sorted(spec.items()):
        parameter = _PLACEHOLDER_VALUE_PARAMETER.get(kind)
        if parameter is None:
            continue
        reference_class = _find_device_class(reference_netlist, model)
        layout_class = _find_device_class(layout_netlist, model)
        if reference_class is None or layout_class is None:
            # The class is not instantiated on one of the two sides; there is
            # nothing to pair and the ordinary compare already says so.
            continue
        if not reference_class.has_parameter(
            parameter
        ) or not layout_class.has_parameter(parameter):
            continue

        reference_parameter_id = reference_class.parameter_id(parameter)
        reference_values = [
            device.parameter(reference_parameter_id)
            for circuit in reference_netlist.each_circuit()
            for device in circuit.each_device()
            if device.device_class().name == reference_class.name
        ]
        if not reference_values or any(value != 0.0 for value in reference_values):
            # Not (or not only) the conversion's placeholder -- leave the
            # class's value comparison exactly as it was.
            continue

        layout_parameter_id = layout_class.parameter_id(parameter)
        layout_values = [
            device.parameter(layout_parameter_id)
            for circuit in layout_netlist.each_circuit()
            for device in circuit.each_device()
            if device.device_class().name == layout_class.name
        ]

        reference_class.equal_parameters = kdb.EqualDeviceParameters.ignore(
            reference_parameter_id
        )
        layout_class.equal_parameters = kdb.EqualDeviceParameters.ignore(
            layout_parameter_id
        )

        entries.append(
            _mismatch(
                CATEGORY_DEVICE_PLACEHOLDER_VALUE,
                "warning",
                f"reference device class '{reference_class.name}' was "
                f"converted from a subcircuit call (request.reference.form: "
                f"\"subckt-call\"), so its '{parameter}' value is the literal "
                f"0 placeholder on all {len(reference_values)} reference "
                f"instance(s) -- klt lvs has no PDK sheet-resistance/"
                f"capacitance-per-area data to compute a real one. "
                f"'{parameter}' was therefore excluded from this compare on "
                f"both sides (layout: {len(layout_values)} instance(s)"
                + (
                    f", {parameter} "
                    + (
                        f"{_format_placeholder_value(layout_values[0])}"
                        if len(set(layout_values)) == 1
                        else f"{_format_placeholder_value(min(layout_values))}"
                        f"..{_format_placeholder_value(max(layout_values))}"
                    )
                    if layout_values
                    else ""
                )
                + f") and the two sides were paired on topology alone -- that "
                f"dimension of the compare is not independently verified. "
                f"Supply a reference in the plain-element form carrying real "
                f"'{parameter}' values to compare it (see docs/cli/lvs.md, "
                "'device.placeholder_value')",
                "reference",
                device={
                    "layout": None,
                    "reference": None,
                    "class": reference_class.name,
                },
                details={
                    "parameter": parameter,
                    "device_kind": kind,
                    "reference_devices": len(reference_values),
                    "layout_devices": len(layout_values),
                    "layout_values": sorted(set(layout_values)),
                },
            )
        )

    return entries


def _format_placeholder_value(value: float) -> str:
    """A compact, round-trippable rendering of one layout-side device value
    for a :data:`CATEGORY_DEVICE_PLACEHOLDER_VALUE` description -- an integral
    value without its trailing ``.0`` (``120000`` rather than ``120000.0``),
    everything else via ``repr``."""
    if value == int(value):
        return str(int(value))
    return repr(value)


# --------------------------------------------------------------------------- #
# options.compare_parameters (issue #1928): scope which device-class
# parameters take part in the compare
# --------------------------------------------------------------------------- #


def _parse_compare_parameters(
    options: Mapping[str, Any],
) -> dict[str, list[str]] | None:
    """Resolve ``options.compare_parameters`` into an ordered
    ``{<device-class name>: [<parameter name>, ...]}`` mapping, or ``None``
    when the key is absent (issue #1928).

    Every numeric parameter a device class declares is always compared by
    KLayout's own ``NetlistComparer`` unless ``DeviceClass.enable_parameter``
    is used to turn it off -- nothing in the request document reached that
    hook before this option existed, so a single always-compared parameter
    neither side can state identically (a geometry-derived layout-side value
    a reference netlist's own device cards never carry, for instance) made
    ``status: "match"`` unreachable, with no escape hatch narrower than
    teaching one side to state the missing parameter (explicitly out of
    scope for this option -- see :func:`_apply_compare_parameters`).

    ``compare_parameters`` names, per device class, the parameters to
    compare; every other parameter that class declares is disabled via
    ``enable_parameter(name, False)`` before the comparer runs (see
    :func:`_apply_compare_parameters`) and disclosed as a
    ``severity: "warning"`` :data:`CATEGORY_DEVICE_PARAMETER_EXCLUDED` entry
    per suppressed parameter, so a ``"match"`` reached this way is never
    silently indistinguishable from a full parameter compare.

    A wrong-shaped value is a clean request error, matching this module's
    other option-parsing convention (see :func:`_parse_combine_devices`):
    the value must be a non-empty JSON object whose keys are non-empty
    device-class name strings and whose values are non-empty lists of
    non-empty parameter-name strings. Naming a device class or parameter
    that does not actually exist is *not* validated here -- that requires
    resolving against the two netlists' own device classes, which is not yet
    available at request-parse time; see :func:`_apply_compare_parameters`
    for that check (the same two-stage split ``options.combine_devices``'s
    list shape uses between :func:`_parse_combine_devices` and
    :func:`_validate_combine_device_classes`).
    """
    if "compare_parameters" not in options:
        return None
    value = options["compare_parameters"]
    if not isinstance(value, dict) or not value:
        raise LvsError(
            "options.compare_parameters must be a non-empty JSON object "
            "mapping a device-class name to a non-empty list of parameter-"
            'name strings to compare, e.g. {"NFET_01V8": ["W", "L"]}'
        )
    result: dict[str, list[str]] = {}
    for class_name, params in value.items():
        if not isinstance(class_name, str) or not class_name.strip():
            raise LvsError(
                "options.compare_parameters keys must be non-empty "
                "device-class name strings"
            )
        if (
            not isinstance(params, list)
            or not params
            or not all(isinstance(name, str) and name.strip() for name in params)
        ):
            raise LvsError(
                f"options.compare_parameters['{class_name}'] must be a "
                "non-empty list of non-empty parameter-name strings"
            )
        result[class_name.strip()] = [name.strip() for name in params]
    return result


def _apply_compare_parameters(
    spec: dict[str, list[str]] | None,
    layout_netlist: Any,
    reference_netlist: Any,
) -> list[dict[str, Any]]:
    """Enable exactly the requested parameters on each named device class,
    disabling every other declared parameter on **both** sides via
    ``DeviceClass.enable_parameter`` before the comparer is constructed
    (issue #1928).

    ``spec`` is :func:`_parse_compare_parameters`'s resolved
    ``{<device-class name>: [<parameter name>, ...]}`` mapping. Applied to
    both sides' own ``DeviceClass`` instance for the named class (matched
    case-insensitively via :func:`_find_device_class`, the same convention
    ``reference.device_bulk``/``combine_devices``'s list shape already use,
    since ``NetlistSpiceReader`` upper-cases class names read back from
    SPICE while a deck-declared class keeps its own casing) -- symmetrically,
    the same way :func:`_apply_reference_placeholder_values` excludes a
    parameter on both sides, because ``NetlistComparer`` consults each
    device's own class object, not a single shared one.

    **Validation, never a silent no-op.** A device-class name that resolves
    on *neither* side, or a parameter name not declared by the class on
    either side it does resolve on, is a clean :class:`LvsError` naming the
    typo and what is actually available -- the same "typo must be visible"
    discipline ``hints.same_nets`` and ``options.combine_devices``'s array
    form already apply (a silently-ignored typo here would look exactly like
    a device class whose every parameter happens to agree, which is far more
    dangerous than a typo that simply does nothing). A class named in
    ``spec`` but present on only one side is legitimate (mirrors
    ``options.combine_devices``'s own "present on just one side is not an
    error" rule) and is scoped on that side alone.

    **Narrower than teaching a side to state the missing parameter.** This
    option does not compute or synthesize a value for the disabled
    parameter on either side -- it removes that parameter from the compare
    entirely, on both sides, for the named class. Reconciling *what* a
    missing parameter should read (the way ``reference.device_bulk``
    reconciles a missing bulk terminal) is a narrower, per-case fix this
    issue deliberately leaves out of scope; this is the generic escape
    hatch for when that narrower fix is not (yet) available.

    Returns one ``severity: "warning"`` :data:`CATEGORY_DEVICE_PARAMETER_EXCLUDED`
    entry per parameter excluded from a named class (one entry regardless of
    how many sides that class resolves on, ``side: "both"``), which
    ``run_lvs`` appends to ``mismatches[]``. The disclosure is the point --
    same discipline as ``device.bulk_reconciled``/``device.placeholder_value``:
    a ``"match"`` reached with a parameter scoped out is never silently
    indistinguishable from one where every parameter actually agreed.
    """
    entries: list[dict[str, Any]] = []
    if not spec:
        return entries

    known_classes: dict[str, str] = {}
    for netlist in (layout_netlist, reference_netlist):
        for device_class in netlist.each_device_class():
            known_classes.setdefault(device_class.name.lower(), device_class.name)
    unknown_classes = sorted(
        {name for name in spec if name.lower() not in known_classes}
    )
    if unknown_classes:
        available = ", ".join(sorted(known_classes.values())) or "(none)"
        raise LvsError(
            "options.compare_parameters names device class(es) present in "
            f"neither the layout nor the reference netlist: "
            f"{', '.join(unknown_classes)} -- device classes available "
            f"across both sides: {available}"
        )

    for class_name, wanted_params in spec.items():
        layout_class = _find_device_class(layout_netlist, class_name)
        reference_class = _find_device_class(reference_netlist, class_name)
        resolved_classes = [
            device_class
            for device_class in (layout_class, reference_class)
            if device_class is not None
        ]

        known_params: dict[str, str] = {}
        for device_class in resolved_classes:
            for param in device_class.parameter_definitions():
                known_params.setdefault(param.name.lower(), param.name)
        unknown_params = sorted(
            {name for name in wanted_params if name.lower() not in known_params}
        )
        if unknown_params:
            available = ", ".join(sorted(known_params.values())) or "(none)"
            raise LvsError(
                f"options.compare_parameters['{class_name}'] names "
                f"parameter(s) not declared by that device class: "
                f"{', '.join(unknown_params)} -- parameters available on "
                f"'{class_name}': {available}"
            )

        wanted_lower = {name.lower() for name in wanted_params}
        compared_names = sorted(
            {known_params[name] for name in wanted_lower if name in known_params}
        )
        resolved_name = resolved_classes[0].name

        for device_class in resolved_classes:
            for param in device_class.parameter_definitions():
                device_class.enable_parameter(
                    param.name, param.name.lower() in wanted_lower
                )

        for param_lower, param_name in sorted(known_params.items()):
            if param_lower in wanted_lower:
                continue
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_PARAMETER_EXCLUDED,
                    "warning",
                    f"options.compare_parameters scoped device class "
                    f"'{resolved_name}' to compare only "
                    f"{compared_names} -- '{param_name}' was excluded from "
                    "this compare and is NOT verified; a 'match' does not "
                    "confirm the two sides agree on it (see "
                    "docs/cli/lvs.md, 'device.parameter_excluded')",
                    "both",
                    device={
                        "layout": None,
                        "reference": None,
                        "class": resolved_name,
                    },
                    details={
                        "parameter": param_name,
                        "compared_parameters": compared_names,
                    },
                )
            )

    return entries


# --------------------------------------------------------------------------- #
# Compare event capture
# --------------------------------------------------------------------------- #


def _name_or_none(obj: Any) -> str | None:
    """The reported name of a KLayout ``Net``/``Device`` (or a test double
    implementing the same protocol), or ``None`` for a missing side.

    A ``Net``'s ``expanded_name()`` (the branch every real net/device object
    hits) is passed through :func:`spice_safe_net_name` (issue #696) so a
    label-merged net's name matches the exact spelling `klt extract`'s
    ``nets[]``/``merged_net_labels[]`` and the written SPICE netlist already
    use (`|`-joined) -- not KLayout's own un-escaped, comma-joined
    ``Net.expanded_name()`` string. A no-op for every other name (device
    names, and the ``.name`` fallback below never carry a comma in
    practice).
    """
    if obj is None:
        return None
    if hasattr(obj, "expanded_name"):
        return spice_safe_net_name(obj.expanded_name())
    if hasattr(obj, "name"):
        name = obj.name
        name = name() if callable(name) else name
        return spice_safe_net_name(name) if name is not None else None
    return None


def _subcircuit_parent_name(obj: Any) -> str | None:
    """The name of the ``kdb.Circuit`` that contains subcircuit instance
    ``obj`` (a ``kdb.SubCircuit``, or ``None`` for a missing side), i.e.
    ``obj.circuit()`` -- **not** the circuit ``obj`` refers to (that is
    :func:`_subcircuit_ref_name`). Used to populate an unmatched-subcircuit
    ``mismatches[]`` entry's ``circuit`` field (issue #1132) so the finding
    names which module the missing instance lives in. ``getattr``-guarded so
    a stand-in test double that only implements ``expanded_name()`` (like
    the rest of this module's ``_FakeLogger`` fixtures) degrades to ``None``
    rather than raising.
    """
    if obj is None:
        return None
    method = getattr(obj, "circuit", None)
    if not callable(method):
        return None
    return _name_or_none(method())


def _subcircuit_ref_name(obj: Any) -> str | None:
    """The name of the ``kdb.Circuit`` subcircuit instance ``obj`` refers to
    (``obj.circuit_ref()``) -- its "cell type", as opposed to
    :func:`_subcircuit_parent_name`'s containing circuit. Populates an
    unmatched-subcircuit ``mismatches[]`` entry's ``subcircuit`` field (issue
    #1132). ``getattr``-guarded for the same reason as
    :func:`_subcircuit_parent_name`."""
    if obj is None:
        return None
    method = getattr(obj, "circuit_ref", None)
    if not callable(method):
        return None
    return _name_or_none(method())


def _circuit_pin_names(circuit: Any) -> list[str]:
    """``circuit``'s declared pin names, upper-cased, skipping unnamed pins.

    Upper-cased because SPICE is case-insensitive and the two sides of the
    comparison reach this module through different readers:
    ``NetlistSpiceReader`` normalises what it reads to upper case, while
    :func:`~klayout_tools.verilog_netlist.parse_subckt_pin_orders` reports a
    library file's ``.subckt`` header verbatim. Folding both to one case is
    what lets a library pin order line up with the reference circuit read
    back from the converted SPICE (issue #1622).
    """
    each_pin = getattr(circuit, "each_pin", None)
    if not callable(each_pin):
        return []
    return [name.upper() for pin in each_pin() if (name := pin.name())]


def _reference_signal_pin_names(netlist: Any | None) -> frozenset[str] | None:
    """Every pin name declared on any circuit in ``netlist`` (upper-cased),
    or ``None`` when ``netlist`` itself is unavailable.

    Issue #1622: a ``reference.form: "gate-level-verilog"`` reference
    netlist never carries power/ground pins at all -- its conversion only
    ever emits the *signal* subset of a standard cell's real PDK pin order
    (``verilog_netlist.py``'s own "No power/ground pins" docstring note;
    see also ``docs/cli/lvs.md``'s "No power/ground pins" section). So
    every pin name that appears *anywhere* in the reference netlist is, by
    construction, a genuine **signal** pin.

    That makes this the *subtrahend* in :func:`_gate_level_power_pin_names`'s
    derivation, never a power-pin test on its own: "this pin name is absent
    from the reference" says nothing by itself, because a reference that
    never instantiates a given master says nothing at all about that
    master's pins (see that function's docstring).

    **And it is only half the guard** (issue #2076). Subtracting this set
    excludes a signal pin that *some* instance in the design connects --
    it cannot exclude one that the design's only carrier of that pin name
    leaves dangling, because then the conversion emits no pin of that name
    anywhere and this set does not contain it at all. The complementary
    half -- requiring every instantiated master to declare a pin before
    admitting it -- lives in :func:`_gate_level_power_pin_evidence`.
    """
    if netlist is None:
        return None
    each_circuit = getattr(netlist, "each_circuit", None)
    if not callable(each_circuit):
        return None
    names: set[str] = set()
    for circuit in each_circuit():
        names.update(_circuit_pin_names(circuit))
    return frozenset(names)


def _gate_level_power_pin_evidence(
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> tuple[frozenset[str] | None, list[str], bool]:
    """``(power_pin_names, masters, corroborated)`` -- the derived
    power/ground pin universe (issue #1622) together with the evidence it
    rests on: the sorted names of the library cells (masters) the reference
    instantiates, and whether that evidence is genuinely cross-corroborated
    (issue #2076).

    ``power_pin_names`` is the set of pin names (upper-cased) that are
    demonstrably **power/ground** pins of this PDK's standard-cell library,
    derived from data ``run_lvs`` has already read -- or ``None`` when there
    is not enough evidence to derive one at all.

    The derivation, and why each part of it is load-bearing:

    * ``library_pin_orders`` is the standard-cell library's own
      ``.subckt`` data (see :func:`_resolve_gate_level_pin_orders`), i.e.
      each cell's **full** PDK pin order -- signal *and* power/ground.
    * :func:`~klayout_tools.verilog_netlist.convert_gate_level_verilog`
      emits, for each cell the Verilog instantiates, only the pins that
      Verilog actually connects -- structurally never a power/ground pin.

    So a pin the library declares but the conversion did not carry is a
    *candidate* power/ground pin. Two independent conditions must both hold
    before one is admitted, and the second is what issue #2076 added:

    1. **No reference circuit carries the name.**
       :func:`_reference_signal_pin_names` is subtracted, so a pin that any
       instance of any cell connects is a signal pin.
    2. **Every library cell the reference instantiates declares the name.**
       A genuine supply is declared by every standard cell in the library's
       logic set -- the conversion drops it on *all* of them, so it survives
       the intersection. A signal pin is declared by only some of them.

    Condition 1 alone is the pre-#2076 rule, and it does not hold when the
    design's **only carrier of a pin name leaves that pin dangling**: CTS
    emits clock-load cells with unconnected outputs (``sky130_fd_sc_hd__inv_1
    clkload0 (.A(clknet));`` -- ``Y`` dangling), so if no other instantiated
    master declares ``Y``, the conversion emits no ``Y`` pin anywhere, the
    subtrahend is empty for it, and a signal output is admitted as a supply.
    That inflated ``power_pins``, and -- once CTS put two clock loads on two
    different leaf nets -- produced a spurious
    :data:`RULE_POWER_INCONSISTENT_PIN_NET` finding on a layout whose
    supplies were perfectly connected. Condition 2 excludes it: the
    flip-flops declare ``Q``, the clock buffers ``X``, and neither declares
    ``Y``, while all three declare the four supplies.

    Both conditions keep the universe free of any hardcoded per-PDK name
    table (sky130's ``VPWR``/``VGND``/``VPB``/``VNB`` vs. gf180mcu's
    ``VDD``/``VSS``/``VNW``/``VPW``) and of any cell-name glob
    (``fill_*``/``tap*``, the superseded ``docs/cli/lvs.md`` workaround).

    **Restricting the candidate set to cells the reference instantiates is
    the whole point, not an optimisation.** The obvious wider version --
    "every pin name anywhere in the library that is not in the reference's
    signal-pin universe" -- is unsound in exactly the direction this
    function exists to prevent: in a library containing ``dfxtp_1``
    (``CLK D Q VGND VNB VPB VPWR``), a reference that only instantiates
    inverters and buffers mentions ``CLK``/``D``/``Q`` nowhere, so they
    would be admitted as "power" pins and a stray, genuinely
    signal-bearing ``dfxtp_1`` master in the layout would be pruned as
    power-only -- masking a real missing-cell defect. A cell the reference
    never instantiates contributes nothing here, so its signal pins can
    never be mistaken for power pins.

    **Where the evidence genuinely runs out, and why it is reported rather
    than guessed at.** Condition 2 is a cross-master corroboration, so it
    can only corroborate when there is more than one *genuinely distinct*
    master: a reference instantiating a single library cell -- or several
    drive-strength variants of the same one -- that leaves one of its pins
    dangling offers nothing that separates that pin from the cell's
    supplies -- both are declared identically by every instantiated master
    and carried by none. Counting *names* (``len(masters) > 1``) is not
    enough: ``mylib__inv_1``/``mylib__inv_2`` are two masters by name but
    declare the identical pin order, so their intersection is no more
    informative than either alone -- a dangling ``Y`` on both survives
    exactly as it would with only one of them instantiated. ``corroborated``
    is therefore true only when the instantiated masters' full declared pin
    sets are not all identical, i.e. there are at least two distinct
    *shapes* among them (the flip-flop's ``CLK``/``D``/``Q`` vs. the
    inverter's ``A``/``Y``, not merely two differently-named inverters).
    ``masters`` is returned alongside the universe -- and alongside
    ``corroborated`` -- precisely so a same-shape-only case is visible in
    the report (``power_connectivity.power_pins_derivation``, see
    :func:`_power_pins_derivation`) instead of being silently indistinguishable
    from a genuinely corroborated one. Narrowing further -- e.g. demanding
    that a supply be declared by every cell in the *whole* library -- was
    measured against both supported PDKs and rejected: it admits only
    ``VGND``/``VPWR`` for sky130 (9 of 437 cells omit a well tie) and only
    ``VDD``/``VSS`` for gf180mcu, dropping the well-tie pins out of the
    checked universe and out of :func:`_is_power_only_circuit`'s tap-cell
    classification -- real supply-defect coverage traded away to fix a case
    the intersection above already fixes.

    ``power_pin_names`` is ``None`` when either input is missing/unusable,
    and an empty set when nothing qualifies; :func:`_is_power_only_circuit`
    treats both as "no evidence" and prunes nothing. ``masters`` is ``[]``
    whenever the universe is ``None`` or the reference instantiates no cell
    of this library at all.
    """
    if not library_pin_orders:
        return None, [], False
    signal_pin_names = _reference_signal_pin_names(reference_netlist)
    if signal_pin_names is None:
        return None, [], False
    # Case-folded once: library cell names are verbatim from the `.subckt`
    # header (lower case, in both supported libraries), while the reference
    # circuit names come back from `NetlistSpiceReader` upper-cased.
    by_upper_name = {
        str(cell).upper(): pins for cell, pins in library_pin_orders.items()
    }
    masters: set[str] = set()
    declared_per_master: list[set[str]] = []
    for circuit in reference_netlist.each_circuit():
        name = str(circuit.name).upper()
        pins = by_upper_name.get(name)
        if pins is None or name in masters:
            continue
        masters.add(name)
        declared_per_master.append({pin.upper() for pin in pins if pin})
    if not declared_per_master:
        return frozenset(), [], False
    corroborated_pins = set.intersection(*declared_per_master)
    # Genuine corroboration requires at least two distinct declared-pin
    # *shapes* -- not just two master names -- see the docstring above.
    distinct_shapes = {frozenset(pins) for pins in declared_per_master}
    corroborated = len(distinct_shapes) > 1
    return (
        frozenset(corroborated_pins - signal_pin_names),
        sorted(masters),
        corroborated,
    )


def _gate_level_power_pin_names(
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> frozenset[str] | None:
    """Just the power/ground pin universe half of
    :func:`_gate_level_power_pin_evidence` -- the form
    :func:`_prune_power_only_circuits` wants, which needs the
    classification but has no report block to disclose the evidence in."""
    return _gate_level_power_pin_evidence(reference_netlist, library_pin_orders)[0]


def _is_power_only_circuit(
    circuit: Any, power_pin_names: frozenset[str] | None
) -> bool:
    """True when every pin ``circuit`` declares is a known power/ground pin
    of this PDK's standard-cell library (see
    :func:`_gate_level_power_pin_names`) -- i.e. ``circuit`` is a power-only
    cell (a filler/tap cell, whose real PDK pin list is
    ``VPWR``/``VGND``/``VPB``/``VNB`` and nothing else) that is out of
    scope for a signal-only ``gate-level-verilog`` compare (issue #1622).

    Structural, not name-pattern: this never special-cases a cell-name glob
    like ``fill_*``/``tap*`` -- it looks only at ``circuit``'s own declared
    pins, so it also catches an equally power-only cell instantiated under
    an unrelated name (e.g. sky130's ``tapvpwrvgnd_1`` tap cell, which the
    superseded ``[!f]*`` glob workaround in ``docs/cli/lvs.md`` did not
    exclude).

    It is deliberately a *positive* test ("every pin is a known power pin")
    rather than the absence test it replaced ("no pin appears in the
    reference"). The absence test could not tell a power-only master apart
    from a signal-bearing master the reference simply never instantiates --
    the false negative this direction rules out by construction.

    A falsy/``None`` ``power_pin_names`` (no derivable universe) and a
    circuit with zero declared pins both return ``False`` -- neither is
    evidence this circuit is power-only, and the safe default on missing
    evidence is to leave the circuit in place and keep reporting the
    existing topology mismatch rather than risk masking a real one.
    """
    if circuit is None or not power_pin_names:
        return False
    pin_names = _circuit_pin_names(circuit)
    if not pin_names:
        return False
    return all(name in power_pin_names for name in pin_names)


def _boundary_supply_nets(
    circuit: Any, power_pin_names: frozenset[str]
) -> Iterator[Any]:
    """The **boundary-side** half of :func:`_layout_supply_net_names`'s two
    traversals: for every pin ``circuit`` itself declares whose name is a
    known power pin, the net that pin resolves to (issue #2136).

    This is the cell-interior view -- an extracted ``..._inv_1``'s own
    ``VPWR``/``VGND`` nets -- which :func:`_instance_supply_nets` alone
    never reaches, because the instance-side walk only ever sees nets in a
    *parent* circuit.
    """
    for pin in circuit.each_pin():
        pin_name = pin.name()
        if pin_name and pin_name.upper() in power_pin_names:
            yield circuit.net_for_pin(pin.id())


def _instance_supply_nets(
    circuit: Any, power_pin_names: frozenset[str]
) -> Iterator[Any]:
    """The **instance-side** half of :func:`_layout_supply_net_names`'s two
    traversals: for every subcircuit instance in ``circuit``, and every pin
    its master declares whose name is a known power pin, the net ``circuit``
    wires that pin to (issue #2136).

    This is the top-level power grid (the same edge set
    :func:`_power_pin_connections` walks for the ``power_connectivity``
    report), and it is the only evidence available in a circuit that does
    not itself declare a supply pin -- the case
    :func:`_boundary_supply_nets` cannot cover.
    """
    for sub in circuit.each_subcircuit():
        ref = sub.circuit_ref()
        if ref is None:
            continue
        for pin in ref.each_pin():
            pin_name = pin.name()
            if pin_name and pin_name.upper() in power_pin_names:
                yield sub.net_for_pin(pin.id())


def _supply_net_spellings(net: Any) -> set[str]:
    """Every upper-cased spelling under which ``net`` should be recognised
    as a supply net, or an empty set for an unnamed/``None`` net.

    Net names go through :func:`_name_or_none`, so a label-merged net is
    spelled exactly as ``net_correspondence[]``/``mismatches[].net`` spell
    it (``VPWR|VDD``); both the joined spelling and each ``|``-separated
    alias are returned, so a correspondence entry matches whichever of the
    two the compare-time net object reports.
    """
    name = _name_or_none(net)
    if not name:
        return set()
    spellings = {name.upper()}
    spellings.update(alias.upper() for alias in name.split("|") if alias)
    return spellings


def _layout_supply_net_names(
    layout_netlist: Any | None, power_pin_names: frozenset[str] | None
) -> frozenset[str] | None:
    """Every layout net name (upper-cased) that is demonstrably carrying a
    standard-cell **power/ground** pin, or ``None`` when there is not enough
    evidence to derive one (issue #2136).

    Structural, not name-pattern -- the same discipline
    :func:`_is_power_only_circuit` applies, and for the same
    PDK-independence reason (sky130's ``VPWR``/``VGND``/``VPB``/``VNB`` vs.
    gf180mcu's ``VDD``/``VSS``/``VNW``/``VPW``): a net qualifies only
    because something the *library* declares to be a power pin
    (``power_pin_names``, derived by :func:`_gate_level_power_pin_names`)
    actually lands on it. Two traversals, one helper each, both needed --
    neither subsumes the other, which is why this is a union and not a
    choice:

    * **Boundary side** -- :func:`_boundary_supply_nets`, the cell-interior
      view an instance-side walk never reaches.
    * **Instance side** -- :func:`_instance_supply_nets`, the top-level
      power grid, the only evidence in a circuit that declares no supply
      pin of its own.

    Each yielded net is recorded under every spelling
    :func:`_supply_net_spellings` gives it (the ``|``-joined label-merged
    form *and* each alias), so a correspondence entry matches whichever of
    the two the compare-time net object reports.

    Returns ``None`` for a falsy/``None`` ``power_pin_names`` (no derivable
    universe) or an unusable netlist, and an empty set when nothing
    qualifies -- callers treat both as "no evidence" and flag nothing, the
    same missing-evidence discipline :func:`_is_power_only_circuit` follows.
    """
    if layout_netlist is None or not power_pin_names:
        return None
    each_circuit = getattr(layout_netlist, "each_circuit", None)
    if not callable(each_circuit):
        return None
    names: set[str] = set()
    for circuit in each_circuit():
        for net in _boundary_supply_nets(circuit, power_pin_names):
            names.update(_supply_net_spellings(net))
        for net in _instance_supply_nets(circuit, power_pin_names):
            names.update(_supply_net_spellings(net))
    return frozenset(names)


def _supply_pin_universe(
    layout_netlist: Any | None,
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> _SupplyPinUniverse | None:
    """Bundle the two name sets :func:`_build_net_correspondence` needs to
    tell a verified net correspondence apart from a supply-net fallback
    (issue #2136), or ``None`` when either side yields no evidence.

    ``None`` is the answer for every ``reference.form`` other than
    ``"gate-level-verilog"`` (nothing licenses calling any pin name a power
    pin there -- the same restriction that scopes
    :func:`_prune_power_only_circuits` and the
    ``power_connectivity`` report), and for a ``gate-level-verilog`` run
    whose library pin orders could not be resolved. A ``None`` universe
    leaves ``net_correspondence[]`` byte-identical to what it was before
    this issue.
    """
    power_pin_names = _gate_level_power_pin_names(reference_netlist, library_pin_orders)
    if not power_pin_names:
        return None
    layout_supply_nets = _layout_supply_net_names(layout_netlist, power_pin_names)
    if not layout_supply_nets:
        return None
    return _SupplyPinUniverse(power_pin_names, layout_supply_nets)


def _apply_gate_level_port_aliases(
    port_aliases: Mapping[str, Mapping[str, str]],
    reference_netlist: Any,
) -> list[dict[str, Any]]:
    """Join a reference-side port's net onto its ``assign``-alias target's
    net, for every module a ``reference.form: "gate-level-verilog"``
    conversion carried a port-to-port (or port-to-internal-net) alias for
    (issue #2021, the other half of issue #1994's 77 false LVS errors).

    **The gap this closes.** ``convert_gate_level_verilog`` resolves an
    ``assign <alias> = <target>;`` statement transparently for every
    *instance* connection (see ``verilog_netlist.py``'s own docstring), but
    never for a module's own declared port list -- a ``.SUBCKT`` boundary
    pin whose only Verilog-level connection was an ``assign`` is emitted as
    its own pin, with nothing inside the body ever referencing its name
    (every instance that would have used it was rewritten to the alias's
    *target* instead). Reading that SPICE back with ``NetlistSpiceReader``
    creates exactly the isolated, zero-device/zero-terminal net that
    implies, which ``NetlistComparer`` then reports as an unmatched pin/net
    even when the layout is completely correct -- gate-level Verilog
    routinely carries a port-to-port alias like ``assign dbg_uart_byte[i] =
    rx_byte[i];``, where the layout has exactly one physical net serving
    both names.

    **Fix, applied here rather than in the SPICE text.** SPICE has no
    pin-alias primitive, and repeating one net name across two ``.SUBCKT``
    header positions would just drop the other, distinct declared name --
    so this runs *after* ``NetlistSpiceReader`` has built the real
    ``kdb.Circuit``/``kdb.Net``/``kdb.Pin`` objects, using
    ``Circuit.connect_pin()`` to move the alias port's declared pin onto its
    canonical target's net. Deliberately not ``Circuit.join_nets()``: that
    method merges the two nets' *pins* into one (renamed to a
    comma-joined string), which loses the alias port's own name entirely --
    verified directly against a real ``kdb.Netlist`` in this issue's own
    investigation. ``connect_pin`` instead leaves both declared pins in
    place, each still individually named, now both pointing at the same
    net -- the exact "one net, two named pins" shape a correctly-wired
    layout already has, so the two sides compare cleanly on both the
    pin-name match and the net match. The alias port's now-disconnected
    original net (zero terminals, zero pins, zero subcircuit pins once its
    pin is moved off it) is left for :func:`_purge_emptied_nets` to remove,
    exactly like every other emptied net this module already cleans up.

    ``port_aliases`` is
    :func:`klayout_tools.verilog_netlist.collect_gate_level_port_aliases`'s
    own return shape, ``{<module name>: {<alias port>: <canonical net>}}``
    -- every module ``_read_reference_netlist`` parsed with at least one
    port-to-port/port-to-net alias, empty (the common case) for every
    reference this fix does not apply to.

    A multi-hop alias chain (``assign c = b; assign b = a;``) collapses
    every alias port for one canonical target into a single
    :func:`_mismatch` entry and is applied correctly regardless of
    dict-iteration order: every alias in a group is looked up and moved
    onto the *same*, already-resolved ``canonical_net`` object, never
    re-resolved by name after an earlier move in the same group could have
    changed what that name refers to.

    Never raises: a module name that does not resolve to a circuit in
    ``reference_netlist``, or an alias/canonical port name that does not
    resolve to a net inside that circuit (should not happen in practice --
    the mapping was derived from the exact same parse
    ``convert_gate_level_verilog`` used to build the SPICE this netlist was
    read from -- but this is an internal consistency hook, not a
    caller-facing contract like ``hints.same_nets``), is silently skipped
    rather than treated as a request error.

    Returns one ``severity: "warning"``
    :data:`CATEGORY_TOPOLOGY_REFERENCE_PORT_ALIAS_JOINED` entry per joined
    canonical net (naming every alias port folded into it), so a
    ``"match"`` reached this way stays auditable -- the same disclosure
    discipline :func:`_apply_reference_device_bulk`/
    :func:`_apply_reference_placeholder_values` follow for their own
    reference-side fixups. Empty when ``port_aliases`` is empty (the common
    case).
    """
    entries: list[dict[str, Any]] = []
    any_joined = False
    for module_name in sorted(port_aliases):
        aliases = port_aliases[module_name]
        if not aliases:
            continue
        circuit = reference_netlist.circuit_by_name(module_name)
        if circuit is None:
            continue
        # Group by canonical target so a multi-hop chain joins every alias
        # for one target in a single pass -- see the docstring above.
        groups: dict[str, list[str]] = {}
        for alias_name, canonical_name in sorted(aliases.items()):
            if alias_name == canonical_name:
                continue
            groups.setdefault(canonical_name, []).append(alias_name)
        for canonical_name in sorted(groups):
            alias_names = groups[canonical_name]
            canonical_net = circuit.net_by_name(canonical_name)
            if canonical_net is None:
                continue
            joined: list[str] = []
            for alias_name in alias_names:
                alias_pin = circuit.pin_by_name(alias_name)
                alias_net = circuit.net_by_name(alias_name)
                if alias_pin is None or alias_net is None or alias_net is canonical_net:
                    continue
                circuit.connect_pin(alias_pin, canonical_net)
                joined.append(alias_name)
            if not joined:
                continue
            any_joined = True
            entries.append(
                _mismatch(
                    CATEGORY_TOPOLOGY_REFERENCE_PORT_ALIAS_JOINED,
                    "warning",
                    f"reference circuit '{module_name}' declared port(s) "
                    f"{', '.join(sorted(joined))} via a plain 'assign "
                    f"<port> = ...;' alias (request.reference.form: "
                    f'"gate-level-verilog") -- joined onto the same net as '
                    f"'{canonical_name}' before comparing, matching a "
                    f"layout with a single physical net for these names, "
                    f"instead of reporting them as unmatched (see "
                    f"docs/cli/lvs.md, "
                    '"topology.reference_port_alias_joined")',
                    "reference",
                    circuit={"layout": None, "reference": module_name},
                    details={
                        "canonical_net": canonical_name,
                        "aliased_ports": sorted(joined),
                    },
                )
            )
    if any_joined:
        _purge_emptied_nets(reference_netlist)
    return entries


def _purge_named_circuits(netlist: Any, removed_names: Sequence[str]) -> None:
    """Remove every circuit named in ``removed_names`` from ``netlist``,
    along with every subcircuit instance of it, then purge any net left
    empty as a result -- the mechanical removal step shared by the
    layout-side and reference-side halves of
    :func:`_prune_power_only_circuits` (issues #1622, #2244). A no-op for an
    empty ``removed_names``.

    Every instance of a to-be-removed circuit type must be detached first --
    ``Netlist.purge_circuit`` refuses (leaves a dangling reference behind)
    if the circuit still has callers.
    """
    removed_set = set(removed_names)
    if not removed_set:
        return
    for circuit in list(netlist.each_circuit()):
        for sub in list(circuit.each_subcircuit()):
            ref = sub.circuit_ref()
            if ref is not None and ref.name in removed_set:
                circuit.remove_subcircuit(sub)
    for name in removed_names:
        target = netlist.circuit_by_name(name)
        if target is not None:
            netlist.purge_circuit(target)
    # Issue #500's precedent: a net left with no terminals/pins/subcircuit
    # pins once its only connection (the just-removed instance) is gone is
    # not part of the netlist's topology by any definition -- see
    # `_purge_emptied_nets`'s own docstring.
    _purge_emptied_nets(netlist)


def _reference_power_only_masters(
    reference_netlist: Any,
    library_pin_orders: Mapping[str, list[str]] | None,
    power_pin_names: frozenset[str] | None,
    *,
    keep_circuit: Any | None = None,
) -> list[str]:
    """The sorted names of every circuit in ``reference_netlist`` that is a
    power-only library master -- the reference-side counterpart to
    :func:`_is_power_only_circuit` (issue #2244).

    **Cannot reuse `_is_power_only_circuit` directly.** That function
    classifies a circuit by its *own* declared pins, which is correct for a
    layout-side abstraction (whose declared pin list is the real PDK pin
    order, e.g. ``klt extract --abstract-cells``' output) but wrong for a
    ``gate-level-verilog`` reference's own ``.SUBCKT`` stub for the same
    cell: :func:`~klayout_tools.verilog_netlist.convert_gate_level_verilog`
    only ever emits the pins the Verilog actually connects (see that
    module's "No power/ground pins" note), so a power-only master's
    reference-side stub can declare **zero** pins at all when every instance
    leaves every pin unconnected -- exactly the shape a DEF-derived
    ``write_verilog`` reference produces for a filler/tap/endcap instance
    (``library__fill_4 FILLER_0_10 ();``, an empty connection list, because
    the cell has no signal pins to connect in the first place). Zero
    declared pins is what :func:`_is_power_only_circuit` treats as "no
    evidence" and refuses to prune (the same discipline that keeps a
    genuinely unresolvable circuit from being masked).

    So this checks the *library's* full declared pin order for the same
    cell name instead -- the same ``library_pin_orders`` lookup
    :func:`_gate_level_power_pin_evidence` already performs to find which
    masters the reference instantiates at all, reused here rather than
    re-derived. A circuit qualifies when its name resolves to a library
    cell whose full declared pin order is non-empty and is a subset of
    ``power_pin_names``; ``keep_circuit`` (the caller's own resolved
    reference top circuit, when given) is excluded by identity, mirroring
    :func:`_prune_power_only_circuits`'s layout-side guard for the same
    "never prune the compare target itself" reason.
    """
    if not library_pin_orders or not power_pin_names:
        return []
    by_upper_name = {
        str(cell).upper(): pins for cell, pins in library_pin_orders.items()
    }
    names: set[str] = set()
    for circuit in reference_netlist.each_circuit():
        if circuit == keep_circuit:
            continue
        pins = by_upper_name.get(str(circuit.name).upper())
        if not pins:
            continue
        pin_set = {pin.upper() for pin in pins if pin}
        if pin_set and pin_set <= power_pin_names:
            names.add(str(circuit.name))
    return sorted(names)


def _power_only_pruned_mismatch(
    removed_layout_names: Sequence[str], removed_reference_names: Sequence[str]
) -> dict[str, Any]:
    """Build the single ``topology.power_only_pruned`` disclosure for
    :func:`_prune_power_only_circuits`'s layout-side removals,
    reference-side removals, or both (issue #2244) -- always exactly one
    entry regardless of how many circuits were removed or from which
    side(s), so a ``"match"`` reached this way stays auditable without the
    entry count itself becoming a second signal.
    """
    total = len(removed_layout_names) + len(removed_reference_names)
    if removed_layout_names and removed_reference_names:
        side = "both"
        description = (
            "removed circuit(s) whose every declared pin is a power/ground "
            "pin of the reference library, and every instance of them, from "
            "both the layout and the reference before comparing -- the "
            "reference itself instantiated a power-only master (e.g. a "
            "DEF-derived write_verilog filler/tap/endcap instance with no "
            f"signal connections) -- {total} circuit(s): layout "
            f"{', '.join(removed_layout_names)}; reference "
            f"{', '.join(removed_reference_names)} (see docs/cli/lvs.md, "
            '"topology.power_only_pruned")'
        )
    elif removed_layout_names:
        side = "layout"
        description = (
            "removed layout-side circuit(s) whose every declared pin is a "
            "power/ground pin of the reference library (a pin the library "
            "declares but the gate-level-Verilog reference never carries), "
            f"and every instance of them, before comparing -- {total} "
            f"circuit(s): {', '.join(removed_layout_names)} (see "
            'docs/cli/lvs.md, "topology.power_only_pruned")'
        )
    else:
        side = "reference"
        description = (
            "removed reference-side circuit(s) whose every declared pin "
            "(per the reference library's own .subckt declaration) is a "
            "power/ground pin -- the reference itself instantiated a "
            "power-only master (e.g. a DEF-derived write_verilog "
            "filler/tap/endcap instance with no signal connections) that "
            "has no counterpart on the layout side -- and every instance "
            f"of them, before comparing -- {total} circuit(s): "
            f"{', '.join(removed_reference_names)} (see docs/cli/lvs.md, "
            '"topology.power_only_pruned")'
        )
    return _mismatch(CATEGORY_TOPOLOGY_POWER_ONLY_PRUNED, "warning", description, side)


def _prune_power_only_circuits(
    layout_netlist: Any,
    reference_netlist: Any,
    library_pin_orders: Mapping[str, list[str]] | None,
    *,
    layout_keep_name: str | None = None,
    reference_keep_name: str | None = None,
) -> dict[str, Any] | None:
    """Issue #1622 (layout side) / #2244 (reference side): remove every
    circuit whose entire declared pin list is power/ground -- a filler/
    tap-cell master, e.g. sky130's unconditionally-inserted
    ``tapvpwrvgnd_1`` tap cell (``place_and_route.py``'s ``_TAPCELL_CELLS``)
    or a ``fill_*`` row-gap filler (issue #1442) -- along with every
    subcircuit instance of it, from **whichever side(s) actually
    instantiate it** before the compare runs.

    **Why removal, not just suppressing its own mismatch report.** A
    ``klt place-and-route`` ``verilog_path`` reference conversion never
    instantiates a cell with no logic function at all (see
    ``verilog_netlist.py``'s "No power/ground pins" note), so a power-only
    layout circuit has nothing on that reference to describe it. Filtering
    the resulting finding out of ``mismatches[]`` after the fact (the
    obvious first instinct, and what a ``_build_mismatches``-level check
    would do) fixes nothing: ``status`` is always derived from
    ``compare()``'s own boolean result, never re-derived from
    ``mismatches[]`` (see this module's docstring and ``run_lvs``), so the
    verdict would stay ``"mismatch"``. It is also not the only finding --
    left in place, the *containing* circuit fails to verify too:
    ``NetlistComparer`` cannot pair the container's subcircuit-instance list
    against the other side's (one side has an extra instance the other
    cannot describe), and reports a second, consequential ``circuit could
    not be matched to a counterpart`` finding for the *container* (KLayout's
    own ``circuit_skipped`` event, both sides present). Removing the circuit
    and its instances before ``compare()`` ever runs is the only thing that
    makes the container's own comparison clean and the verdict ``"match"``.

    **Not every ``write_verilog`` consumer honors the "never instantiates a
    power-only cell" assumption** (issue #2244). A gate-level netlist
    produced by reading a routed DEF back through ``write_verilog`` (or any
    other DEF-derived netlist writer, as opposed to ``klt
    place-and-route``'s own ``verilog_path`` writer) contains a
    filler/tap/endcap instantiation line per placed physical-only component,
    each with an empty connection list (``library__fill_4 FILLER_0_10
    ();``). Read back through
    :func:`~klayout_tools.verilog_netlist.convert_gate_level_verilog`, each
    such cell type becomes its own zero-pin ``.SUBCKT`` stub on the
    *reference* side -- a power-only master the freshly-layout-pruned
    layout side can no longer match, surfacing as a ``topology`` "circuit
    could not be matched to a counterpart" error cascade (one per
    reference type, one per reference instance) stranded around an
    otherwise-clean ``power_connectivity: "match"`` verdict. This function
    therefore prunes symmetrically: a master's type and every instance of it
    are removed from the layout side, the reference side, or both,
    whichever actually instantiate it -- never just one side unconditionally.

    Mirrors the known-safe downstream workaround issue #1622 cites
    (2AMLogic/sky130-fpga issue #20: stripping a layout-side ``.SUBCKT``
    whose pin list is a non-empty subset of ``{VPWR, VGND, VPB, VNB}``, plus
    its instance lines, from the extracted SPICE netlist before calling
    ``klt lvs``) -- implemented natively here, and structurally
    (:func:`_is_power_only_circuit`/:func:`_reference_power_only_masters`'s
    pin-list checks against the power-pin universe
    :func:`_gate_level_power_pin_names` derives from the PDK library's own
    pin-order data) rather than a hardcoded, per-PDK power-pin name table or
    a cell-name glob.

    **Scoped to ``reference.form: "gate-level-verilog"`` only** -- the caller
    invokes this for that form alone, and that restriction is load-bearing,
    not caution. ``library_pin_orders`` (the resolved standard-cell
    library's full ``.subckt`` pin orders) exists only for that form, and
    the power-pin derivation it feeds is sound only because a
    ``gate-level-verilog`` reference is a *conversion of the same design*
    through that known library, guaranteed never to carry a power pin's
    *connection* (whether or not it emits the pin itself). A
    ``"plain-element"``/``"subckt-call"`` reference is arbitrary SPICE with
    no library behind it, so nothing there licenses calling any pin name a
    power pin -- applying an unscoped version of this masked a genuine
    unmatched signal circuit (a layout-only ``.SUBCKT`` with pins
    ``A``/``Y`` against a flat reference declaring only ``IN``/``OUT``
    vanished from the report), which
    ``test_run_lvs_power_only_pruning_is_scoped_to_gate_level_verilog``
    now guards against.

    Note this classification is broader than "filler/tap" by design: it
    also catches the other *purely* physical cells a P&R flow inserts
    without the logic netlist knowing -- decoupling capacitors, whose PDK
    pin list is supplies and well ties only -- which are equally invisible
    to a signal-only compare and equally not a topology defect. It stops
    exactly where the evidence does: a physical-only cell that declares
    any non-power pin (e.g. sky130's antenna diode, whose pin list
    includes ``DIODE``) is **not** pruned, because nothing in the data
    establishes that pin as power/ground. That is the intended direction of
    error -- an un-pruned cell costs a reported topology mismatch, a
    wrongly-pruned one masks a real defect. Every removal is named in the
    returned disclosure, so a ``"match"`` that depended on one stays
    auditable from the report alone.

    ``layout_keep_name``/``reference_keep_name`` (the caller's own
    ``layout.top``/``reference.top``, when given) are never pruned even if
    they happen to classify as power-only -- this runs before
    ``_select_circuit`` resolves the actual compare targets, and removing
    the very circuit the caller asked to compare would turn a
    classification edge case into a confusing "circuit not found" error
    instead of a clean no-op. Not expected to matter in practice (a real
    design's top circuit always has genuine signal I/O), but cheap to guard
    against.

    Returns a single ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "topology.power_only_pruned"``, built by
    :func:`_power_only_pruned_mismatch`) naming every circuit removed from
    every side pruned, so a ``"match"`` reached this way is never silently
    indistinguishable from one reached against either netlist's original,
    unpruned shape -- or ``None`` when nothing was power-only on either side
    (the common case, and always the case when there is no derivable
    power-pin universe, e.g. a ``reference_netlist`` with no library-cell
    circuits at all), in which case there is nothing to disclose.
    """
    power_pin_names = _gate_level_power_pin_names(reference_netlist, library_pin_orders)
    if not power_pin_names:
        return None
    # Resolved through the netlist's own `circuit_by_name` (whatever
    # case-folding convention it applies -- e.g. a `NetlistSpiceReader`
    # netlist matches `"top"` against a circuit actually named `"TOP"`) and
    # compared by object equality -- a plain name-string comparison against
    # `circuit.name` would miss that case-folding and could let this guard
    # silently fail to protect the real top circuit. Deliberately *not*
    # `cell_index`-based (unlike `_prune_extra_top_circuits`'s own
    # object-identity check): a `kdb.Netlist` read straight from SPICE/
    # Verilog text, with no backing `kdb.Layout`, reports `cell_index == 0`
    # for every circuit -- a real, pre-existing gap in that comparison,
    # tracked separately (issue #1657) rather than fixed here (out of this
    # issue's own scope) -- so this guard uses `==`, which compares
    # correctly in that same no-Layout-backing case.
    layout_keep_circuit = (
        layout_netlist.circuit_by_name(layout_keep_name)
        if layout_keep_name is not None
        else None
    )
    removed_layout_names = sorted(
        {
            circuit.name
            for circuit in layout_netlist.each_circuit()
            if circuit != layout_keep_circuit
            and _is_power_only_circuit(circuit, power_pin_names)
        }
    )
    # Only resolved (and only matters) when there is something to guard --
    # keeps this call site usable against a lightweight `each_circuit()`-only
    # test double that does not implement `circuit_by_name` when no reference
    # top name is given (see `_reference_power_only_masters`'s own tests).
    reference_keep_circuit = (
        reference_netlist.circuit_by_name(reference_keep_name)
        if reference_keep_name is not None
        else None
    )
    removed_reference_names = _reference_power_only_masters(
        reference_netlist,
        library_pin_orders,
        power_pin_names,
        keep_circuit=reference_keep_circuit,
    )
    if not removed_layout_names and not removed_reference_names:
        return None
    _purge_named_circuits(layout_netlist, removed_layout_names)
    _purge_named_circuits(reference_netlist, removed_reference_names)
    return _power_only_pruned_mismatch(removed_layout_names, removed_reference_names)


# --------------------------------------------------------------------------- #
# Power/ground connectivity check (issue #1952)
# --------------------------------------------------------------------------- #


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


def _power_pins_derivation(masters: list[str], corroborated: bool) -> dict[str, Any]:
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
    power_pin_names, masters, corroborated = _gate_level_power_pin_evidence(
        reference_netlist, library_pin_orders
    )
    derivation = _power_pins_derivation(masters, corroborated)
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


# --------------------------------------------------------------------------- #
# Environment / reproducibility
# --------------------------------------------------------------------------- #


def _engine_version() -> str | None:
    import klayout

    return getattr(klayout, "__version__", None)
