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
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING, Any, NamedTuple

from . import env_provenance
from ._paths import _load_request_json, _resolve_relative
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape
from ._provenance import (
    INPUT_ROLE_LAYOUT,
    INPUT_ROLE_NETLIST,
    _content_hash,
    build_provenance,
    explicit_deck_options,
    sha256_file,
    version_drift_advisories,
)
from ._report_verify import (
    VOLATILE_PROVENANCE_PATHS,
    build_check_result,
    build_rerun_result,
    get_path,
    hash_check,
    resolve_committed_path,
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
from .lvs_combine import (
    _combine_circuit_devices_safely,
    _combine_devices_safely,
    _correct_combine_parameters,
    _flatten_netlist_safely,
    _net_for_terminal_or_none,
    _parse_combine_devices,
    _parse_combine_devices_max_attempts,
    _parse_combine_devices_per_circuit,
    _purge_emptied_nets,
    _resolve_combine_devices_per_circuit_targets,
    _series_fold_offset_absorbed_warnings,
    _unmatched_combine_devices_per_circuit_warnings,
    _validate_combine_device_classes,
)
from .lvs_finfet import (
    bulk_disclosures,
    finfet_bulk_replay_options,
    prepare_finfet_comparison,
    prepare_finfet_request,
    reference_reader,
    sliced_reference_file,
)
from .lvs_gate_level import (
    _apply_gate_level_port_aliases,
    _boundary_supply_nets,  # noqa: F401
    _circuit_pin_names,  # noqa: F401
    _gate_level_power_pin_evidence,  # noqa: F401
    _gate_level_power_pin_evidence_detail,  # noqa: F401
    _gate_level_power_pin_names,  # noqa: F401
    _instance_supply_nets,  # noqa: F401
    _is_power_only_circuit,  # noqa: F401
    _layout_supply_net_names,  # noqa: F401
    _name_or_none,  # noqa: F401
    _power_only_pruned_mismatch,  # noqa: F401
    _prune_power_only_circuits,
    _purge_named_circuits,  # noqa: F401
    _reference_power_only_masters,  # noqa: F401
    _reference_signal_pin_names,  # noqa: F401
    _subcircuit_parent_name,  # noqa: F401
    _subcircuit_ref_name,  # noqa: F401
    _supply_net_spellings,  # noqa: F401
    _supply_pin_universe,
)
from .lvs_mismatch import (
    _annotate_case_collision_merges,
    _apply_tolerance_snaps,
    _body_net_warnings,
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
from .lvs_power import (
    _body_verification_report,
    _body_verification_unchecked,
    _parse_power_connectivity,
    _power_connectivity_report,
    _power_connectivity_unchecked,
    _with_stub_power_connectivity_reason,
)
from .lvs_runset import RUNSET_ENGINE, run_lvs_klayout_runset
from .lvs_supply import parse_supply_nets, supply_fragmentation_findings
from .lvs_timeout import (
    COMPARE_TIMEOUT_CODE,
    CompareTimeout,
    heartbeat,
    parse_compare_timeout,
    run_with_deadline,
)
from .netlist_capacitor_recovery import (
    custom_device_classes_for_deck,
    make_capacitor_class_recovery_reader,
    parse_capacitor_class_comments,
    resistor_classes_for_deck,
)
from .pdk import PdkNotFoundError, find_pdk, list_pdks
from .pdk_models import MOS_FLAVOUR_PROPERTY, flavoured_mos_class_name
from .place_and_route_portable import portable_response
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
#:
#: Bumped 1 -> 2 (issue #2659): the top-level `layout` field changed from the
#: request document's own `layout.file`/`layout.netlist` string, echoed
#: verbatim, to the `{path, scope}` shape `env_provenance.repo_relative_path`
#: already defines -- the same normalization `klt sim`/`klt pex` adopted in
#: issue #1261 and `klt drc`/`klt extract` in #2659. A committed `klt lvs`
#: envelope is cited verbatim by a `klt signoff` manifest, and the echoed
#: string was anchored to whatever directory the producing run happened to
#: use (often that host's absolute path), so `klt signoff`'s
#: `input_verified` freshness gate (issue #2196) could re-verify such a
#: citation only there and reported drift from every other checkout.
#: `reference` is deliberately *not* part of this bump and stays the verbatim
#: echo: it is pinned by its own `environment.reference_sha256` digest and is
#: not the artifact `provenance.input` hashes. See `_report_path` below.
SCHEMA_VERSION = 2

#: ``klayout`` (in-process ``NetlistComparer``) is the primary engine;
#: ``netgen`` (issue #343) is a second, independent comparator wrapped as a
#: subprocess in netlist-vs-netlist mode only -- see this module's docstring
#: for the scope boundary the accepted spike drew around it.
SUPPORTED_ENGINES = ("klayout", "netgen", "klayout-runset")

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


class _ReferenceNetlistParseError(LvsError):
    """The reference netlist's final ``NetlistSpiceReader`` read raised
    (issue #2250).

    A private :class:`LvsError` subclass -- message and exception chain are
    byte-identical to the plain ``LvsError`` it replaces, so every existing
    caller, the CLI, and the JSON error envelope see no difference. It exists
    only so :func:`rerun_lvs_report` can recognise *this* failure (and no
    other layout/deck/engine failure) and add ``--rerun``-specific context
    about the omitted ``reference.form``.
    """


def _compare_timeout_mismatch(
    timeout: CompareTimeout, context: Mapping[str, Any]
) -> dict[str, Any]:
    """The `compare.timeout` entry for an expired `options.compare_timeout_s`
    (issue #4165): elapsed time and both sides' sizes in `details`."""
    return _mismatch(
        CATEGORY_COMPARE_TIMEOUT,
        "error",
        f"the netlist compare did not finish within "
        f"options.compare_timeout_s={timeout.timeout_s:g}s; no LVS verdict "
        "was reached (this is neither a match nor a mismatch)",
        "both",
        details={
            "code": COMPARE_TIMEOUT_CODE,
            "timeout_s": timeout.timeout_s,
            "elapsed_s": round(timeout.elapsed_s, 3),
            **context,
        },
    )


def _compare_timeout_replay_options(echoed: Mapping[str, Any]) -> dict[str, Any]:
    """`{"compare_timeout_s": n}` when set, else `{}` (issue #4165): used both
    to echo the option and to replay it, so an unset option leaves the report
    and the reconstructed request byte-identical to before."""
    value = echoed.get("compare_timeout_s")
    return {} if value is None else {"compare_timeout_s": value}


def _compare_context(layout_circuit: Any, reference_circuit: Any) -> dict[str, int]:
    """Device/net/pin counts of both sides, for the heartbeat and the
    `compare.timeout` entry (issue #4165)."""
    context: dict[str, int] = {}
    for side, circuit in (("layout", layout_circuit), ("reference", reference_circuit)):
        context[f"{side}_devices"] = sum(1 for _ in circuit.each_device())
        context[f"{side}_nets"] = sum(1 for _ in circuit.each_net())
        context[f"{side}_pins"] = circuit.pin_count()
    return context


def _guarded_compare(
    comparer: Any,
    layout_netlist: Any,
    reference_netlist: Any,
    timeout_s: float | None,
    context: Mapping[str, Any],
) -> tuple[bool, CompareTimeout | None]:
    """`_compare_netlists`, with an expired deadline returned instead of
    raised: `(False, timeout)` (issue #4165)."""
    try:
        return (
            _compare_netlists(
                comparer,
                layout_netlist,
                reference_netlist,
                timeout_s=timeout_s,
                context=context,
            ),
            None,
        )
    except CompareTimeout as exc:
        return False, exc


def _timeout_mismatches(
    mismatches: list[dict[str, Any]],
    timeout: CompareTimeout | None,
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """`mismatches` unchanged, or just the `compare.timeout` entry."""
    if timeout is None:
        return mismatches
    return [_compare_timeout_mismatch(timeout, context)]


def _compare_netlists(
    comparer: Any,
    layout_netlist: Any,
    reference_netlist: Any,
    *,
    timeout_s: float | None = None,
    context: Mapping[str, Any] | None = None,
) -> bool:
    """Run ``NetlistComparer.compare`` and turn an engine-internal failure
    into :class:`LvsError` (issue #4134).

    KLayout's comparer can trip an internal assertion (e.g. ``bt_count !=
    failed_match``) that surfaces as ``RuntimeError``. That is a tool failure,
    not a verdict, so it must not be reported as ``status: "mismatch"``; the
    CLI maps ``LvsError`` to the standard error envelope and exit code 1.

    Issue #4165: ``timeout_s`` (``options.compare_timeout_s``) first proves
    the compare terminates inside the budget in a killable child, raising
    :class:`~klayout_tools.lvs_timeout.CompareTimeout` otherwise; either way
    a stderr heartbeat reports progress (see ``lvs_timeout``).
    """
    try:
        if timeout_s is not None:
            run_with_deadline(
                lambda: comparer.compare(layout_netlist, reference_netlist),
                timeout_s,
                context or {},
            )
        with heartbeat(context or {}):
            return comparer.compare(layout_netlist, reference_netlist)
    except RuntimeError as exc:
        raise LvsError(
            "KLayout's netlist comparer failed internally and could not "
            f"reconcile the layout and reference netlists ({exc}); no LVS "
            "verdict was reached"
        ) from exc


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


def _merge_deck_flags(
    request: dict[str, Any],
    engine: str,
    deck_file: str | None,
    deck_vars: dict[str, str] | None,
) -> None:
    """Fold ``--deck-file``/``--deck-var`` CLI values into ``request`` in
    place (issue #3206). A relative ``deck_file`` is made absolute against the
    current directory first -- the request's own relative paths resolve
    against the request file's directory, which is not what a flag means."""
    if deck_file is None and not deck_vars:
        return
    flags = "/".join(
        f
        for f, given in (("--deck-file", deck_file), ("--deck-var", deck_vars))
        if given
    )
    if engine != RUNSET_ENGINE:
        raise LvsError(
            f"{flags} only applies to engine '{RUNSET_ENGINE}' (this request "
            f"uses engine '{engine}')"
        )
    if deck_file is not None:
        if request.get("deck_file") is not None:
            raise LvsError(
                "--deck-file conflicts with request.deck_file: set one, not "
                "both (a flag never silently overrides the request document)"
            )
        request["deck_file"] = os.path.abspath(deck_file)
    if deck_vars:
        if request.get("deck_vars") is not None:
            raise LvsError(
                "--deck-var conflicts with request.deck_vars: set one, not "
                "both (a flag never silently overrides the request document)"
            )
        request["deck_vars"] = dict(deck_vars)


def _report_path(path: str | None, *, repo_root: str | None) -> dict[str, Any]:
    """Normalise one input-path field for this module's own JSON response
    (issue #2659) -- a thin, module-local wrapper over
    :func:`~klayout_tools.env_provenance.repo_relative_path` rather than a
    second normalizer, so `klt lvs`'s reports, `klt drc`/`klt extract`/`klt
    sim`/`klt pex`'s and `klt env-provenance`'s own emitter agree on exactly
    one ``{path, scope}`` shape.

    Applied to the ``layout`` field only: it is the artifact
    ``provenance.input.content_hash`` pins and therefore the one `klt
    signoff`'s ``input_verified`` gate re-hashes for an `lvs` citation
    (``signoff.py``'s ``_INPUT_ARTIFACT_FIELDS``). ``reference`` stays the
    verbatim request echo -- it is pinned by its own
    ``environment.reference_sha256``.

    Pass the *resolved* layout path (``layout_hash_source``, i.e. the file
    actually hashed), never the request's own echo: a bare relative string
    would be resolved against this process's cwd rather than the request
    file's own directory, which is not what the request meant.
    """
    return env_provenance.repo_relative_path(path, repo_root=repo_root)


def _report_repo_root(path: str | None) -> str | None:
    """The repo root this run's own ``layout`` field is normalised against --
    resolved from the **layout's** own location, not the request document's.

    The field describes the layout, so its ``scope`` has to answer "is the
    layout inside a repo", and the two are not the same question: a request
    document may name a layout anywhere, and the inline/stdin request forms
    have no document directory at all (``request_dir`` is this process's cwd
    there -- see :func:`load_request_arg`), which would make the field's
    scope depend on where `klt` happened to be invoked from. Anchoring on the
    layout itself is also the same "walk up from the input" choice `klt
    drc`/`klt extract`/`klt pex` make for their own input-path fields, so the
    four verbs agree on what ``scope: "repo"`` means."""
    if path is None:
        return None
    return env_provenance.find_repo_root(os.path.dirname(os.path.abspath(path)))


def run_lvs(
    request: str,
    *,
    deck_file: str | None = None,
    deck_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the netlist compare declared by ``request``.

    ``deck_file``/``deck_vars`` (issue #3206) carry the ``klt lvs
    --deck-file``/``--deck-var`` CLI values for ``engine: klayout-runset``;
    they fill the request's ``deck_file``/``deck_vars`` fields, and setting
    both a flag and its request field is a clean :class:`LvsError`.

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

    ``layout`` is the committable ``{path, scope}`` object
    :func:`~klayout_tools.env_provenance.repo_relative_path` defines (issue
    #2659, ``schema_version`` ``2``), built from the *resolved* layout path
    this run actually hashed into ``provenance.input.content_hash`` --
    ``scope: "repo"`` with a path relative to the repo the **layout** sits
    in (:func:`_report_repo_root`), ``{"path": null, "scope": "external"}``
    when it sits in none. It is therefore no longer the request's own verbatim
    ``layout.file``/``layout.netlist`` echo, which was anchored to whatever
    directory the producing run used and so could be re-verified only
    there: a committed `klt lvs` envelope is cited verbatim in `klt signoff`
    evidence. ``reference`` *is* still the verbatim echo -- it is pinned by
    its own ``environment.reference_sha256``, not by ``provenance.input``.

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

    ``layout.label_layers`` (issue #2686) is the request-document counterpart
    of ``klt extract --label-layer`` (that verb's own ``label_layers`` request
    field, issue #2656): ``{"<role>": [layer, datatype] | null}`` redirecting
    which GDS purpose inline extraction reads net/pin name text from, for a
    layout whose names sit on a text purpose the curated deck does not read.
    Extraction-only: a non-empty override needs ``layout.file`` +
    ``layout.deck`` and is a clean :class:`LvsError` on the pre-extracted
    ``layout.netlist`` shape (see :func:`_parse_layout_label_layers`).
    Omitting it (or giving ``null``/``{}``) extracts exactly as before. An
    applied override is echoed as the response's top-level ``label_layers``
    field (absent otherwise) and replayed by ``--check --rerun``.

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

    ``device.geometry_not_compared`` (issue #2461) closes the last silent
    gap in that family, and needs no option at all to reach: KLayout marks
    only ``R`` primary on ``DeviceClassResistor`` and only ``C`` on
    ``DeviceClassCapacitor``, so such a class's *entire geometry* is outside
    every default compare -- a reference resistor whose ``W`` is an order of
    magnitude off the layout's still reports ``status: "match"`` with
    ``error_count: 0``. One ``severity: "warning"`` entry per participating
    class per run says so, on every ``reference.form``; the top-level
    ``device_parameter_coverage`` block is the gradeable counterpart, built
    from the same determination (see :func:`_device_parameter_coverage`).
    Under ``reference.form: "subckt-call"`` the two disclosures combine:
    the primary is excluded as a placeholder *and* the geometry is
    secondary, so nothing about the device is compared at all.

    ``options.anchor_top_level_pins`` (issue #2692, ``"engine": "klayout"``
    only) decides whether every top-level pin name declared on **both** sides
    is used as a compare anchor -- ``NetlistComparer`` pairs nets by topology
    and never by top-level pin name, so without it two boundary nets with
    identical topology (every bit of a black-box macro's bus wired straight
    through) are interchangeable and a bit-reversed bus compares clean.
    Default **on** for ``reference.form: "gate-level-verilog"`` (whose
    reference is generated from the same DEF/Verilog the layout's pin names
    come from), **off** for every other form; ``false`` restores the
    topology-only compare. Every anchored run discloses the pins it anchored
    as a ``severity: "warning"`` ``topology.top_level_pins_anchored`` entry,
    and a pin pairing the comparer refuses is reported as ``hints.rejected``
    carrying ``details.anchored_top_level_pin``. See
    :func:`_parse_anchor_top_level_pins`, :func:`_anchor_top_level_pins_enabled`
    and :func:`_apply_top_level_pin_anchors`.
    """
    loaded, request_dir = load_request_arg(request)
    request = loaded

    engine = request.get("engine", "klayout")
    if engine not in SUPPORTED_ENGINES:
        raise LvsError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )
    _merge_deck_flags(loaded, engine, deck_file, deck_vars)
    portable_paths = _validate_portable_paths(loaded.get("portable_paths"))

    layout_spec = request["layout"]
    if not isinstance(layout_spec, dict):
        raise LvsError("request.layout must be a JSON object")
    reference_spec = request["reference"]
    if not isinstance(reference_spec, dict):
        raise LvsError("request.reference must be a JSON object")

    if engine == RUNSET_ENGINE:
        # Issue #2849: the PDK's own runset does extraction and compare; the
        # verdict is read back from its .lvsdb (see lvs_runset.py).
        return _run_runset_response(request, request_dir, portable_paths)

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

    # Issue #2686: `layout.label_layers` mirrors `klt extract --label-layer`
    # (request field `label_layers`, issue #2656) for inline extraction only
    # -- see `_parse_layout_label_layers` for the shape and the
    # extraction-only rule. `None` when omitted/`null`.
    label_layers = _parse_layout_label_layers(layout_spec)
    # Issue #2853: `layout.abstract_cells`/`abstract_cell_lef` mirror `klt
    # extract --abstract-cells`/`--abstract-cell-lef` for inline extraction
    # only -- see `_parse_layout_abstract_cells` for the shape and the
    # extraction-only rule.
    layout_abstraction = _parse_layout_abstract_cells(layout_spec, request_dir)

    options = request.get("options") or {}
    # Issue #2813: a FinFET extraction deck's devices are only correct with
    # their geometry-counted `nfin` compared exactly -- every request shape or
    # option that would drop or relax it is refused up front, and the
    # nfin-preserving reference reader is selected below.
    finfet_layout, finfet_bulk = prepare_finfet_request(
        layout_spec, reference_spec, options, engine
    )
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
    # Issue #2692: top-level pin anchoring -- parsed unconditionally here (the
    # same discipline `power_connectivity` above follows) so a malformed value
    # is a clean request error for every reference form, with the
    # form-dependent default resolved below once `reference_form` is known.
    # See `_apply_top_level_pin_anchors` for the soundness gap it closes.
    (
        anchor_top_level_pins_requested,
        anchor_top_level_pins_echo,
    ) = _parse_anchor_top_level_pins(options, engine)
    strict_top_level_pin_count = _parse_strict_top_level_pin_count(options, engine)
    compare_mos_flavour = _parse_compare_mos_flavour(
        options, engine, layout_spec, reference_spec
    )
    supply_nets = parse_supply_nets(options)
    # Issue #1085: opt-in, per-side structural flatten -- see
    # `_flatten_netlist_safely`'s docstring for the full rationale (`klt
    # extract` is always flat, so a hierarchical reference/pre-extracted
    # layout netlist otherwise can never structurally match it).
    flatten_reference = bool(options.get("flatten_reference", False))
    # Issue #4165: opt-in deadline for the `klayout` engine's compare.
    compare_timeout_s = parse_compare_timeout(options, engine)
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
        # Issue #2659: the request's own verbatim `layout.file`/
        # `layout.netlist` echo is no longer what the response reports --
        # `layout` is now built from `layout_hash_source` (the resolved
        # path) via `_report_path`, so the field is portable across
        # checkouts. `_resolve_layout` still returns the echo for callers
        # that want it.
        _layout_echo,
        layout_hash_source,
        extracted_netlist_path,
        layout_net_label_positions,
        layout_pin_renames,
        layout_abstracted_cells,
    ) = _resolve_layout(
        layout_spec,
        request_dir,
        keep_extracted,
        combine_devices_enabled,
        deck_options,
        label_layers,
        reference_spec.get("deck"),
        layout_abstraction,
    )
    # Inspect original scoped nets before any transform can discard a power
    # island or flatten independent child definitions into a shared scope.
    supply_findings = supply_fragmentation_findings(
        _select_circuit(layout_netlist, layout_spec.get("top"), "layout"),
        supply_nets,
        layout_net_label_positions,
    )

    _split_layout_mos_flavours(layout_netlist, compare_mos_flavour)

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

    # Issue #2952: the layout's circuit names are snapshotted before
    # `flatten_layout` removes the per-macro circuits, so the
    # `reference.unresolved_subckt_call` scan's same-named-layout-circuit
    # exemption (a black-box/`--abstract-cells` macro paired by name) holds
    # whether or not the layout is flattened.
    layout_circuit_names = {
        circuit.name.upper() for circuit in layout_netlist.each_circuit()
    }
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
    # Issue #2692: resolved here, the first point both the caller's own
    # `options.anchor_top_level_pins` and the `reference.form` its default
    # depends on are known -- see `_anchor_top_level_pins_enabled` for why the
    # default is form-dependent (on for `gate-level-verilog`, off elsewhere).
    anchor_top_level_pins = _anchor_top_level_pins_enabled(
        anchor_top_level_pins_requested, reference_form
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
    # Issue #1622: resolved here rather than inside `_read_reference_netlist`
    # so the *same* mapping serves both consumers with one read of each
    # pin-order source -- the conversion's own `pin_order_lookup` and, below,
    # the power-pin universe `_prune_power_only_circuits` needs.
    reference_pin_orders: dict[str, list[str]] | None = None
    # Issue #1901: the PDK `_resolve_gate_level_pin_orders` resolves
    # internally to read the library's pin-order source -- reused here (not
    # re-resolved) purely so `provenance.pdk` can record it below, matching
    # `klt extract`/`klt sta`/`klt place-and-route`. Stays `None` for every
    # other `reference.form`, which resolves no PDK at all.
    reference_pdk_info: dict[str, Any] | None = None
    # Issue #2693: one record per pin-order file actually read -- a
    # macro-backed design resolves pin orders from several libraries (the
    # standard cells plus each macro's own one-file-per-macro library), so
    # `provenance.pdk` alone no longer says where a cell's pin order came
    # from. Empty for every other `reference.form`, which reads none.
    reference_pin_order_sources: list[dict[str, Any]] = []
    # Issue #2693: `reference.library`/`reference.pin_order_files` are
    # validated inside the resolver (both are specific to this one form, and
    # "neither was given" is its own error there), so this branch stays a
    # single call.
    # Issue #4353: the resolved files are also echoed (`reference_conversion`)
    # so `--rerun` is independent of the original request directory.
    reference_pin_order_files: list[str] = []
    if reference_form == "gate-level-verilog":
        reference_pin_order_files = _resolve_reference_pin_order_files(
            reference_spec, request_dir
        )
        resolved_pin_orders = _resolve_gate_level_pin_orders(
            reference_spec.get("library"),
            reference_spec.get("pdk"),
            reference_spec.get("pdk_root"),
            pin_order_files=reference_pin_order_files,
        )
        reference_pin_orders = resolved_pin_orders.pin_orders
        reference_pdk_info = resolved_pin_orders.pdk_info
        reference_pin_order_sources = resolved_pin_orders.sources
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
        resistor_value_classes=_resistor_value_classes(compare_parameters),
        deck_options=(
            deck_options
            if layout_spec.get("deck") == reference_spec.get("deck")
            else None
        ),
        gate_level_port_aliases=reference_gate_level_port_aliases,
        finfet=finfet_layout,
        finfet_top=reference_spec.get("top"),
        mos_flavour_classes=compare_mos_flavour,
    )
    # Issue #2952: scanned now, before any flatten/prune below erases the
    # hierarchy that names each offending instance. Appended to
    # `mismatches[]` and applied to `status` at the end of the compare.
    unresolved_subckt_call_findings = _unresolved_reference_subckt_calls(
        reference_netlist,
        layout_circuit_names,
        form=reference_form,
        top=reference_spec.get("top"),
        reference_path=reference_netlist_path,
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

    # Issue #2813: FinFET comparison setup -- reference fingers merged with the
    # extractor's combiner, the opt-in bulk policy applied (and disclosed),
    # NFIN compared exactly on both sides.
    finfet_bulk_records = prepare_finfet_comparison(
        finfet_layout, layout_netlist, layout_circuit, reference_netlist, finfet_bulk
    )

    # Issue #2853 (option (b)): resolved here, after both sides' optional
    # flatten, because a flattened side no longer carries the stub instances
    # the reason talks about -- see `_with_stub_power_connectivity_reason`.
    power_connectivity = _with_stub_power_connectivity_reason(
        power_connectivity, reference_form, layout_circuit
    )

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
            # Issue #3017: disclose the (N-1) head/end terms the fold above
            # absorbed (a no-op after a symmetric degrade).
            combine_warnings.extend(
                _series_fold_offset_absorbed_warnings(
                    layout_snapshot,
                    layout_netlist,
                    layout_deck,
                    combine_incomplete=combine_incomplete,
                )
            )

    bulk_warnings: list[dict[str, Any]] = []
    short_nets_warnings: list[dict[str, Any]] = []
    placeholder_warnings: list[dict[str, Any]] = []
    tolerance_warnings: list[dict[str, Any]] = []
    compare_parameter_warnings: list[dict[str, Any]] = []
    geometry_warnings: list[dict[str, Any]] = []
    # Issue #2461: `null` for `engine: "netgen"` (KLayout device-class state
    # is what this block reports, and that engine never builds one) -- the
    # same always-present-but-nullable convention `hints_applied`/
    # `device_classes` follow, never a spuriously empty `[]` that would read
    # as "no resistor/capacitor class participated".
    device_parameter_coverage: list[dict[str, Any]] | None = None
    # Issue #2461: every `(class, parameter)` pair a request-side hook took
    # out of *this* run's compare, so the classification pass downstream never
    # reports one of them as a `device.property` error in the same report that
    # discloses it as excluded. Kept as two sets so the coverage block can name
    # which hook excluded each one.
    placeholder_excluded: set[tuple[str, str]] = set()
    compare_scoped_excluded: set[tuple[str, str]] = set()
    # Issue #2654: the subset of `compare_scoped_excluded` that
    # `options.compare_parameters` actually took *out of* the compare -- the
    # parameters that were primary before `enable_parameter` ran. The option
    # can only narrow, so scoping a class to one subset also disables every
    # sibling the caller did not name; a sibling the class itself declares
    # secondary was never compared, so the coverage block must not attribute
    # its exclusion to the caller's option (see
    # `_record_excluded_parameters`). The full set above still drives the
    # classification pass's suppression, unchanged.
    compare_scoped_narrowed: set[tuple[str, str]] = set()
    # Issue #1998: populated only for `engine == "klayout"` -- the `netgen`
    # branch below rejects any `request.hints` outright (no equivalent hook
    # in that engine's scope), so this stays empty for every netgen run.
    equivalent_pins_applied: dict[str, list[list[str]]] = {}
    # Issue #2692: populated only for `engine == "klayout"` (the netgen engine
    # has no `same_nets` equivalent -- `_parse_anchor_top_level_pins` already
    # rejected an explicit opt-in there), and only when anchoring is enabled
    # for this reference form. `anchored_pins` feeds `_build_mismatches`'s
    # same-nets reconciliation (a refused anchor is a `hints.rejected`
    # finding); `anchor_warnings` is the single disclosure entry appended with
    # the other request-side disclosures further down.
    anchored_pins: list[_TopLevelPinAnchor] = []
    anchor_warnings: list[dict[str, Any]] = []
    # Issue #3014: the caller's explicit `hints.constant_nets` bindings; same
    # roles as `anchored_pins`/`anchor_warnings` above.
    constant_bindings: list[_ConstantNetBinding] = []
    constant_warnings: list[dict[str, Any]] = []

    if engine == "klayout":
        # Issue #2653: resolve a mask-option (metal-option/probe-pad/e-fuse
        # trim) reference's behavioural option construct to one concrete
        # drawn state *before* anything else below -- every other reference-
        # side normalisation in this block (`device_bulk`, placeholder
        # values, `compare_parameters`, the parameter-coverage summary)
        # should see the already-resolved device/net set, exactly as it
        # would for a reference hand-authored at that one option code to
        # begin with. Opt-in via `hints.short_nets`; a no-op (returns `[]`
        # immediately) for every request that does not declare it, so a
        # reference with no option construct present stays byte-identical to
        # before this hook existed.
        short_nets_warnings = _apply_short_nets_hint(
            request.get("hints") or {},
            reference_netlist,
            reference_circuit,
        )

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
            placeholder_excluded,
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
            compare_scoped_excluded,
            compare_scoped_narrowed,
        )

        # Issue #2461: read the two netlists' device-class state *after* every
        # hook above has had its say (`enable_parameter` flips `is_primary` in
        # place, so this has to run downstream of `_apply_compare_parameters`
        # to describe the compare that is actually about to happen) and
        # *before* the comparer is built. Read-only -- it changes nothing
        # about the compare, it only records what the compare will cover.
        geometry_warnings, device_parameter_coverage = _device_parameter_coverage(
            layout_netlist,
            reference_netlist,
            placeholder_excluded,
            compare_scoped_narrowed,
        )
        excluded_parameters = placeholder_excluded | compare_scoped_excluded

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
        # Issue #2692: applied *after* `_apply_hints` so a hand-written
        # `hints.same_nets` pair stays the authority on the nets it names (see
        # `_apply_top_level_pin_anchors`'s `declared` argument), and before
        # `compare()` for the same reason `same_nets` itself must be: the
        # assertions are comparer state read during the compare, not netlist
        # state.
        # Issue #3014: applied after `_apply_hints` (a hand-written
        # `hints.same_nets` pair stays the authority on the nets it names) and
        # before the pin anchors, which then leave a bound net alone.
        constant_bindings, constant_warnings = _apply_constant_nets(
            comparer,
            request.get("hints") or {},
            layout_circuit,
            reference_circuit,
            declared=same_nets_hints,
        )
        anchored_pins, anchor_warnings = _apply_top_level_pin_anchors(
            comparer,
            layout_circuit,
            reference_circuit,
            enabled=anchor_top_level_pins,
            declared=[*same_nets_hints, *_constant_binding_pairs(constant_bindings)],
        )

        # `logger` is already bound via the `NetlistComparer(logger)` constructor
        # above, so the 2-arg overload is used here (not the 3-arg one, which
        # would pass a second, redundant logger reference).
        compare_context = _compare_context(layout_circuit, reference_circuit)
        compare_result, compare_timeout = _guarded_compare(
            comparer,
            layout_netlist,
            reference_netlist,
            compare_timeout_s,
            compare_context,
        )

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
                snaps = _collect_tolerance_snaps(
                    logger, parameter_tolerance, excluded_parameters
                )
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
                # Issue #2692: re-declared on the fresh comparer for exactly
                # the same reason the hints above are -- an anchor is comparer
                # state, so the second pass would otherwise compare on
                # topology alone and could reach a `"match"` the first,
                # anchored pass had already refused.
                # Issue #3014: bindings are comparer state too, so they are
                # re-applied on the fresh comparer exactly like the hints.
                constant_bindings, constant_warnings = _apply_constant_nets(
                    comparer,
                    request.get("hints") or {},
                    layout_circuit,
                    reference_circuit,
                    declared=same_nets_hints,
                )
                anchored_pins, anchor_warnings = _apply_top_level_pin_anchors(
                    comparer,
                    layout_circuit,
                    reference_circuit,
                    enabled=anchor_top_level_pins,
                    declared=[
                        *same_nets_hints,
                        *_constant_binding_pairs(constant_bindings),
                    ],
                )
                compare_result, compare_timeout = _guarded_compare(
                    comparer,
                    layout_netlist,
                    reference_netlist,
                    compare_timeout_s,
                    compare_context,
                )
                if compare_result or compare_timeout:
                    break

        mismatches = _build_mismatches(
            logger,
            layout_netlist,
            reference_netlist,
            same_nets_hints=same_nets_hints,
            # Issue #2692: an anchored pin pairing the comparer refused is a
            # real finding, reported through the same reconciliation a
            # hand-written `hints.same_nets` refusal goes through.
            anchored_pins=[
                (anchor.layout_net, anchor.reference_net, anchor.pin)
                for anchor in anchored_pins
            ],
            # Issue #3014: a refused constant binding is a `hints.rejected`
            # finding attributed through `details.declared_constant_net`.
            constant_bindings=constant_bindings,
            excluded_parameters=excluded_parameters,
        )
        # Issue #4165: after a timeout nothing the (empty) logger produced
        # describes the design; report only the timeout.
        mismatches = _timeout_mismatches(mismatches, compare_timeout, compare_context)
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
        status = STATUS_INCONCLUSIVE if compare_timeout else status
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
        # no equivalent to `same_nets`/`equivalent_pins`/`short_nets` in this
        # scope), and -- unlike the `klayout` engine's in-process compare --
        # an external subprocess whose own exit code is not trustworthy on
        # its own (see `_run_netgen_lvs`).
        if request.get("hints"):
            raise LvsError(
                "request.hints (same_nets/equivalent_pins/short_nets) is "
                "only supported for engine 'klayout' -- the netgen engine "
                "has no equivalent hook in this issue's netlist-vs-netlist "
                'scope (see docs/cli/lvs.md, "Engine")'
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
        # Issue #2673: the same conversion-provenance + all-reference-
        # instances-zero proof the klayout branch applies, but with the
        # exclusion carried into netgen's own setup (`property ... delete`)
        # instead of a `klayout.db` hook. The disclosure is identical.
        netgen_exclusions: list[tuple[str, str, str]] = []
        placeholder_warnings = _apply_reference_placeholder_values(
            reference_placeholder_classes,
            layout_netlist,
            reference_netlist,
            mutate_classes=False,
            netgen_exclusions=netgen_exclusions,
        )
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
            excluded_properties=netgen_exclusions,
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

    # Issue #2845: opt-in strict pin-count check, at the one point both
    # engines converge, on the circuits actually compared (after any
    # flatten/prune) -- the same counts reported as `counts.pins`.
    status, strict_pin_findings = _strict_pin_count_check(
        strict_top_level_pin_count, status, counts, layout_circuit, reference_circuit
    )
    mismatches.extend(strict_pin_findings)

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

    # Issue #2653: same rationale as `bulk_warnings` below -- a
    # `hints.short_nets` disclosure records a *request-side* reference
    # transform (device drops / net merges) applied before the compare, not a
    # `NetlistComparer` event, so it is appended here rather than folded into
    # `_build_mismatches`. Always `severity: "warning"`: it never changes
    # `status`, it only keeps a match reached through the resolved option
    # construct from being indistinguishable from a fully independent one.
    # Extended unconditionally (an empty list is a no-op) rather than behind
    # an `if` like its neighbours, so this hook adds no branch to `run_lvs`'s
    # already-baselined cyclomatic complexity -- see
    # `scripts/check_complexity_baseline.py`.
    mismatches.extend(short_nets_warnings)

    # Issue #2692: same rationale as `short_nets_warnings` just above -- a
    # `topology.top_level_pins_anchored` disclosure records a *request-side*
    # strengthening of the compare (assertions declared on the comparer before
    # `compare()` ran), not a `NetlistComparer` event, so it is appended here
    # rather than folded into `_build_mismatches`. Always `severity:
    # "warning"`: anchoring only ever removes a degree of freedom, so it never
    # changes `status` on its own -- it keeps a match that rests on
    # name-anchored boundary pins from being indistinguishable from one
    # reached by topology alone. Extended unconditionally (an empty list is a
    # no-op) for the same `run_lvs`-complexity reason, see
    # `scripts/check_complexity_baseline.py`.
    mismatches.extend(anchor_warnings)
    # Issue #3014: same rationale -- a request-side `hints.constant_nets`
    # disclosure (empty list is a no-op); a warning never changes `status`.
    mismatches.extend(constant_warnings)
    # Issue #2813: `options.finfet_bulk` disclosure (empty list is a no-op).
    mismatches.extend(
        bulk_disclosures(finfet_bulk_records, CATEGORY_DEVICE_FINFET_BULK_TIED)
    )

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

    # Issue #2461: same append-here rationale once more -- which parameters a
    # device class declares primary is a property of the two netlists' classes
    # as prepared for this run, not a `NetlistComparer` event, so it is
    # appended here rather than folded into `_build_mismatches`. Always
    # `severity: "warning"`: it never changes `status`, it only keeps a match
    # reached without comparing a resistor/capacitor's geometry at all from
    # being indistinguishable from one where the two sides' geometry actually
    # agreed. Extended unconditionally (like `family_findings` below, unlike
    # the `if`-guarded warnings above): the list is empty on every run with no
    # resistor/capacitor class to report.
    mismatches.extend(geometry_warnings)

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

    # Issue #2854: same append-here rationale as `family_findings` above --
    # a promoted pin net carrying 2+ declared pin names is a pre-compare
    # determination the inline extraction's rename pass made about the
    # layout's declared interface, not a `NetlistComparer` event. Like
    # `device.class_family_unsatisfiable` it is `severity: "error"` but
    # diagnostic only: it never moves `status` on its own -- the compare
    # that already ran reports the mismatch this entry explains (the
    # layout's one joined boundary net against the reference's two separate
    # pins). Extended unconditionally (an empty list is a no-op when no
    # rename ran or no short was found) so this hook adds no branch to this
    # already-complexity-baselined function.
    mismatches.extend(_declared_pin_short_mismatches(layout_pin_renames))

    mismatches.extend(supply_findings)
    # Issue #4200: explain a net.merged that is really a case-fold name collision.
    _annotate_case_collision_merges(mismatches, layout_circuit, "file" in layout_spec)
    # Issue #2952: an unresolved reference subcircuit call is a pre-compare
    # determination (see `_unresolved_reference_subckt_calls`); unlike the
    # diagnostic-only errors above it *does* move `status` -- below, after
    # the combine downgrade, so it can never be hidden behind
    # `"inconclusive"` either.
    mismatches.extend(unresolved_subckt_call_findings)
    mismatches.sort(key=_sort_key)

    # Issue #2845: a strict `pin.count` error is exempt from the downgrade
    # below -- `combine_devices` only folds devices and never changes a
    # circuit's declared pins, so the pin-count difference is a real
    # mismatch whether or not combining applied, and downgrading would hide
    # it behind `"inconclusive"`.
    if combine_incomplete and status == "mismatch" and not strict_pin_findings:
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
    status = _force_mismatch_on(status, unresolved_subckt_call_findings)

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

    provenance = build_provenance(
        deck_name=layout_deck_name,
        deck_path=(deck_source_path(layout_deck_name) if layout_deck_name else None),
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
    )
    if reference_pin_order_sources:
        # Issue #2693: *which* pin-order sources this compare's cell pin
        # orders came from -- one record per file read, in read order. A
        # macro-backed `gate-level-verilog` reference resolves several
        # (`sky130_fd_sc_hd`'s aggregate library file plus one file per
        # instantiated SRAM macro), so `provenance.pdk` alone cannot answer
        # "where did this cell's pin order come from". Omitted entirely for
        # every other `reference.form`, which reads no pin-order source at
        # all -- never a spuriously present empty list.
        provenance["pin_order_sources"] = reference_pin_order_sources

    report = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        # Issue #2659: the committable `{path, scope}` shape, built from the
        # *resolved* layout path (`layout_hash_source` -- the file
        # `provenance.input.content_hash` below actually hashes), never the
        # request's own cwd-dependent echo. See `_report_path`. `reference`
        # stays the verbatim echo, pinned by `environment.reference_sha256`.
        "layout": _report_path(
            layout_hash_source, repo_root=_report_repo_root(layout_hash_source)
        ),
        # Issue #2686: the applied `layout.label_layers` override (which GDS
        # purpose inline extraction read net/pin names from, per role) --
        # see `_label_layers_echo`. Absent when no override was applied, so
        # those reports stay byte-identical to before.
        **_label_layers_echo(label_layers),
        # Issue #2853: the applied `layout.abstract_cells`/
        # `abstract_cell_lef` black-box abstraction and its per-cell report
        # -- see `_layout_abstraction_echo`. Absent when no abstraction was
        # requested, so those reports stay byte-identical to before.
        **_layout_abstraction_echo(layout_abstraction, layout_abstracted_cells),
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
        # Issue #2673: the reference-conversion context `--rerun` needs to
        # replay a `reference.form: "subckt-call"` reference -- see
        # `_reference_conversion_echo`. Absent for every other form, so those
        # reports stay byte-identical to before.
        **_reference_conversion_echo(
            reference_form, reference_spec, reference_pin_order_files
        ),
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
            # Issue #2682: the resolved retry budget (the parser's default
            # when omitted), echoed even when combining is off -- it
            # describes the configured budget, not retries consumed.
            "combine_devices_max_attempts": combine_devices_max_attempts,
            "flatten_layout": flatten_layout,
            "flatten_reference": flatten_reference,
            # Issue #4165: present only when the caller set it, so a request
            # without the option produces a byte-identical report.
            **_compare_timeout_replay_options({"compare_timeout_s": compare_timeout_s}),
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
            # Issue #2692: `null` when the option was omitted (the
            # form-dependent default applies -- on for
            # `reference.form: "gate-level-verilog"`, off elsewhere), else the
            # caller's own boolean verbatim. Same always-present-but-nullable
            # convention `power_connectivity` above follows, echoed unresolved
            # so a committed report round-trips back into the request document
            # it came from; what this run actually *did* is reported as the
            # `topology.top_level_pins_anchored` disclosure in `mismatches[]`.
            "anchor_top_level_pins": anchor_top_level_pins_echo,
            # Issue #2845: the effective boolean (`false` when omitted).
            "strict_top_level_pin_count": strict_top_level_pin_count,
            # Issue #3650: the effective boolean (`false` when omitted).
            "compare_mos_flavour": compare_mos_flavour,
            "supply_nets": supply_nets,
            # Issue #2813: `null` when omitted, else the resolved
            # `{"nmos": <net>, "pmos": <net>}` bulk-tie policy (opt-in; the
            # applied ties are disclosed in `mismatches[]`).
            "finfet_bulk": finfet_bulk,
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
        # Issue #3014: the `hints.constant_nets` bindings applied
        # (`{constant: layout net}`), `null` when none -- the echo `--rerun`
        # replays, like `hints_applied` above.
        "constant_nets_applied": _constant_nets_echo(constant_bindings),
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
        # Issue #2461: per participating resistor/capacitor device class,
        # which of its declared parameters this compare actually covered and
        # why each of the others did not -- the gradeable form of the
        # `device.geometry_not_compared` warning (and of the
        # `device.placeholder_value`/`device.parameter_excluded` warnings,
        # whose exclusions are labelled with their own `reason`). Reported
        # beside `status`, never folded into it: a class whose geometry was
        # never compared still reports `status: "match"` when the compare
        # matched -- that is precisely the disclosure gap this answers. `null`
        # for `"engine": "netgen"`. See `_device_parameter_coverage`.
        "device_parameter_coverage": device_parameter_coverage,
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
        # Issue #2693: built just above the return (rather than inline here)
        # so the `gate-level-verilog` form's `pin_order_sources` can be added
        # to it without duplicating this whole call per form.
        "provenance": provenance,
        "mismatches": mismatches,
        "net_correspondence": net_correspondence,
    }
    return _finish_response(
        report,
        portable=portable_paths,
        request_dir=request_dir,
        pdk_root=(reference_pdk_info or {}).get("root"),
    )


def _validate_portable_paths(value: Any) -> bool:
    """Optional ``request.portable_paths`` (issue #3798, mirroring
    ``klt place-and-route``'s from #3820): rewrite every absolute host path in
    the response to a committable form (see
    :mod:`klayout_tools.place_and_route_portable`). Omitted/``None`` is
    ``False``: the default response is unchanged."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise LvsError("request.portable_paths must be a boolean")
    return value


def _run_runset_response(
    request: dict[str, Any], request_dir: str, portable_paths: bool
) -> dict[str, Any]:
    """Run the ``klayout-runset`` engine and finish its response.

    Issue #3988: under ``portable_paths`` the tool's identity (its basename)
    is kept for ``environment.klayout_binary`` instead of the generic
    external-path object; the version is recorded separately in
    ``engine_version``."""
    runset_report = run_lvs_klayout_runset(request, request_dir)
    binary = runset_report.get("environment", {}).get("klayout_binary")
    response = _finish_response(
        runset_report, portable=portable_paths, request_dir=request_dir
    )
    if portable_paths and isinstance(binary, str):
        response["environment"]["klayout_binary"] = os.path.basename(binary)
    return response


def _finish_response(
    report: dict[str, Any],
    *,
    portable: bool,
    request_dir: str,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Apply the opt-in ``portable_paths`` rewrite to the returned report
    only (the compare itself ran on the real paths); no-op when not
    requested."""
    if portable:
        return portable_response(report, request_dir=request_dir, pdk_root=pdk_root)
    return report


# --------------------------------------------------------------------------- #
# --check / --rerun: verify a previously committed report (issue #1106)
# --------------------------------------------------------------------------- #


def _input_hash_check(
    field: str,
    expected: str | None,
    echoed: Any,
    report_path: str,
    override: str | None = None,
) -> dict[str, Any]:
    """One :func:`~klayout_tools._report_verify.hash_check` entry for an
    input path the committed report echoes back (``layout``/``reference``),
    with issue #2595's ``input_not_found`` diagnostic attached when that
    path does not name an existing file.

    :func:`~klayout_tools._provenance.sha256_file` returns ``None`` both for
    "the file is not there" and (vacuously) for "no path was recorded", so a
    plain ``hash_check`` renders an unresolvable input as ``actual: null`` --
    indistinguishable, to a reader, from a recorded hash that no longer
    matches. That is exactly the confusing shape issue #2595 reported when a
    committed request/report pair with *request-file-relative*
    ``layout``/``reference`` paths is ``--check``ed from a different
    directory.

    This does **not** change where the path is resolved: ``--check`` still
    anchors a relative echoed path to the current working directory, the
    same convention ``klt drc --check``'s ``file`` field uses (re-anchoring
    one verb and not the other would make the two silently disagree). It
    only makes the failure legible, as an *additional*, optional key on the
    entry -- ``expected``/``actual``/``match`` and the resulting ``status``
    are byte-identical to before:

    - ``path`` -- the echoed path verbatim, as the report recorded it.
    - ``resolved`` -- the absolute path that was actually looked for.
    - ``found_relative_to_report`` -- the absolute path the input *does*
      occupy relative to the committed report's own directory, when it is
      there; ``null`` otherwise (including for an echoed absolute path,
      which has no report-relative candidate to try).

    Kept local to this module rather than folded into the shared
    ``hash_check`` on purpose: ``klt drc --check`` resolves its own
    ``provenance.input`` path the identical way and would need the identical
    diagnostic to stay consistent, which is a deliberate follow-up, not a
    side effect of this one. Promote this helper to ``_report_verify`` if
    and when that happens.

    Issue #2659: ``echoed`` is resolved through
    :func:`klayout_tools._report_verify.resolve_committed_path`, so the
    ``{path, scope}`` object ``layout`` carries today is joined to the repo
    root of the committed report's own directory -- which is precisely the
    case the "known limitation" above describes, now *resolved* for the
    layout side rather than merely diagnosed. ``reference`` still echoes the
    request verbatim and keeps the cwd anchoring (and the diagnostic). An
    entry naming no path at all (``scope: "external"``/``"absent"``, or a
    report with no such field) resolves to ``None`` and renders exactly as
    an absent one always has.
    """
    path = resolve_committed_path(echoed, report_path=report_path, override=override)
    check = hash_check(field, expected, sha256_file(path))
    if path is None or os.path.isfile(path):
        return check
    found_relative_to_report: str | None = None
    if not os.path.isabs(path):
        candidate = os.path.join(os.path.dirname(os.path.abspath(report_path)), path)
        if os.path.isfile(candidate):
            found_relative_to_report = os.path.abspath(candidate)
    check["input_not_found"] = {
        "path": path,
        "resolved": os.path.abspath(path),
        "found_relative_to_report": found_relative_to_report,
    }
    return check


def check_lvs_report(
    report_path: str, *, layout_path: str | None = None
) -> dict[str, Any]:
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

    **Known limitation, now only for the reference side**:
    ``committed["reference"]`` is echoed *exactly as given* in the original
    request document (see :func:`run_lvs`'s docstring on
    ``reference_echo``), which may have been relative to that request
    *file's own directory* -- not necessarily the current working directory.
    This re-hashes it relative to the current working directory (the same
    convention ``klt drc --check``'s own pre-#2659 ``file`` field used); if
    the original request used request-file-relative paths, invoke
    ``--check`` from that same directory, or commit reports whose
    ``reference`` is already an absolute path. Issue #2595: that anchoring
    is unchanged, but hitting it no longer renders as a bare ``actual:
    null`` -- an echoed path that names no existing file gets an
    ``input_not_found`` block on its ``checks[]`` entry saying where it was
    looked for, and where it *was* found relative to the report's own
    directory when it is there. See :func:`_input_hash_check`.
    ``committed["layout"]`` no longer has this limitation at all: since
    issue #2659 it is the ``{path, scope}`` object, resolved against the
    repo root of the committed report's own directory, so it re-hashes
    correctly from any clone and from any working directory. A pre-#2659
    report whose ``layout`` is a bare string keeps the old anchoring, and
    the diagnostic above with it.

    ``layout_path`` (issue #2699, ``klt lvs --layout``) is an explicit layout
    that overrides the report's recorded ``layout`` unconditionally (see
    :func:`~klayout_tools._report_verify.resolve_committed_path`) -- the way
    to verify a report whose layout is ``scope: "external"``.

    Raises :class:`LvsError` for a missing/unparseable committed report --
    never a traceback.

    The result additionally carries an ``advisories`` list (issue #1373):
    entries naming a ``provenance.klt_version``/``provenance.klayout_version``
    that differs between the committed report and the engine currently
    running ``--check`` -- see :func:`version_drift_advisories`. This is
    purely informational: it never affects ``status`` (still driven only by
    the hash ``checks`` above) or the caller's exit code, and is never folded
    into ``checks``' ``[OK]``/``[DRIFTED]`` list, so a scripted gate grepping
    that list for a failure marker does not start matching on it.
    """
    committed = _load_committed_report(report_path, LvsError)
    checks = [
        _input_hash_check(
            "environment.layout_sha256",
            get_path(committed, ("environment", "layout_sha256")),
            committed.get("layout"),
            report_path,
            override=layout_path,
        ),
        _input_hash_check(
            "environment.reference_sha256",
            get_path(committed, ("environment", "reference_sha256")),
            committed.get("reference"),
            report_path,
        ),
    ]
    deck = get_path(committed, ("provenance", "deck"))
    if (
        committed.get("engine") != RUNSET_ENGINE
        and isinstance(deck, dict)
        and deck.get("name") is not None
    ):
        checks.append(
            hash_check(
                "provenance.deck.content_hash",
                deck.get("content_hash"),
                _content_hash(deck_source_path(deck["name"])),
            )
        )
    runset_hash = get_path(committed, ("provenance", "runset", "content_hash"))
    if committed.get("engine") == RUNSET_ENGINE and runset_hash is not None:
        # Issue #2849: the PDK runset is an input too -- a changed runset
        # can change the verdict without moving the layout or reference.
        checks.append(
            hash_check(
                "provenance.runset.content_hash",
                runset_hash,
                _content_hash(committed.get("deck_file")),
            )
        )
    result = build_check_result(report_path=report_path, checks=checks)
    result["advisories"] = version_drift_advisories(committed)
    return result


def _strict_top_level_pin_count_replay_options(
    echoed: Mapping[str, Any],
) -> dict[str, Any]:
    """Issue #2845: re-assert ``strict_top_level_pin_count`` only when it was
    on (``false`` is the default). A helper keeps
    :func:`_reconstruct_lvs_request` within its complexity baseline."""
    replay: dict[str, Any] = {}
    if echoed.get("strict_top_level_pin_count") is True:
        replay["strict_top_level_pin_count"] = True
    # Issue #3650: same rule for `compare_mos_flavour`.
    if echoed.get("compare_mos_flavour") is True:
        replay["compare_mos_flavour"] = True
    return replay


def _reconstruct_runset_request(
    committed: dict[str, Any], layout: str | None = None
) -> dict[str, Any]:
    """Issue #2849: replay a ``klayout-runset`` report from its own echo
    (``layout``/``reference``/``top``/``deck_file``/``deck_vars``/``options``).
    Re-running needs the same standalone ``klayout`` application; there is no
    fallback to another engine."""
    layout_spec: dict[str, Any] = {"file": layout or committed.get("layout")}
    if committed.get("top"):
        layout_spec["top"] = committed["top"]
    reference_spec: dict[str, Any] = {"netlist": committed.get("reference")}
    if committed.get("reference_top"):
        reference_spec["top"] = committed["reference_top"]
    request: dict[str, Any] = {
        "engine": RUNSET_ENGINE,
        "layout": layout_spec,
        "reference": reference_spec,
        "deck_file": committed.get("deck_file"),
    }
    if committed.get("deck_vars"):
        request["deck_vars"] = committed["deck_vars"]
    if committed.get("options"):
        request["options"] = committed["options"]
    return request


def _reconstruct_lvs_request(
    committed: dict[str, Any],
    *,
    report_path: str | None = None,
    layout_path: str | None = None,
) -> dict[str, Any]:
    """Best-effort reconstruction of a ``klt lvs`` request document from a
    previously committed report, for :func:`rerun_lvs_report`.

    ``report_path`` (issue #2659) is where the committed report was read
    from: the ``{path, scope}`` ``layout`` field is resolved against the repo
    root of that file's own directory
    (:func:`klayout_tools._report_verify.resolve_committed_path`), so the
    reconstructed ``layout.file``/``layout.netlist`` is an absolute path that
    resolves on *this* checkout. ``None`` (a caller holding only the decoded
    report) falls back to the current working directory's repo root; a
    committed report whose ``layout`` is a bare string -- every report
    predating #2659 -- reconstructs byte-identically either way.

    Reconstructs the fields the response actually echoes: ``engine``,
    ``layout`` (``file``+``deck``[+``deck_options``][+``label_layers``,
    issue #2686, from the report's own ``label_layers`` echo] when
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
    layout stream, in that case. A ``"subckt-call"`` reference is replayed
    from the report's ``reference_conversion`` echo (issue #2673; a report
    committed before it existed stays best-effort), as is a
    ``"gate-level-verilog"`` reference (issue #4353), but
    ``reference.device_bulk``
    and ``layout.top_cell_pins``/``declared_pins``/``pin_source_cells``/
    ``rename_declared_pins`` are
    never echoed anywhere in the response and are always omitted
    (reconstructed as each option's own default). Use ``--check`` (cheap
    mode) instead when any of these apply. When the omitted form makes the
    plain-element re-read of the reference fail to parse,
    :func:`_run_reconstructed_lvs` says so in the error (issue #2250).
    """
    deck = get_path(committed, ("provenance", "deck"))
    has_deck = isinstance(deck, dict) and deck.get("name") is not None
    top = committed.get("top")
    # Issue #1205: each side's own top. `reference_top` is additive -- a
    # report predating it only ever recorded the one `top`, which is exactly
    # the (symmetric-top) assumption this used to make unconditionally, so
    # falling back to it reconstructs such a report the same way as before.
    reference_top = committed.get("reference_top") or top

    # Issue #2659: the `{path, scope}` object resolved back to a path on this
    # checkout (a bare string passes through unchanged) -- see this
    # function's `report_path` docstring note.
    # Issue #2699: an explicit `--layout` wins over the recorded value.
    layout = resolve_committed_path(
        committed.get("layout"), report_path=report_path, override=layout_path
    )

    layout_spec: dict[str, Any] = {"top": top} if top else {}
    if has_deck:
        layout_spec["file"] = layout
        layout_spec["deck"] = deck["name"]
        # Issue #2394: replay only the options the *caller* pinned, not the
        # resolved set `provenance.deck.options` now records -- re-pinning a
        # silently-defaulted key would replay over exactly the deck-default
        # change a rerun exists to surface as drift.
        deck_options = explicit_deck_options(deck)
        if deck_options:
            layout_spec["deck_options"] = deck_options
        # Issue #2686: replay the applied label-purpose override, `null`
        # roles included; a report without the echo replays without one.
        layout_spec.update(_label_layers_replay(committed))
        # Issue #2853: replay the cell-level black-box abstraction too.
        layout_spec.update(_layout_abstraction_replay(committed))
    else:
        layout_spec["netlist"] = layout

    reference_spec: dict[str, Any] = {"netlist": committed.get("reference")}
    if reference_top:
        reference_spec["top"] = reference_top
    reference_spec.update(_reference_conversion_replay(committed))

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
    options.update(_compare_timeout_replay_options(echoed))
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
    # Issue #2692: re-assert the caller's own `anchor_top_level_pins` value
    # verbatim when the committed report carried one -- see
    # `_anchor_top_level_pins_replay_options` for why `false` must survive the
    # replay too (unlike the options above, it is not the default for every
    # form). Applied as an `update` of a helper's result, like
    # `_supply_nets_replay_options` below, rather than an inline branch:
    # `_reconstruct_lvs_request` is complexity-baselined (see
    # `scripts/check_complexity_baseline.py`).
    options.update(_anchor_top_level_pins_replay_options(echoed))
    options.update(_strict_top_level_pin_count_replay_options(echoed))
    # Issue #2682: replay the echoed retry budget verbatim (never through the
    # boolean flag loop); a malformed recorded value stays malformed so the
    # parser rejects it rather than it becoming an unrelated valid budget.
    options.update(_combine_devices_max_attempts_replay_options(echoed))
    # Includes []: explicitly disabling the finding must survive a replay.
    options.update(_supply_nets_replay_options(echoed))
    options.update(finfet_bulk_replay_options(echoed))
    if options:
        request["options"] = options
    # Issue #3014: replay the applied `hints.constant_nets` bindings; other
    # hints are not echoed and stay best-effort (see the docstring).
    request.update(_constant_nets_replay_hints(committed))
    return request


def _combine_devices_max_attempts_replay_options(
    echoed: dict[str, Any],
) -> dict[str, Any]:
    """``{"combine_devices_max_attempts": <echoed value>}`` when the committed
    report recorded one (issue #2682), else ``{}`` so a legacy report replays
    at the parser's default. The value is passed through unmodified, so a
    malformed recorded budget is rejected by the parser on replay rather than
    silently becoming a valid one."""
    key = "combine_devices_max_attempts"
    return {key: echoed[key]} if key in echoed else {}


def _reference_conversion_echo(
    reference_form: str,
    reference_spec: dict[str, Any],
    pin_order_files: list[str] | None = None,
) -> dict[str, Any]:
    """``{"reference_conversion": {...}}`` for a ``subckt-call`` or
    ``gate-level-verilog`` reference, else ``{}`` (issues #2673, #4353).

    The conversion is a function of the reference file *and* this context, so
    a report that records only the file cannot be replayed: ``--rerun`` would
    re-read the simulation-form SPICE / structural Verilog as plain-element
    SPICE. ``subckt-call`` echoes ``deck``/``device_map``; ``gate-level-
    verilog`` echoes ``library``/``pdk``/``pdk_root`` and the *resolved*
    ``pin_order_files`` (``null`` when omitted). Values are echoed as the
    caller gave them.
    """
    if reference_form == "subckt-call":
        return {
            "reference_conversion": {
                "form": reference_form,
                "deck": reference_spec.get("deck"),
                "device_map": reference_spec.get("device_map"),
            }
        }
    if reference_form == "gate-level-verilog":
        return {
            "reference_conversion": {
                "form": reference_form,
                "library": reference_spec.get("library"),
                "pdk": reference_spec.get("pdk"),
                "pdk_root": reference_spec.get("pdk_root"),
                "pin_order_files": list(pin_order_files) if pin_order_files else None,
            }
        }
    return {}


def _reference_conversion_replay(committed: dict[str, Any]) -> dict[str, Any]:
    """The ``request.reference`` keys that rebuild a committed report's
    ``reference_conversion`` echo (issues #2673, #4353); ``{}`` for a report
    without one (every other form, and any committed before the echo existed
    -- those stay best-effort, as documented)."""
    echoed = committed.get("reference_conversion")
    if not isinstance(echoed, dict):
        return {}
    if echoed.get("form") == "gate-level-verilog":
        spec: dict[str, Any] = {"form": "gate-level-verilog"}
        for key in ("library", "pdk", "pdk_root"):
            if echoed.get(key) is not None:
                spec[key] = echoed[key]
        if isinstance(echoed.get("pin_order_files"), list):
            spec["pin_order_files"] = list(echoed["pin_order_files"])
        return spec
    if echoed.get("form") != "subckt-call":
        return {}
    spec = {"form": "subckt-call"}
    if echoed.get("deck") is not None:
        spec["deck"] = echoed["deck"]
    if isinstance(echoed.get("device_map"), dict):
        spec["device_map"] = dict(echoed["device_map"])
    return spec


def _constant_nets_replay_hints(committed: dict[str, Any]) -> dict[str, Any]:
    """``{"hints": {"constant_nets": {...}}}`` when the committed report
    echoed applied bindings, else ``{}`` (issue #3014)."""
    applied = committed.get("constant_nets_applied")
    if isinstance(applied, dict) and applied:
        return {"hints": {"constant_nets": dict(applied)}}
    return {}


def _anchor_top_level_pins_replay_options(echoed: dict[str, Any]) -> dict[str, Any]:
    """``{"anchor_top_level_pins": <bool>}`` when the committed report echoed
    an explicit value for it, else ``{}`` (issue #2692).

    Includes ``false``: unlike every other option
    :func:`_reconstruct_lvs_request` replays, omitting this one does **not**
    mean "off" for every reference form -- a ``gate-level-verilog`` request
    that omits it gets anchoring *on* (see
    :func:`_anchor_top_level_pins_enabled`). Dropping an explicit ``false``
    would therefore replay the original compare with anchoring turned on, and
    report the resulting (correct, but different) verdict as drift.
    """
    requested = echoed.get("anchor_top_level_pins")
    return {"anchor_top_level_pins": requested} if isinstance(requested, bool) else {}


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
    ("environment", "klayout_binary"),  # Issue #2849: host-local path
}


def _run_reconstructed_lvs(request: dict[str, Any]) -> dict[str, Any]:
    """:func:`run_lvs` on a :func:`_reconstruct_lvs_request` result, adding
    ``--rerun`` context to a reference-netlist parse failure (issue #2250).

    A report without a ``reference_conversion`` echo (committed before issues
    #2673/#4353) reconstructs without ``reference.form``, so the reference is
    re-read as the default ``"plain-element"`` SPICE. When the original
    request used a non-default form (e.g. a
    ``"gate-level-verilog"`` ``.v`` reference), that read fails with the
    generic "could not parse reference netlist" error, which on its own does
    not point at the cause. The original form is unknowable from the report
    -- the reference's extension is only a hint -- so the added context says
    it *may* have been non-default, and names cheap ``--check`` (without
    ``--rerun``) as the workaround. The underlying message is kept verbatim
    and chained via ``raise ... from``. Every other failure (layout, deck,
    engine, or any other reference error) propagates unchanged.
    """
    try:
        return run_lvs(json.dumps(request))
    except _ReferenceNetlistParseError as exc:
        raise LvsError(
            f"{exc}. Note: --rerun reconstructs the request from the committed "
            "report, which never records reference.form, so the reference "
            'netlist was re-read as the default "plain-element" SPICE form; the '
            "original request may have used a non-default reference.form "
            '(e.g. "gate-level-verilog" or "subckt-call"). If so, verify this '
            "report with cheap mode instead: klt lvs --check <report> (without "
            '--rerun) -- see docs/cli/lvs.md, "Full mode (--rerun)".'
        ) from exc


def rerun_lvs_report(
    report_path: str, *, layout_path: str | None = None
) -> dict[str, Any]:
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

    ``layout_path`` (issue #2699, ``klt lvs --layout``) is an explicit layout
    that overrides the report's recorded ``layout`` unconditionally -- the way
    to rerun a report whose layout is ``scope: "external"``.

    Raises :class:`LvsError` for a missing/unparseable committed report, a
    report missing ``layout``/``reference`` to rerun, or any error the
    rerun itself raises -- never a traceback.
    """
    committed = _load_committed_report(report_path, LvsError)
    layout = resolve_committed_path(
        committed.get("layout"), report_path=report_path, override=layout_path
    )
    if layout is None or committed.get("reference") is None:
        raise LvsError(
            f"committed report has no 'layout'/'reference' field to rerun: "
            f"{report_path}"
        )
    request = (
        _reconstruct_runset_request(committed, layout)
        if committed.get("engine") == RUNSET_ENGINE
        else _reconstruct_lvs_request(
            committed, report_path=report_path, layout_path=layout_path
        )
    )
    fresh = _run_reconstructed_lvs(request)
    exclude = set(_LVS_RERUN_EXCLUDE_PATHS)
    exclude.update(
        (field,)
        for field in (
            "reference_top",
            "reference_conversion",
            "options",
            "power_connectivity",
            # Issue #1983: same rule -- a report committed before the
            # `body_verification` block existed cannot have drifted in it.
            "body_verification",
        )
        if field not in committed
    )
    # Issue #1952 (extended by #2692's `anchor_top_level_pins`): the same "a
    # field the committed report never carried cannot itself have drifted"
    # rule, one level down -- a report committed after issue #1205 added the
    # `options` echo but before one of these options existed has an `options`
    # block without that key, and the current build's richer echo is not
    # drift.
    committed_options = committed.get("options")
    for option in (
        "power_connectivity",
        "supply_nets",
        "anchor_top_level_pins",
        "strict_top_level_pin_count",  # Issue #2845
        "compare_mos_flavour",  # Issue #3650
        "combine_devices_max_attempts",  # Issue #2682
    ):
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


def _parse_layout_label_layers(
    layout_spec: dict[str, Any],
) -> dict[str, tuple[int, int] | None] | None:
    """``request.layout.label_layers`` (issue #2686) as the ``{role: (layer,
    datatype) | None}`` mapping
    :func:`~klayout_tools.extract.extract_netlist_from_layout`'s own
    ``label_layers`` parameter takes, or ``None`` when the field is absent
    or ``null``.

    The JSON shape is exactly ``klt extract``'s request-document
    ``label_layers`` field (issue #2656): an object mapping a label role
    (``well``/``poly``/``metal<i>``) to a ``[layer, datatype]`` pair of
    non-negative integers, or ``null`` to read no label layer for that role.
    Validated without coercion (a bool, a float, a string ``"8/25"`` or a
    wrong-length array is rejected, never converted), matching
    :func:`klayout_tools.cli._request_document.get_label_layer_map_as_pairs`.
    An empty object is accepted and is a no-op, the same as ``klt extract``
    given an empty ``label_layers`` object.

    Role *names* are not checked here: the role set depends on the deck, and
    :meth:`~klayout_tools.decks.ExtractionDeck.with_label_layers` rejects an
    unknown one during inline extraction (surfaced as :class:`LvsError`,
    naming the roles that deck does have).

    A non-empty override is **extraction-only**: it changes where inline
    extraction reads net/pin name text from, so it requires ``layout.file``
    plus ``layout.deck``. The pre-extracted ``layout.netlist`` shape is
    rejected outright -- its net names were fixed when that netlist was
    written, and silently accepting an override that cannot rename anything
    would read as "the override had no effect". (``layout.deck_options`` is
    different on purpose: it *is* honored for that shape, issue #585.)
    """
    raw = layout_spec.get("label_layers")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LvsError(
            "request.layout.label_layers must be a JSON object mapping a label "
            'role to a [layer, datatype] pair (or null), e.g. {"metal0": [8, 25]}'
        )
    overrides: dict[str, tuple[int, int] | None] = {}
    for role, value in raw.items():
        if value is None:
            overrides[role] = None
            continue
        if not (
            isinstance(value, list)
            and len(value) == 2
            and all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value
            )
        ):
            raise LvsError(
                f"request.layout.label_layers.{role} must be a [layer, datatype] "
                f"pair of non-negative integers, or null (got {value!r})"
            )
        overrides[role] = (value[0], value[1])
    if overrides and "netlist" in layout_spec:
        raise LvsError(
            "request.layout.label_layers applies only to inline extraction "
            "(request.layout.file + request.layout.deck): a pre-extracted "
            "request.layout.netlist already carries its net names and cannot "
            "be re-read from a different label purpose -- re-extract it with "
            "`klt extract --label-layer ...`, or give request.layout.file instead"
        )
    if overrides and not layout_spec.get("deck"):
        raise LvsError("request.layout.label_layers requires request.layout.deck")
    return overrides


class _LayoutAbstraction(NamedTuple):
    """``request.layout.abstract_cells``/``abstract_cell_lef`` (issue #2853),
    parsed by :func:`_parse_layout_abstract_cells`.

    ``patterns``/``lef_paths`` are what
    :func:`~klayout_tools.extract.extract_netlist_from_layout` takes
    (``abstract_cell_patterns``/``abstract_cell_lef_paths``; the LEF paths
    resolved against the request document's directory). ``echo`` is the
    response-side echo of the fields *as given* (paths unresolved, the
    convention ``layout``/``reference`` are echoed under), ``{}`` when
    neither field was given -- see :func:`_layout_abstraction_echo`.
    """

    patterns: tuple[str, ...]
    lef_paths: tuple[str, ...]
    echo: dict[str, list[str]]


def _layout_str_list(layout_spec: dict[str, Any], key: str) -> list[str]:
    """``request.layout.<key>`` as a list of strings, ``[]`` when absent or
    ``null``. A bare string is accepted as a one-element list, exactly as
    ``klt extract``'s request document accepts it for the same field
    (:func:`klayout_tools.cli._request_document.get_str_list`) -- the flags
    these fields mirror are repeatable and one value is the common case."""
    value = layout_spec.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(
        isinstance(entry, str) for entry in value
    ):
        raise LvsError(
            f"request.layout.{key} must be a string or an array of strings "
            f"(got {type(value).__name__}: {value!r})"
        )
    return list(value)


def _parse_layout_abstract_cells(
    layout_spec: dict[str, Any], request_dir: str
) -> _LayoutAbstraction:
    """``request.layout.abstract_cells``/``abstract_cell_lef`` (issue #2853):
    the request-document counterparts of ``klt extract --abstract-cells`` /
    ``--abstract-cell-lef`` (issue #620), forwarded to inline extraction so a
    mixed-level compare -- a transistor-level region next to placed cells
    extracted as pin-only black boxes -- runs as **one** ``klt lvs`` request
    and keeps the deck-aware checks (``body_verification``) that need the
    inline-extraction shape.

    Same shape and validation as ``klt extract``'s request fields of the same
    names (``cli/extract_cmd.py``): each is a string or an array of strings
    (``fnmatch`` cell-name globs / LEF file paths, the latter resolved
    against the request document's directory). An empty array is a no-op,
    as it is for ``klt extract``. ``abstract_cell_lef`` without a non-empty
    ``abstract_cells`` is rejected, mirroring ``run_extract``'s own check (the
    LEF is only ever a pin-resolution *fallback* for a matched cell type).

    **Extraction-only**, like ``layout.label_layers``: a non-empty value on
    the pre-extracted ``layout.netlist`` shape is a clean :class:`LvsError`
    -- that netlist's cells were already abstracted (or not) when it was
    extracted, and silently accepting the field would read as "the
    abstraction was applied".
    """
    patterns = _layout_str_list(layout_spec, "abstract_cells")
    lef_given = _layout_str_list(layout_spec, "abstract_cell_lef")
    if (patterns or lef_given) and "netlist" in layout_spec:
        given = "abstract_cells" if patterns else "abstract_cell_lef"
        raise LvsError(
            f"request.layout.{given} applies only to inline extraction "
            "(request.layout.file + request.layout.deck): a pre-extracted "
            "request.layout.netlist was already abstracted (or not) when it "
            "was extracted -- re-extract it with `klt extract "
            "--abstract-cells ...`, or give request.layout.file instead"
        )
    if lef_given and not patterns:
        raise LvsError(
            "request.layout.abstract_cell_lef was given but "
            "request.layout.abstract_cells is empty -- abstract_cell_lef only "
            "has an effect as a pin-resolution fallback for a cell type "
            "abstract_cells actually matches"
        )
    echo: dict[str, list[str]] = {}
    if patterns:
        echo["abstract_cells"] = list(patterns)
    if lef_given:
        echo["abstract_cell_lef"] = list(lef_given)
    return _LayoutAbstraction(
        patterns=tuple(patterns),
        lef_paths=tuple(_resolve_relative(path, request_dir) for path in lef_given),
        echo=echo,
    )


def _layout_abstraction_echo(
    abstraction: _LayoutAbstraction, abstracted_cells: list[dict[str, Any]]
) -> dict[str, Any]:
    """The response's ``abstract_cells``/``abstract_cell_lef``/
    ``abstracted_cells`` fields (issue #2853), or ``{}`` when the request
    abstracted nothing.

    ``abstract_cells``/``abstract_cell_lef`` echo the request fields as given
    (and are replayed by ``--check --rerun``); ``abstracted_cells`` is the
    per-matched-cell-type report ``klt extract`` emits under the same name
    (``{"cell", "instance_count", "pin_count", "resolution_source",
    "lef_path"}``) -- an empty list when the patterns matched no instantiated
    cell, so "the glob matched nothing" is visible rather than silently
    indistinguishable from a full abstraction. All three are omitted
    entirely (not ``null``) when ``layout.abstract_cells`` was not given, so
    every report from a request without it -- and every report committed
    before it existed -- stays byte-identical.
    """
    if not abstraction.patterns:
        return {}
    return {**abstraction.echo, "abstracted_cells": list(abstracted_cells)}


def _layout_abstraction_replay(committed: dict[str, Any]) -> dict[str, Any]:
    """The ``request.layout`` keys that rebuild a committed report's
    ``abstract_cells``/``abstract_cell_lef`` echo (issue #2853), or ``{}`` for
    a report without one. Passed through verbatim, so a malformed recorded
    value is rejected by :func:`_parse_layout_abstract_cells` on replay rather
    than silently becoming a different, valid request."""
    replay: dict[str, Any] = {}
    for key in ("abstract_cells", "abstract_cell_lef"):
        if committed.get(key) is not None:
            replay[key] = committed[key]
    return replay


def _label_layers_echo(
    label_layers: Mapping[str, tuple[int, int] | None] | None,
) -> dict[str, Any]:
    """``{"label_layers": {role: [layer, datatype] | null}}`` when this run
    applied a non-empty ``layout.label_layers`` override (issue #2686), else
    ``{}``.

    Omitted entirely (not ``null``) when no override was applied, so every
    report from a request without the field -- and every report committed
    before it existed -- stays byte-identical, and ``--check --rerun`` of
    such a report cannot see the field as drift. The value shape is ``klt
    extract``'s own ``label_layers`` echo (sorted by role, a cleared role kept
    as ``null``); :func:`_label_layers_replay` reads it back.
    """
    if not label_layers:
        return {}
    return {
        "label_layers": {
            role: (None if layer is None else [layer[0], layer[1]])
            for role, layer in sorted(label_layers.items())
        }
    }


def _label_layers_replay(committed: dict[str, Any]) -> dict[str, Any]:
    """The ``request.layout`` key that rebuilds a committed report's
    ``label_layers`` echo (issue #2686), or ``{}`` for a report without one
    (every run that applied no override, and every report committed before
    the field existed -- those replay exactly as they used to).

    Passed through verbatim, ``null`` roles included: a malformed recorded
    value stays malformed, so :func:`_parse_layout_label_layers` rejects it
    on replay rather than it silently becoming a different, valid override.
    """
    echoed = committed.get("label_layers")
    if not isinstance(echoed, dict) or not echoed:
        return {}
    return {"label_layers": dict(echoed)}


def _reject_subckt_call_layout_netlist(path: str, text: str) -> None:
    """Issue #2972: the layout-side twin of `_read_reference_netlist`'s
    simulation-form guard. `klt extract --pdk` writes PDK-bound MOS `X` cards;
    read back unguarded they are undefined-subcircuit instances (zero layout
    devices, a topology cascade) rather than a form-mismatch error."""
    from .netlist_normalize import detect_subckt_call_devices

    offending = detect_subckt_call_devices(text)
    if offending:
        raise LvsError(
            f"layout netlist '{path}' is in the simulation "
            f"(subcircuit-call) form: it instantiates curated PDK device "
            f"subcircuit(s) {', '.join(offending)} via undefined 'X' cards, "
            "which 'klt lvs' cannot compare as extracted plain-element "
            "devices (it would silently read as zero layout devices and "
            "report a topology mismatch). Re-extract the layout without "
            "'--pdk' (plain-element M-cards) -- see docs/cli/lvs.md, "
            '"Netlist form".'
        )


def _logical_spice_cards(netlist_text: str) -> list[str]:
    """Split ``netlist_text`` into lines, joining ``+`` continuations so a
    wrapped card is one logical card."""
    logical: list[str] = []
    for raw in netlist_text.splitlines():
        if raw.lstrip().startswith("+") and logical:
            logical[-1] += " " + raw.lstrip()[1:]
        else:
            logical.append(raw)
    return logical


def _bare_resistor_x_card_name(
    tokens: list[str], classes: Mapping[str, str]
) -> str | None:
    """``"<name> (<class>)"`` when ``tokens`` is a ``klt extract`` resistor
    ``X`` card (``X<name> <net> <net> <net> <model> r=...``) naming one of
    ``classes``; otherwise ``None``."""
    if not tokens or tokens[0][0] not in "xX":
        return None
    # The model is the last token before the first `k=v` parameter.
    first_param = next((i for i, tok in enumerate(tokens) if "=" in tok), len(tokens))
    if first_param != 5:
        return None
    model = tokens[first_param - 1].upper()
    if model not in classes:
        return None
    if not any(tok.lower().startswith("r=") for tok in tokens[first_param:]):
        return None
    return f"{tokens[0]} ({classes[model]})"


def _reject_bare_resistor_x_cards(
    netlist_text: str,
    netlist_path: str,
    reference_deck_name: str | None,
    layout_deck_name: str | None,
) -> None:
    """Issue #3078: refuse a bare pre-extracted ``layout.netlist`` (no
    ``layout.deck``) carrying a ``klt extract`` resistor ``X`` card -- one
    naming a resistor class of ``reference.deck`` with the writer's 3-net
    shape and an ``r=`` parameter (the test ``netlist_capacitor_recovery``
    applies). Without ``layout.deck`` the card is read as an undefined
    subcircuit, the layout side has no resistor devices, and the compare
    reports a spurious ``mismatch``. Best-effort: an unresolvable
    ``reference.deck`` is left to the reference-side validation."""
    if layout_deck_name or not reference_deck_name:
        return
    try:
        classes = resistor_classes_for_deck(get_extraction_deck(reference_deck_name))
    except (UnknownExtractionDeckError, InvalidDeckOptionError):
        return
    if not classes:
        return
    offending = [
        name
        for line in _logical_spice_cards(netlist_text)
        if (name := _bare_resistor_x_card_name(line.split(), classes))
    ]
    if offending:
        raise LvsError(
            f"layout netlist '{netlist_path}' contains resistor X card(s) "
            f"{', '.join(offending)} that need `layout.deck` to be read as "
            "resistor devices; without it they are read as undefined "
            "subcircuits and the compare reports a spurious mismatch. Add "
            f'`layout.deck` (e.g. "deck": "{reference_deck_name}").'
        )


def _resolve_layout(
    layout_spec: dict[str, Any],
    request_dir: str,
    keep_extracted: bool,
    combine_devices: bool = False,
    deck_options: Mapping[str, str] | None = None,
    label_layers: Mapping[str, tuple[int, int] | None] | None = None,
    reference_deck_name: str | None = None,
    abstraction: _LayoutAbstraction | None = None,
) -> tuple[
    kdb.Netlist,
    str,
    str,
    str | None,
    dict[int, list[dict[str, Any]]],
    dict[str, Any] | None,
    list[dict[str, Any]],
]:
    """Resolve ``request.layout`` to ``(netlist, echo, hash_source_path,
    extracted_netlist_path_or_none, net_label_positions, pin_renames,
    abstracted_cells)``.
    Original drawn labels are keyed by layout cluster for the
    supply-fragmentation check; pre-extracted SPICE has no such label map
    and returns an empty mapping. ``pin_renames`` (issue #2854) is `klt
    extract`'s own ``pin_renames`` block -- ``None`` unless
    ``layout.rename_declared_pins`` opted in (and always ``None`` for the
    pre-extracted ``layout.netlist`` shape, where no extraction ran); its
    ``error``-severity findings become ``pin.declared_short``
    ``mismatches[]`` entries (see :func:`_declared_pin_short_mismatches`),
    the rename/collision disclosures stay extract-report-only.
    ``abstracted_cells`` (issue #2853) is inline extraction's
    ``--abstract-cells`` report (always ``[]`` for the ``layout.netlist``
    shape, and whenever ``abstraction`` carries no patterns).

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

    ``label_layers`` (issue #2686, ``request.layout.label_layers``): forwarded
    to :func:`~klayout_tools.extract.extract_netlist_from_layout` for the
    ``layout.file`` shape -- replaces, per label role, the GDS purpose inline
    extraction reads net/pin name text from, exactly like ``klt extract
    --label-layer``. Already validated by :func:`_parse_layout_label_layers`
    (which also rejects a non-empty override on the ``layout.netlist``
    shape, so it never reaches that branch); an unknown role raises
    :class:`~klayout_tools.extract.ExtractError`, re-raised as
    :class:`LvsError` below.

    ``abstraction`` (issue #2853, ``request.layout.abstract_cells``/
    ``abstract_cell_lef``): forwarded to
    :func:`~klayout_tools.extract.extract_netlist_from_layout` for the
    ``layout.file`` shape -- every instantiated cell matching one of the
    patterns is extracted as a pin-only black box, exactly like ``klt extract
    --abstract-cells``. Already validated by
    :func:`_parse_layout_abstract_cells` (which rejects it on the
    ``layout.netlist`` shape, so it never reaches that branch). No PDK is
    resolved here (``klt lvs`` has no ``layout.pdk``), so an
    ``abstract_cell_lef`` port is probed the way ``klt extract
    --abstract-cell-lef`` probes it without ``--pdk``.

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

        # Issue #2854: `request.layout.rename_declared_pins`
        # renames each surviving promoted pin net to its declared name, so
        # `options.anchor_top_level_pins` (#2692) can pair it with a
        # same-named reference pin even when the boundary conductor carries
        # extra drawn labels. Validated by its own helper (bool shape,
        # requires `declared_pins`) to keep this function's
        # complexity-baselined branch count unchanged.
        rename_declared_pins = _parse_layout_rename_declared_pins(layout_spec)
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
            # --abstract-cells`'s own report; `klt lvs` forwards
            # `request.layout.abstract_cells` (issue #2853) and echoes this
            # report as the response's `abstracted_cells` (an empty list
            # when no abstraction was requested). The 11th return
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
            # instance, so it has no use here either. The 15th return
            # (def_pin_promotion, #2473) reports how a `def_pins` declared
            # port set landed; `klt lvs` has no `--def-pins` equivalent
            # request field, so it is always `None` here. The 17th return
            # (pin_renames, #2854) carries `layout.rename_declared_pins`'s
            # own report; its error-severity findings become
            # `pin.declared_short` mismatches[] entries in `run_lvs`.
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
                _def_pin_promotion,
                _substrate_spreading,
                _pin_renames,
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
                # Issue #2854: `request.layout.rename_declared_pins` --
                # validated by `_parse_layout_rename_declared_pins` above;
                # `False` when the field was never given, unchanged from
                # every request that predates it.
                rename_declared_pins=rename_declared_pins,
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
                # Issue #2686: `request.layout.label_layers` -- mirrors `klt
                # extract --label-layer`. `None` when the field was never
                # given, unchanged from every request that predates it.
                label_layers=label_layers,
                # Issue #2853: `request.layout.abstract_cells`/
                # `abstract_cell_lef` -- mirrors `klt extract
                # --abstract-cells`/`--abstract-cell-lef`. `()` when the
                # fields were never given, unchanged from every request that
                # predates them.
                abstract_cell_patterns=(
                    abstraction.patterns if abstraction is not None else ()
                ),
                abstract_cell_lef_paths=(
                    abstraction.lef_paths if abstraction is not None else ()
                ),
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
            _pin_renames,
            _abstracted_cells,
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
    _reject_subckt_call_layout_netlist(layout_netlist_path, layout_netlist_text)
    recovered_capacitor_classes = parse_capacitor_class_comments(layout_netlist_text)
    _reject_bare_resistor_x_cards(
        layout_netlist_text,
        layout_netlist_path,
        reference_deck_name,
        layout_spec.get("deck"),
    )
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

    # Issue #2854: no inline extraction ran for the pre-extracted
    # `layout.netlist` shape, so there is no `pin_renames` block to report
    # and (issue #2853) nothing was abstracted.
    return netlist, layout_spec["netlist"], layout_netlist_path, None, {}, None, []


def _read_reference_netlist(
    path: str,
    *,
    form: str = "plain-element",
    deck: str | None = None,
    device_map: dict[str, object] | None = None,
    library: str | list[str] | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    pin_order_files: Sequence[str] | None = None,
    pin_orders: dict[str, list[str]] | None = None,
    placeholder_value_classes: dict[str, str] | None = None,
    gate_level_port_aliases: dict[str, dict[str, str]] | None = None,
    finfet: bool = False,
    finfet_top: str | None = None,
    resistor_value_classes: Sequence[str] | None = None,
    deck_options: Mapping[str, str] | None = None,
    mos_flavour_classes: bool = False,
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

    ``resistor_value_classes`` (issue #2905), ``form="subckt-call"`` only,
    names resistor classes whose ``R`` the converter fills from the deck's
    sheet resistance (the ``klt extract`` ``r_ohm`` code path) and the call's
    ``L``/``W`` rather than the ``0`` placeholder; ``deck_options`` selects
    the deck's flavours as for extraction. See
    :func:`_resistor_value_classes`.

    ``mos_flavour_classes`` (issue #3650), ``form="subckt-call"`` only, puts a
    flavoured MOS model's devices into a flavour-specific device class (see
    :func:`~klayout_tools.netlist_normalize.convert_reference_netlist`).

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
    form, unless ``pin_order_files`` is given instead) names the
    standard-cell library -- or, as a list, every library the design
    instantiates cells from (issue #2693) -- whose real
    ``libs.ref/<library>/spice/<library>.spice`` (or ``.../cdl/<library>.cdl``,
    or the per-macro files a macro library ships instead of an aggregate)
    resolves each instantiated cell's pin order; ``pin_order_files`` adds
    explicit, already-resolved ``.spice``/``.cdl`` paths to that lookup; and
    ``pdk_variant``/``pdk_root`` are forwarded to
    :func:`klayout_tools.pdk.find_pdk` exactly like `klt extract`'s own
    ``--pdk``/``--pdk-root`` flags.

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
                text,
                deck=deck,
                device_map=device_map,
                resistor_value_classes=resistor_value_classes,
                deck_options=deck_options,
                mos_flavour_classes=mos_flavour_classes,
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
            pin_orders = _resolve_gate_level_pin_orders(
                library, pdk_variant, pdk_root, pin_order_files=pin_order_files
            ).pin_orders
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
    # Issue #2813: a FinFET layout deck reads `M` cards through the
    # nfin-preserving reader (the planar reader would drop `nfin`).
    read_path, read_text, tmp_path = sliced_reference_file(
        finfet, finfet_top, read_path, read_text, tmp_path
    )
    reader = reference_reader(
        finfet,
        lambda: make_capacitor_class_recovery_reader(
            recovered_capacitor_classes,
            custom_device_classes=custom_device_classes,
            resistor_classes=resistor_classes,
        ),
    )
    try:
        netlist.read(read_path, reader)
    except Exception as exc:
        raise _ReferenceNetlistParseError(
            f"could not parse reference netlist '{path}': {exc}"
        ) from exc
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


class _AttributedPinOrders(dict):
    """The merged ``{<cell>: [<pin>, ...]}`` pin-order mapping, carrying the
    library each cell's declaration came from as ``cell_libraries``
    (upper-cased cell name -> library name, or ``None`` for a cell from an
    explicit ``reference.pin_order_files`` entry; issue #2707).

    A ``dict`` subclass so every existing consumer of the plain mapping is
    unaffected, while :func:`_gate_level_power_pin_evidence_detail` can derive
    the power-pin universe per library without every intermediate function
    threading a second argument.
    """

    cell_libraries: dict[str, str | None]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cell_libraries = {}


class _GateLevelPinOrders(NamedTuple):
    """What :func:`_resolve_gate_level_pin_orders` resolved (issue #2693).

    ``pin_orders`` is the merged ``{<cell>: [<pin>, ...]}`` mapping across
    *every* source the request named; ``pdk_info`` is the
    :func:`~klayout_tools.pdk.find_pdk`-shaped dict the
    ``reference.library`` resolution went through (``None`` only when the
    request named ``reference.pin_order_files`` alone, which resolves no PDK
    at all); ``sources`` is one ``{"library", "path", "cells"}`` record per
    file actually read, in read order -- what ``run_lvs`` records as
    ``provenance.pin_order_sources``.

    A tuple-shaped return existed before this issue (``(pin_orders,
    pdk_info)``); naming the fields is what keeps a third element from
    turning every call site into positional guesswork.
    """

    pin_orders: dict[str, list[str]]
    pdk_info: dict[str, Any] | None
    sources: list[dict[str, Any]]


def _normalize_reference_libraries(value: Any) -> list[str]:
    """``request.reference.library`` -> an ordered, de-duplicated list of
    library names (issue #2693).

    A bare string (the original shape, unchanged) names one library. A list
    names several, resolved left to right: a macro-backed digital design
    instantiates cells from **at least two** libraries -- the standard-cell
    library (e.g. ``sky130_fd_sc_hd``) plus each hard macro's own (e.g.
    ``sky130_sram_macros``) -- and before this issue only one could be named,
    so such a design failed with "library cell '...' has no resolvable pin
    order" no matter which single library was chosen.

    ``None``/absent yields ``[]``, which is legal only when
    ``reference.pin_order_files`` supplies the pin orders instead (the caller
    raises the "library is required" error). A wrong-shaped value is a clean
    request error rather than a silent coercion, matching this module's other
    request-side list-or-scalar options (``options.combine_devices``).
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise LvsError(
            "request.reference.library must be a library-name string or a "
            'list of library-name strings (e.g. ["sky130_fd_sc_hd", '
            f'"sky130_sram_macros"]); got {type(value).__name__}'
        )
    libraries: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise LvsError(
                "request.reference.library list entries must be non-empty "
                f"library-name strings; got {name!r}"
            )
        if name.strip() not in libraries:
            libraries.append(name.strip())
    return libraries


def _resolve_reference_pin_order_files(
    reference_spec: Mapping[str, Any], request_dir: str
) -> list[str]:
    """``request.reference.pin_order_files`` -> resolved, existing, de-
    duplicated ``.spice``/``.cdl`` paths (issue #2693).

    The escape hatch for a pin-order source that is not reachable by the
    ``libs.ref/<library>/...`` convention at all -- a macro's netlist sitting
    beside the design, a hand-written stub for a cell no installed library
    ships. Paths resolve against the request file's directory exactly like
    ``layout.netlist``/``reference.netlist`` (:func:`_resolve_relative`), and
    a missing file is a clean request error rather than a pin order that
    silently fails to resolve later.

    Omitted (the default) yields ``[]``. An empty list is rejected for the
    same reason ``layout.declared_pins`` rejects one: it reads as a
    deliberate "no files", which is what omitting the key already means, so
    it is far more likely a mistake.
    """
    value = reference_spec.get("pin_order_files")
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise LvsError(
            "request.reference.pin_order_files must be a non-empty list of "
            ".spice/.cdl path strings -- omit the key entirely to resolve "
            "pin orders from request.reference.library alone"
        )
    paths: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise LvsError(
                "request.reference.pin_order_files entries must be non-empty "
                f"path strings; got {entry!r}"
            )
        resolved = _resolve_relative(entry.strip(), request_dir)
        if not os.path.isfile(resolved):
            raise LvsError(f"reference pin_order_files entry not found: {resolved}")
        if resolved not in paths:
            paths.append(resolved)
    return paths


def _variants_shipping_library(pdk_root: str, library: str, variant: str) -> list[str]:
    """Names of the *other* variants under ``pdk_root`` whose ``libs.ref``
    ships ``<library>`` (issue #3748) -- the hint that turns "this variant has
    no such library" into "pick another variant". Best effort: a variant that
    does not resolve is simply not listed."""
    shipping: list[str] = []
    for install in list_pdks(root=pdk_root)["installs"]:
        for entry in install["variants"]:
            name = entry["name"]
            if name == variant or name in shipping:
                continue
            try:
                libs_ref = find_pdk(variant=name, root=pdk_root)["assets"]["libs_ref"]
            except PdkNotFoundError:
                continue
            if libs_ref is not None and os.path.isdir(os.path.join(libs_ref, library)):
                shipping.append(name)
    return shipping


def _library_pin_order_files(
    libs_ref: str, library: str, variant: str, pdk_root: str | None = None
) -> list[str]:
    """Every pin-order file ``<library>`` ships, in read order (issue #2693).

    Two shapes, because real installs ship two. A standard-cell library ships
    one aggregate file covering every cell
    (``libs.ref/<library>/{spice,cdl}/<library>.{spice,cdl}``, issue #1336's
    original and still-first rule). A **macro** library ships one file *per
    macro* and no aggregate at all -- ``sky130_sram_macros`` has
    ``spice/sky130_sram_1kbyte_1rw1r_32x256_8.spice`` and friends,
    ``gf180mcu_fd_ip_sram`` has ``cdl/gf180mcu_fd_ip_sram__sram64x8m8wm1.cdl``
    -- so when no aggregate exists, every ``.spice`` (then every ``.cdl``)
    the library's asset directory contains is read instead.

    That per-library enumeration is deliberately **eager** rather than a lazy
    per-cell ``<cell>.{spice,cdl}`` probe: the returned mapping has two
    consumers (see :func:`_resolve_gate_level_pin_orders`) and one of them,
    :func:`_supply_pin_universe`, needs the *whole* mapping rather than the
    cells a conversion happened to look up, so a lazily-populated lookup
    would make the derived power-pin universe depend on lookup order. A
    macro library ships a handful of files, so reading them all costs
    nothing; an aggregate-shipping library never reaches this branch.

    Raises :class:`LvsError` naming every candidate it tried when the library
    ships neither shape. The message also names ``reference.pdk`` / ``$PDK`` as
    the way to pick the variant and, when ``pdk_root`` is given, any other
    installed variant under it that ships ``<library>`` (issue #3748).
    """
    lib_dir = os.path.join(libs_ref, library)
    tried: list[str] = []
    for subdir, extension in _LIBRARY_PIN_ORDER_ASSETS:
        candidate = os.path.join(lib_dir, subdir, f"{library}.{extension}")
        tried.append(candidate)
        if os.path.isfile(candidate):
            return [candidate]
    for subdir, extension in _LIBRARY_PIN_ORDER_ASSETS:
        asset_dir = os.path.join(lib_dir, subdir)
        suffix = f".{extension}"
        tried.append(os.path.join(asset_dir, f"*{suffix}"))
        try:
            names = sorted(os.listdir(asset_dir))
        except OSError:
            continue
        per_cell = [
            os.path.join(asset_dir, name) for name in names if name.endswith(suffix)
        ]
        if per_cell:
            return per_cell
    message = (
        f"library '{library}' has no pin-order source under PDK variant "
        f"'{variant}' -- tried: {', '.join(tried)}. Select the variant with "
        "request.reference.pdk (or $PDK)"
    )
    if pdk_root is not None:
        others = _variants_shipping_library(pdk_root, library, variant)
        if others:
            message += f"; installed variant(s) that ship it: {', '.join(others)}"
    raise LvsError(message)


def _merge_pin_order_sources(
    plan: Sequence[tuple[str | None, str]],
) -> tuple[_AttributedPinOrders, list[dict[str, Any]]]:
    """Read every ``(library_or_None, path)`` pin-order source in ``plan``
    and merge their ``.subckt`` declarations into one lookup (issue #2693),
    returning ``(pin_orders, source_records)``.

    **A cell type declared by two sources with different pin lists is a loud
    error, never "first wins".** Two libraries disagreeing about one cell's
    pin order means at least one side of the resulting compare would be wired
    wrong -- and silently, since the conversion emits a positional
    ``X``-card call that parses perfectly either way. An *identical*
    redeclaration is accepted (a cell genuinely shipped by two views, e.g. a
    library that ships both ``.spice`` and ``.cdl`` with byte-identical
    headers) because there is nothing to choose between.
    """
    merged = _AttributedPinOrders()
    origin: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for library, path in plan:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            raise LvsError(
                f"could not read library pin-order source '{path}': {exc}"
            ) from exc
        parsed = parse_subckt_pin_orders(text)
        for cell, pins in parsed.items():
            previous = merged.get(cell)
            if previous is not None and previous != pins:
                raise LvsError(
                    f"library cell '{cell}' is declared with two different "
                    f"pin orders by two pin-order sources: '{origin[cell]}' "
                    f"declares ({' '.join(previous)}) and '{path}' declares "
                    f"({' '.join(pins)}) -- resolve the conflict (drop one "
                    "source from request.reference.library / "
                    "request.reference.pin_order_files, or point "
                    "reference.pdk_root at the install whose declaration is "
                    "authoritative) rather than letting one silently win"
                )
            merged[cell] = pins
            origin[cell] = path
            merged.cell_libraries[cell.upper()] = library
        records.append({"library": library, "path": path, "cells": len(parsed)})
    return merged, records


def _resolve_gate_level_pin_orders(
    library: Any,
    pdk_variant: str | None,
    pdk_root: str | None,
    *,
    pin_order_files: Sequence[str] | None = None,
) -> _GateLevelPinOrders:
    """Every instantiated cell's real, full PDK pin order (signal *and*
    power/ground), read from the pin-order sources the request named (issue
    #1336, extended to more than one source by issue #2693) -- never a
    hardcoded pin-order table.

    Two consumers, one read of each source (issue #1622): ``.get`` on the
    merged pin-order mapping is the ``pin_order_lookup`` callback
    :func:`klayout_tools.verilog_netlist.convert_gate_level_verilog` wants,
    while :func:`_gate_level_power_pin_names` needs the whole mapping, to
    derive which of a cell's real PDK pins the conversion dropped.

    ``library`` is one library name or a list of them (see
    :func:`_normalize_reference_libraries`); each is resolved against a PDK
    resolved exactly like `klt extract --pdk`/`klt place-and-route` do
    (:func:`klayout_tools.pdk.find_pdk`, the same ``variant``/``root``
    resolution order documented in ``docs/cli/pdk.md``), then enumerated by
    :func:`_library_pin_order_files`. ``pin_order_files`` adds already-
    resolved explicit paths on top (see
    :func:`_resolve_reference_pin_order_files`), and is the only way to
    resolve pin orders with no PDK involved at all -- a request naming it
    alone returns ``pdk_info: None``.

    The resolved ``pdk_info`` is returned alongside the mapping (issue #1901)
    so ``run_lvs`` can record it in ``provenance.pdk`` without a second,
    redundant resolution, and every source read is returned with it so
    ``provenance.pin_order_sources`` can name all of them (issue #2693).
    Raises :class:`LvsError` -- never lets a lower-level exception escape
    this command's JSON-envelope contract -- when neither a library nor an
    explicit file was named, the PDK does not resolve, the resolved variant
    ships no ``libs_ref`` asset at all, a named library ships no pin-order
    source, or two sources disagree about one cell's pin order.
    """
    libraries = _normalize_reference_libraries(library)
    explicit_files = list(pin_order_files or ())
    if not libraries and not explicit_files:
        raise LvsError(
            "request.reference.library is required with form: "
            '"gate-level-verilog" -- it names the standard-cell library '
            "(and, for a macro-backed design, each macro library) whose own "
            ".spice/.cdl subcircuit declarations resolve each instantiated "
            "cell's pin order; name request.reference.pin_order_files "
            "instead to point at those files directly"
        )

    pdk_info: dict[str, Any] | None = None
    plan: list[tuple[str | None, str]] = []
    if libraries:
        try:
            pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise LvsError(str(exc)) from exc
        libs_ref = pdk_info["assets"]["libs_ref"]
        if libs_ref is None:
            raise LvsError(
                f"PDK variant '{pdk_info['variant']}' (root "
                f"'{pdk_info['root']}') ships no 'libs.ref' asset -- cannot "
                f"resolve {', '.join(repr(name) for name in libraries)}'s "
                "pin order"
            )
        for name in libraries:
            plan.extend(
                (name, path)
                for path in _library_pin_order_files(
                    libs_ref, name, pdk_info["variant"], pdk_info["root"]
                )
            )
    plan.extend((None, path) for path in explicit_files)

    pin_orders, sources = _merge_pin_order_sources(plan)
    return _GateLevelPinOrders(pin_orders, pdk_info, sources)


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
    netlist. ``keep`` may also be an interior circuit (instantiated by a
    wrapper); wrappers and unrelated circuits are dropped, while ``keep`` and
    its descendants survive (issue #2852).

    Compares circuits by identity (``is not``) rather than ``cell_index``:
    a netlist read directly from SPICE/Verilog text (``kdb.NetlistSpiceReader``
    or this module's gate-level-verilog/subckt-call conversions) has no
    backing ``kdb.Layout``, so every circuit's ``cell_index`` reads back as 0
    -- making a ``cell_index`` comparison always false and silently pruning
    nothing (issue #1657).
    """
    # Issue #2852: ``keep`` may itself be instantiated by another circuit
    # (a leaf plus a wrapper in one file). ``purge_circuit`` on that wrapper
    # recursively deletes its children -- including ``keep`` -- leaving a
    # destroyed handle. Instead, keep ``keep`` plus everything reachable from
    # it, and non-recursively ``remove`` every other circuit.
    reachable: set[str] = set()
    stack = [keep]
    while stack:
        current = stack.pop()
        for subcircuit in current.each_subcircuit():
            child = subcircuit.circuit_ref()
            if child is not None and child.name not in reachable:
                reachable.add(child.name)
                stack.append(child)
    reachable.add(keep.name)
    for circuit in list(netlist.each_circuit()):
        if circuit is keep or circuit.name in reachable:
            continue
        netlist.remove(circuit)


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
# Constant-net binding: hints.constant_nets (issue #3014)
# --------------------------------------------------------------------------- #

#: The reference-side constant nets a ``gate-level-verilog`` conversion can
#: carry (`verilog_netlist._CONST_NET_NAMES`) -- the only legal
#: ``hints.constant_nets`` keys.
_CONSTANT_NET_NAMES = ("__CONST0__", "__CONST1__")


class _ConstantNetBinding(NamedTuple):
    """One ``hints.constant_nets`` entry resolved to nets (issue #3014).

    ``layout_net``/``reference_net`` are ``Net.expanded_name()`` values, the
    spelling :func:`_apply_hints` records its declared pairs under;
    ``constant`` is the reference constant's name as the caller wrote it.
    """

    constant: str
    layout_net: str
    reference_net: str


def _constant_binding_pairs(
    bindings: Sequence[_ConstantNetBinding],
) -> list[tuple[str, str]]:
    return [(b.layout_net, b.reference_net) for b in bindings]


def _constant_nets_echo(
    bindings: Sequence[_ConstantNetBinding],
) -> dict[str, str] | None:
    """``{constant: layout net}`` for the applied bindings, else ``None``."""
    return {b.constant: b.layout_net for b in bindings} or None


def _parse_constant_nets(hints: Mapping[str, Any]) -> dict[str, str]:
    spec = hints.get("constant_nets")
    if spec is None:
        return {}
    if not isinstance(spec, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in spec.items()
    ):
        raise LvsError(
            "hints.constant_nets must be a JSON object mapping a reference "
            "constant net name ('__CONST0__'/'__CONST1__') to a layout net name"
        )
    return dict(spec)


def _apply_constant_nets(
    comparer: Any,
    hints: Mapping[str, Any],
    layout_circuit: Any,
    reference_circuit: Any,
    *,
    declared: Sequence[tuple[str, str]],
) -> tuple[list[_ConstantNetBinding], list[dict[str, Any]]]:
    """Bind each ``hints.constant_nets`` reference constant to the layout net
    the caller says carries it (issue #3014), returning ``(bindings,
    disclosure_warnings)``.

    **Why it exists.** Which layout node is logic-0 and which is logic-1 is
    net *meaning*, not topology: two tie bits of a black-box macro are
    topologically interchangeable, so a swap compares clean (#2692/#2704).
    Only the caller knows the meaning. This is sugar over the mechanism
    ``hints.same_nets`` exposes -- ``NetlistComparer.same_nets(...,
    must_match=True)`` -- plus validation and attribution; nothing is inferred.

    Every request error is raised as :class:`LvsError` here, before
    ``compare()`` runs: a non-object value; a key that is not a constant the
    reference actually carries; a layout net that does not exist; two
    constants bound to one layout net; or a constant (or its layout net) also
    named by a ``hints.same_nets`` pair in ``declared`` (ambiguous authority).

    Comparer state, not netlist state: call it on every fresh comparer.
    Returns ``([], [])`` when the hint is omitted.
    """
    spec = _parse_constant_nets(hints)
    if not spec:
        return [], []
    spoken_for = {name for pair in declared for name in pair}
    bound_layout: dict[str, str] = {}
    bindings: list[_ConstantNetBinding] = []
    for constant, layout_name in spec.items():
        reference_net = (
            reference_circuit.net_by_name(constant)
            if constant in _CONSTANT_NET_NAMES
            else None
        )
        if reference_net is None:
            raise LvsError(
                f"hints.constant_nets: '{constant}' is not a constant net the "
                "reference carries (expected '__CONST0__'/'__CONST1__' present "
                "on the reference top)"
            )
        layout_net = layout_circuit.net_by_name(layout_name)
        if layout_net is None:
            raise LvsError(f"hints.constant_nets: layout net '{layout_name}' not found")
        if layout_name in bound_layout:
            raise LvsError(
                f"hints.constant_nets: '{bound_layout[layout_name]}' and "
                f"'{constant}' are both bound to layout net '{layout_name}'"
            )
        bound_layout[layout_name] = constant
        layout_expanded = layout_net.expanded_name()
        reference_expanded = reference_net.expanded_name()
        if layout_expanded in spoken_for or reference_expanded in spoken_for:
            raise LvsError(
                f"hints.constant_nets: '{constant}' or layout net "
                f"'{layout_name}' is also named by a hints.same_nets pair "
                "(ambiguous authority) -- declare it in one place"
            )
        bindings.append(
            _ConstantNetBinding(constant, layout_expanded, reference_expanded)
        )
    for binding in bindings:
        comparer.same_nets(
            layout_circuit,
            reference_circuit,
            layout_circuit.net_by_name(binding.layout_net),
            reference_circuit.net_by_name(binding.reference_net),
            True,
        )
    return bindings, [_constant_nets_disclosure(bindings)]


def _constant_nets_disclosure(
    bindings: Sequence[_ConstantNetBinding],
) -> dict[str, Any]:
    return _mismatch(
        CATEGORY_HINTS_CONSTANT_NETS_DECLARED,
        "warning",
        f"{len(bindings)} reference constant net(s) were bound to the layout "
        "node(s) the caller declared (hints.constant_nets): each layout net "
        "is asserted to be the node that carries that logic constant, which "
        "the comparer's topology-only pairing does not otherwise require. "
        'Nothing was inferred -- see docs/cli/lvs.md, "hints.constant_nets"',
        "both",
        details={
            "constant_nets": {b.constant: b.layout_net for b in bindings},
            "binding_count": len(bindings),
        },
    )


# --------------------------------------------------------------------------- #
# Top-level pin anchoring: options.anchor_top_level_pins (issue #2692)
# --------------------------------------------------------------------------- #


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


def _anchor_top_level_pins_enabled(requested: bool | None, reference_form: str) -> bool:
    """Whether top-level pin anchoring runs (issue #2692).

    ``requested`` is ``options.anchor_top_level_pins`` as parsed by
    :func:`_parse_anchor_top_level_pins` (``None`` when the caller omitted
    it). Omitted means **on for** ``reference.form:
    "gate-level-verilog"`` and **off for every other form** -- the one form
    whose reference netlist is generated from the very DEF/Verilog the
    layout's own pin names come from, so a pin name shared by both sides
    names the same boundary node by construction and anchoring it is sound
    without the caller having to say so. Every other form's reference is
    arbitrary SPICE whose pin names are only conventionally related to the
    layout's (a hand-authored schematic may legitimately spell a boundary
    node differently on each side), so there anchoring stays opt-in.
    """
    if requested is not None:
        return requested
    return reference_form == "gate-level-verilog"


def _pin_nets_by_name(circuit: Any) -> dict[str, Any]:
    """``{<upper-cased pin name>: <net>}`` for every named top-level pin of
    ``circuit`` that resolves to a net (issue #2692).

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
    """
    nets: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for pin in circuit.each_pin():
        name = (pin.name() or "").upper()
        net = circuit.net_for_pin(pin.id())
        if not name or net is None:
            continue
        if name in nets:
            ambiguous.add(name)
        nets[name] = net
    for name in ambiguous:
        del nets[name]
    return nets


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
    layout_nets = _pin_nets_by_name(layout_circuit)
    reference_nets = _pin_nets_by_name(reference_circuit)
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
    return anchors, _top_level_pin_anchor_warnings(anchors)


def _top_level_pin_anchor_warnings(
    anchors: Sequence[_TopLevelPinAnchor],
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
    if not anchors:
        return []
    pins = [anchor.pin for anchor in anchors]
    sample = pins[:_ANCHORED_PIN_SAMPLE_LIMIT]
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
            '"Top-level pin anchoring"',
            "both",
            details={
                "pins": sample,
                "pin_count": len(pins),
                "pins_truncated": len(sample) < len(pins),
            },
        )
    ]


def _parse_anchor_top_level_pins(
    options: Mapping[str, Any], engine: str
) -> tuple[bool | None, Any]:
    """Resolve ``options.anchor_top_level_pins`` into ``(requested, echo)``
    (issue #2692).

    ``requested`` is ``None`` when the option was omitted -- the
    form-dependent default :func:`_anchor_top_level_pins_enabled` applies --
    and the caller's own boolean otherwise. ``echo`` is the caller's value
    verbatim (``None`` when omitted), so a committed report round-trips back
    into the request document it came from, matching
    ``options.power_connectivity``'s own echo discipline.

    A non-boolean value is a clean request error, like every other option
    parser in this module. An explicit ``true`` on ``"engine": "netgen"`` is
    rejected rather than silently ignored: anchoring is implemented with
    ``klayout.db``'s ``NetlistComparer.same_nets``, the same hook
    ``request.hints`` is rejected for on that engine (see
    ``docs/cli/lvs.md``, "Engine"). An explicit ``false`` is accepted on
    both engines -- it asks for the behaviour netgen already has.
    """
    if "anchor_top_level_pins" not in options:
        return None, None
    value = options["anchor_top_level_pins"]
    if not isinstance(value, bool):
        raise LvsError(
            "options.anchor_top_level_pins must be a boolean -- true to "
            "anchor every top-level pin name declared on both sides by name "
            "before comparing, false to compare on topology alone"
        )
    if value and engine != "klayout":
        raise LvsError(
            "options.anchor_top_level_pins is only supported for engine "
            "'klayout' -- it is applied through klayout.db's "
            "NetlistComparer.same_nets, the same hook request.hints is "
            "rejected for on the netgen engine (see docs/cli/lvs.md, "
            '"Engine")'
        )
    return value, value


def _parse_strict_top_level_pin_count(options: Mapping[str, Any], engine: str) -> bool:
    """Resolve ``options.strict_top_level_pin_count`` (issue #2845); default
    ``False``. A non-boolean is a clean request error. The check compares
    ``Circuit.pin_count()`` of the two selected circuits, which both engines
    expose, so it is supported on every engine."""
    value = options.get("strict_top_level_pin_count", False)
    if not isinstance(value, bool):
        raise LvsError(
            "options.strict_top_level_pin_count must be a boolean -- true to "
            "report a mismatch when the two top circuits declare a different "
            "number of pins, false (the default) to leave pin counts "
            "informational"
        )
    return value


def _parse_compare_mos_flavour(
    options: Mapping[str, Any],
    engine: str,
    layout_spec: Mapping[str, Any],
    reference_spec: Mapping[str, Any],
) -> bool:
    """Resolve ``options.compare_mos_flavour`` (issue #3650); default ``False``.

    ``true`` makes a MOS flavour mismatch (e.g. a native-Vt layout device
    against an ``nfet_g5v0d10v5`` reference) a compare failure instead of
    collapsing both to the base ``nfet`` class. A non-boolean is a request
    error, and so is ``true`` wherever a side cannot say what flavour a device
    is -- failing loudly rather than reporting a match that never checked it:
    only ``engine: "klayout"``, an inline-extracted ``layout.file`` (the
    flavour is a property of the extracted device; a SPICE netlist carries
    none) and a ``reference.form: "subckt-call"`` reference (the flavour is the
    model name an ``X`` card names) qualify.
    """
    value = options.get("compare_mos_flavour", False)
    if not isinstance(value, bool):
        raise LvsError(
            "options.compare_mos_flavour must be a boolean -- true to compare "
            "a MOS device's flavour (e.g. sky130 nvt/hvi) as well as its base "
            "nfet/pfet class, false (the default) to treat every flavour of a "
            "MOS device as the same class"
        )
    if not value:
        return False
    if engine != "klayout":
        raise LvsError(
            "options.compare_mos_flavour is only supported for engine "
            "'klayout' -- the netgen engine has no per-device flavour to compare"
        )
    if "file" not in layout_spec:
        raise LvsError(
            "options.compare_mos_flavour requires an inline-extracted "
            "request.layout.file: a pre-extracted layout.netlist carries no "
            "MOS flavour to compare"
        )
    if reference_spec.get("form", "plain-element") != "subckt-call":
        raise LvsError(
            "options.compare_mos_flavour requires request.reference.form "
            '"subckt-call": the reference flavour is read from the model name '
            "of each X card, which the other reference forms do not carry"
        )
    return True


def _split_layout_mos_flavours(netlist: kdb.Netlist, enabled: bool) -> None:
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


def _strict_pin_count_check(
    enabled: bool,
    status: str,
    counts: dict[str, Any],
    layout_circuit: Any,
    reference_circuit: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """Issue #2845: when ``enabled`` and the compared circuits' pin counts
    differ, force ``status`` to ``"mismatch"`` and return one ``pin.count``
    error finding carrying both counts. Names/order are not compared and
    ``counts`` is never altered. Otherwise ``(status, [])``."""
    layout_pins = counts["pins"]["layout"]
    reference_pins = counts["pins"]["reference"]
    if not enabled or layout_pins == reference_pins:
        return status, []
    finding = _mismatch(
        CATEGORY_PIN_COUNT,
        "error",
        f"top-level pin count differs: layout circuit '{layout_circuit.name}' "
        f"declares {layout_pins} pin(s), reference circuit "
        f"'{reference_circuit.name}' declares {reference_pins} "
        "(options.strict_top_level_pin_count)",
        "both",
        details={
            "layout_pin_count": layout_pins,
            "reference_pin_count": reference_pins,
        },
    )
    return "mismatch", [finding]


def _parse_layout_rename_declared_pins(layout_spec: Mapping[str, Any]) -> bool:
    """Resolve ``request.layout.rename_declared_pins`` (issue #2854);
    default ``False``.

    A non-boolean is a clean request error, the same convention
    ``layout.top_cell_pins``'s bool coercion and every option parser in this
    module follow. An explicit ``true`` with no ``layout.declared_pins`` is
    rejected rather than silently ignored: there is no declared set to
    rename *to* (this request shape has no ``--def-pins`` equivalent), and
    silently extracting an unrenameable netlist would defeat the option's
    whole purpose -- canonical pin names for
    ``options.anchor_top_level_pins`` to anchor against a reference.

    Called only for the inline-extraction (``layout.file``) shape; a
    pre-extracted ``layout.netlist``'s pins are already fixed, so the field
    is silently ignored there exactly as ``top_cell_pins``/
    ``declared_pins`` are.
    """
    value = layout_spec.get("rename_declared_pins", False)
    if not isinstance(value, bool):
        raise LvsError(
            "request.layout.rename_declared_pins must be a boolean -- true "
            "to rename each promoted pin net whose component labels contain "
            "exactly one declared_pins name to that name, false (the "
            "default) to keep KLayout's joined multi-label net names"
        )
    if value and "declared_pins" not in layout_spec:
        raise LvsError(
            "request.layout.rename_declared_pins requires "
            "request.layout.declared_pins -- there is no declared pin set "
            "to rename to"
        )
    return value


def _declared_pin_short_mismatches(
    pin_renames: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """One :data:`CATEGORY_PIN_DECLARED_SHORT` ``mismatches[]`` entry per
    ``error``-severity finding in the ``pin_renames`` block inline
    extraction returned (issue #2854), or ``[]``.

    ``klt lvs`` otherwise discards the extraction's own warnings
    (``_resolve_layout`` drops them -- LVS is topological), but a conductor
    shorting two declared interface pins is a *finding about the layout's
    declared interface*, not extraction prose: it explains the mismatch the
    compare already reports (the layout's one joined boundary net against
    the reference's two separate pins), so it is surfaced here as its own
    named entry -- the same append-here, diagnostic-only rationale
    ``device.class_family_unsatisfiable`` follows. The ``warning``-severity
    rename/collision disclosures stay extract-report-only: they cannot
    change what the compare pairs.
    """
    if not pin_renames:
        return []
    entries: list[dict[str, Any]] = []
    findings = pin_renames.get("findings")
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict) or finding.get("severity") != "error":
            continue
        declared_pins = finding.get("declared_pins")
        entries.append(
            _mismatch(
                CATEGORY_PIN_DECLARED_SHORT,
                "error",
                str(
                    finding.get("message")
                    or "a promoted pin net carries 2+ declared pin names "
                    "(layout.rename_declared_pins) -- one conductor "
                    "shorting two declared interface pins"
                ),
                "layout",
                details={
                    "net": finding.get("net"),
                    "declared_pins": declared_pins
                    if isinstance(declared_pins, list)
                    else [],
                },
            )
        )
    return entries


def _is_reader_placeholder_circuit(circuit: Any) -> bool:
    """Whether ``circuit`` has the shape ``NetlistSpiceReader`` synthesises
    for an ``X`` card naming an undefined subcircuit (issue #2952): no
    devices, no subcircuit instances, and pins named positionally ``1`` ..
    ``N`` (the reader has no ``.subckt`` header to take pin names from).

    The positional-pin-name test is what keeps an included library's real,
    defined subcircuit (whose header names its own pins) from ever being
    mistaken for a placeholder, even though this module cannot see the text
    of a ``.include``-d file."""
    if any(True for _ in circuit.each_device()):
        return False
    if any(True for _ in circuit.each_subcircuit()):
        return False
    names = [pin.name() for pin in circuit.each_pin()]
    return names == [str(index) for index in range(1, len(names) + 1)]


def _reachable_reference_circuits(netlist: Any, top: str | None) -> list[Any]:
    """The reference circuits reachable from the compared top (``top`` by
    name, else the netlist's sole top circuit), each once, in visit order.

    ``[]`` when the top cannot be resolved -- :func:`_select_circuit`'s own
    later call raises the authoritative error for that, so this
    pre-compare scan never pre-empts it with a different one."""
    try:
        root = _select_circuit(netlist, top, "reference")
    except LvsError:
        return []
    seen: set[int] = set()
    ordered: list[Any] = []
    stack = [root]
    while stack:
        circuit = stack.pop()
        if id(circuit) in seen:
            continue
        seen.add(id(circuit))
        ordered.append(circuit)
        stack.extend(sub.circuit_ref() for sub in circuit.each_subcircuit())
    return ordered


def _unresolved_reference_subckt_calls(
    reference_netlist: Any,
    layout_names: AbstractSet[str],
    *,
    form: str,
    top: str | None,
    reference_path: str,
) -> list[dict[str, Any]]:
    """Issue #2952: one :data:`CATEGORY_REFERENCE_UNRESOLVED_SUBCKT_CALL`
    ``severity: "error"`` entry per reference subcircuit instance (reachable
    from the compared top) whose callee the reader could only satisfy with a
    synthesised placeholder circuit -- see
    :func:`_is_reader_placeholder_circuit` -- or ``[]``.

    Runs right after the reference is read, before any flatten/prune, so the
    hierarchy that names each offending instance still exists. A callee is
    **not** reported when:

    * a ``.subckt`` of that name (case-insensitive) is defined in the
      reference file itself -- a genuinely empty, positionally-pinned
      definition is the author's own choice (files pulled in by
      ``.include`` are not followed, so such a definition in an included
      file is reported);
    * ``layout_names`` (the layout's upper-cased circuit names, snapshotted
      *before* ``options.flatten_layout`` runs so a flattened layout keeps
      this exemption) has a circuit of the same name -- the comparer then
      pairs the two by name (a black-box/``--abstract-cells`` macro, or a
      custom device class written as an ``X ... PARAMS:`` card on both sides
      with no ``deck`` given; see docs/cli/lvs.md), so the instance is
      compared, not dropped;
    * ``form`` is ``"gate-level-verilog"``, whose library-cell instances are
      pin-only by design and carry their own pin-order contract.

    A curated or deck-custom device class never reaches here as a
    placeholder: the ``subckt-call`` conversion and the reader's custom-class
    / resistor recovery turn those cards into real devices first.
    """
    if form == "gate-level-verilog":
        return []
    from .netlist_normalize import defined_subckt_names

    try:
        with open(reference_path, encoding="utf-8", errors="replace") as handle:
            defined = defined_subckt_names(handle.read())
    except OSError:
        defined = set()
    findings: list[dict[str, Any]] = []
    for parent in _reachable_reference_circuits(reference_netlist, top):
        for sub in parent.each_subcircuit():
            callee = sub.circuit_ref()
            mangled = callee.name.upper()
            base = mangled.split("(", 1)[0]
            if not _is_reader_placeholder_circuit(callee):
                continue
            if base in defined or mangled in layout_names or base in layout_names:
                continue
            findings.append(
                _mismatch(
                    CATEGORY_REFERENCE_UNRESOLVED_SUBCKT_CALL,
                    "error",
                    f"reference instance '{sub.name}' in circuit "
                    f"'{parent.name}' calls subcircuit '{base}', which is "
                    "not defined in the reference netlist, was not converted "
                    "to a device, and has no same-named layout circuit -- "
                    "the reader substitutes an empty placeholder, so "
                    "whatever device(s) it stands for are never compared. "
                    "Define the subcircuit, map it to a device class "
                    '(reference.form "subckt-call" + reference.device_map), '
                    "or write it as a plain-element card (see "
                    'docs/cli/lvs.md, "reference.unresolved_subckt_call")',
                    "reference",
                    circuit={"layout": None, "reference": parent.name},
                    instance={"layout": None, "reference": sub.name},
                    subcircuit={"layout": None, "reference": base},
                    details={
                        "callee": base,
                        "reader_circuit": callee.name,
                        "pin_count": callee.pin_count(),
                    },
                )
            )
    return findings


def _force_mismatch_on(status: str, findings: list[dict[str, Any]]) -> str:
    """``"mismatch"`` when ``findings`` is non-empty, else ``status``
    unchanged (issue #2952's verdict override, factored out so the caller
    adds no branch)."""
    return "mismatch" if findings else status


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


# --------------------------------------------------------------------------- #
# Mask-option reference resolution: hints.short_nets (issue #2653)
# --------------------------------------------------------------------------- #


def _apply_short_nets_hint(
    hints: dict[str, Any],
    reference_netlist: Any,
    reference_circuit: Any,
) -> list[dict[str, Any]]:
    """Resolve a mask-option (metal-option/probe-pad/e-fuse trim) reference
    netlist's behavioural option construct to one concrete drawn state before
    the comparer is built (issue #2653).

    **The gap this closes.** A mask-option block's schematic states every
    option behaviourally in one netlist -- each link is a resistor whose
    value is an expression over a code parameter, evaluating to a "drawn/
    shorted" sentinel (a near-zero resistance) or a "cut/absent" sentinel (a
    near-infinite resistance). The drawn layout realises exactly one option:
    a shorted link is real metal with no fabricated device at all, and a cut
    link is simply absent -- never a resistor either way. Without this hook,
    every link resistor the reference declares is an unmatched
    ``device.unmatched`` device (the layout has none), and a shorted link's
    two reference nodes stay distinct while the layout has them tied into one
    net, so net correspondence fails too. There is no way to express "compare
    against option code 128" at all.

    ``hints.short_nets`` is the dual of ``hints.same_nets`` (a cross-side net
    *pairing* assertion): a unilateral, reference-only declaration of which
    reference nets to merge and which reference devices to drop, applied
    directly to ``reference_circuit`` -- the *selected* top circuit being
    compared (the same circuit ``hints.same_nets``/``hints.equivalent_pins``
    scope their own net/pin lookups to), not every circuit in
    ``reference_netlist``. ``spec`` is ``hints["short_nets"]``:

    - ``"nets"``: ``[[net_a, net_b, ...], ...]`` -- each inner list names two
      or more reference net names (resolved via
      ``Circuit.net_by_name()``, exact match only -- a mask-option block's own
      node names are caller-authored, unlike a device-class/model name, so
      there is no SPICE-reader-upper-casing ambiguity to paper over here) to
      join into a single net via ``Circuit.join_nets()``, modelling a
      "drawn/shorted" link's real metal connection. The merged net's own
      ``expanded_name()`` becomes the comma-joined label KLayout always
      produces for a merge (``"A,B"``) -- the same spelling
      :func:`~klayout_tools.extract.spice_safe_net_name` already normalises
      to the pipe-joined form (``"A|B"``) `klt extract`'s own
      ``merged_net_labels`` reports use, reused here rather than re-derived.
    - ``"devices"``: ``[name_or_glob, ...]`` -- reference device instance
      names (as read back by ``NetlistSpiceReader`` -- note that reader
      strips a SPICE element-type prefix letter and upper-cases the rest, so
      a ``.subckt`` card written ``rs1 ...`` reads back as device ``"S1"``,
      the same spelling every other ``mismatches[].device.reference`` field
      in this report already uses) or ``fnmatch.fnmatchcase`` glob patterns
      (matched case-insensitively against each device's own
      ``expanded_name()``) naming every link device -- both "drawn" and
      "cut" -- to remove from ``reference_circuit`` via
      ``Circuit.remove_device()``, since neither has a fabricated counterpart
      in a correctly drawn layout.

    A net name that does not resolve, a device pattern that matches nothing,
    or a malformed entry is an :class:`LvsError`, never a silent no-op --
    the same "a typo'd hint should be visible" discipline ``hints.same_nets``
    applies. Returns ``[]`` immediately when ``hints`` carries no
    ``short_nets`` key at all (the overwhelmingly common case), so a request
    that does not use this hook is completely unaffected -- opt-in only, no
    behavior change to any existing reference path.

    Returns one ``severity: "warning"`` :data:`CATEGORY_HINTS_SHORT_NETS_APPLIED`
    entry naming every merged net group and dropped device, which ``run_lvs``
    appends to ``mismatches[]``. The disclosure is the point, exactly as for
    :func:`_apply_reference_device_bulk`: resolving the reference to one
    option code is a caller assertion, not something the compare verifies
    independently, so a ``"match"`` reached this way is never silently
    indistinguishable from a fully independent one.

    ``hints.short_nets`` is looked up by **key presence**, not truthiness
    (matching :func:`_parse_combine_devices_per_circuit`'s own convention):
    an absent key is a clean no-op (returns ``[]`` immediately, the
    overwhelmingly common case -- opt-in only, no behavior change to any
    existing reference path), while a *present* key that is not a non-empty
    JSON object naming at least one of ``"nets"``/``"devices"`` is a request
    error, not a silent no-op -- an empty ``{}`` almost always means the
    caller forgot to fill in the hint, not that they meant nothing to apply.
    """
    if "short_nets" not in hints:
        return []
    spec = hints["short_nets"]
    if not isinstance(spec, dict):
        raise LvsError(
            "hints.short_nets must be a JSON object with 'nets' and/or 'devices' keys"
        )

    net_groups = spec.get("nets") or []
    device_patterns = spec.get("devices") or []
    if not net_groups and not device_patterns:
        raise LvsError(
            "hints.short_nets must declare at least one of 'nets' (reference "
            "net groups to merge) or 'devices' (reference devices to drop)"
        )

    dropped = _short_nets_drop_devices(reference_circuit, device_patterns)
    merged = _short_nets_merge_nets(reference_circuit, net_groups)
    if not dropped and not merged:
        return []

    # Issue #2653: emptied interior nodes (a dropped link device's own net,
    # now with zero terminals/pins/subcircuit-pins) are purged the same way
    # `options.combine_devices` already cleans up its own interior nodes --
    # left in place they would inflate `counts.nets.reference` and surface as
    # spurious `net.unmatched` findings no caller could act on.
    _purge_emptied_nets(reference_netlist)

    return [_short_nets_disclosure(merged, dropped)]


def _short_nets_drop_devices(
    reference_circuit: Any,
    device_patterns: Any,
) -> list[dict[str, str]]:
    """Remove every ``hints.short_nets.devices`` match from
    ``reference_circuit``, returning one ``{"name", "class"}`` record per
    dropped device for the disclosure entry (issue #2653).

    Split out of :func:`_apply_short_nets_hint` purely to keep each half of
    the hint's apply step under the repo's cyclomatic-complexity ratchet
    (`scripts/check_complexity_baseline.py`); it has no independent caller.
    See that function's docstring for the device-name spelling (the
    reader-stripped, upper-cased ``expanded_name()`` form) and for why a
    pattern that matches nothing is an error rather than a no-op.
    """
    if not device_patterns:
        return []
    if not isinstance(device_patterns, list) or not all(
        isinstance(pattern, str) and pattern for pattern in device_patterns
    ):
        raise LvsError(
            "hints.short_nets.devices must be a non-empty list of "
            "reference device names or glob patterns"
        )
    all_devices = list(reference_circuit.each_device())
    dropped: list[dict[str, str]] = []
    for pattern in device_patterns:
        matched = [
            device
            for device in all_devices
            if fnmatch.fnmatchcase(device.expanded_name().upper(), pattern.upper())
        ]
        if not matched:
            present = sorted({device.expanded_name() for device in all_devices}) or [
                "none"
            ]
            raise LvsError(
                f"hints.short_nets.devices: pattern {pattern!r} matched "
                f"no device on reference circuit "
                f"'{reference_circuit.name}' (devices present: "
                f"{', '.join(present)})"
            )
        for device in matched:
            dropped.append(
                {
                    "name": device.expanded_name(),
                    "class": device.device_class().name,
                }
            )
            reference_circuit.remove_device(device)
    return dropped


def _short_nets_merge_nets(
    reference_circuit: Any,
    net_groups: Any,
) -> list[dict[str, Any]]:
    """Join every ``hints.short_nets.nets`` group into one net on
    ``reference_circuit``, returning one ``{"members", "merged_net"}`` record
    per group for the disclosure entry (issue #2653).

    Split out of :func:`_apply_short_nets_hint` for the same
    complexity-ratchet reason as :func:`_short_nets_drop_devices`; no
    independent caller. See that function's docstring for the merged-name
    spelling (``klt extract``'s own pipe-joined ``merged_net_labels`` form)
    and for why an unresolvable net name is an error.
    """
    if not net_groups:
        return []
    if not isinstance(net_groups, list):
        raise LvsError(
            "hints.short_nets.nets must be a list of reference net-name "
            "groups, each with two or more entries"
        )
    merged: list[dict[str, Any]] = []
    for group in net_groups:
        if (
            not isinstance(group, list)
            or len(group) < 2
            or not all(isinstance(name, str) and name for name in group)
        ):
            raise LvsError(
                "hints.short_nets.nets entries must be a list of two or "
                "more reference net names to merge into one net -- got "
                f"{group!r}"
            )
        nets = []
        for name in group:
            net = reference_circuit.net_by_name(name)
            if net is None:
                raise LvsError(
                    f"hints.short_nets.nets: reference net '{name}' not "
                    f"found on circuit '{reference_circuit.name}'"
                )
            nets.append(net)
        primary = nets[0]
        for other in nets[1:]:
            # Issue #2653: a group naming the same already-merged net twice
            # (e.g. a later group's own join already folded it in) is a
            # harmless no-op, not a double-join error.
            if other is not primary:
                reference_circuit.join_nets(primary, other)
        merged.append(
            {
                "members": list(group),
                "merged_net": spice_safe_net_name(primary.expanded_name()),
            }
        )
    return merged


def _short_nets_disclosure(
    merged: list[dict[str, Any]],
    dropped: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the single :data:`CATEGORY_HINTS_SHORT_NETS_APPLIED` entry
    disclosing what ``hints.short_nets`` resolved (issue #2653).

    Split out of :func:`_apply_short_nets_hint` for the same
    complexity-ratchet reason as the two apply helpers above; no independent
    caller. Always ``severity: "warning"`` -- see :func:`_apply_short_nets_hint`
    for why the disclosure, not the transform, is the point.
    """
    description_parts = []
    if merged:
        groups_text = "; ".join(
            f"{entry['members']} -> '{entry['merged_net']}'" for entry in merged
        )
        description_parts.append(
            f"merged {len(merged)} reference net group(s) ({groups_text})"
        )
    if dropped:
        names_text = ", ".join(f"'{entry['name']}'" for entry in dropped)
        description_parts.append(
            f"dropped {len(dropped)} reference device(s) ({names_text})"
        )

    return _mismatch(
        CATEGORY_HINTS_SHORT_NETS_APPLIED,
        "warning",
        "request.hints.short_nets resolved the reference to one concrete "
        "mask-option state before comparing: "
        + " and ".join(description_parts)
        + " -- this resolution was asserted by the request, not read "
        "from the reference netlist's own device values, so this "
        "dimension of the compare is not independently verified (see "
        "docs/cli/lvs.md, 'hints.short_nets_applied')",
        "reference",
        device=None,
        net=None,
        details={
            "merged_nets": merged,
            "dropped_devices": dropped,
        },
    )


#: Issue #1907: the *primary* (compared) parameter of each device family whose
#: `form: "subckt-call"` conversion writes a literal `0` placeholder into the
#: plain-element card's positional value slot -- `DeviceClassResistor`'s `R`
#: and `DeviceClassCapacitor`'s `C` (each class's only primary parameter;
#: `L`/`W`/`A`/`P` are secondary and are not compared by default either way).
#: Keyed by the family name `netlist_normalize.ReferenceConversion`
#: reports.
_PLACEHOLDER_VALUE_PARAMETER = {"resistor": "R", "capacitor": "C"}


def _resistor_value_classes(
    compare_parameters: Mapping[str, Sequence[str]] | None,
) -> list[str]:
    """Device-class names ``options.compare_parameters`` asks to compare ``R``
    on (issue #2905) -- the opt-in for a ``form: "subckt-call"`` reference to
    carry a deck-derived ``R`` instead of the ``0`` placeholder.

    Opt-in on purpose: filling ``R`` for every deck-modelled resistor would
    silently turn existing ``match`` results into value comparisons against
    the deck's sheet resistance. Naming ``R`` is the caller asking for it.
    """
    if not compare_parameters:
        return []
    return [
        name
        for name, params in compare_parameters.items()
        if any(param.lower() == "r" for param in params)
    ]


def _apply_reference_placeholder_values(
    spec: dict[str, str],
    layout_netlist: Any,
    reference_netlist: Any,
    excluded_parameters: set[tuple[str, str]] | None = None,
    *,
    mutate_classes: bool = True,
    netgen_exclusions: list[tuple[str, str, str]] | None = None,
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

    ``excluded_parameters`` (issue #2461), when given, collects every
    ``(class name, parameter name)`` pair this function took out of the
    compare, lower-cased, for both sides' class spellings -- see
    :data:`~klayout_tools.lvs_mismatch.ExcludedParameters`. Nothing
    downstream may then report that parameter as a ``device.property`` error
    in the same run that discloses it as excluded here.

    ``mutate_classes=False`` (issue #2673) runs the identical eligibility
    proof and disclosure but leaves the ``klayout.db`` device classes
    untouched -- the ``engine: "netgen"`` branch, which has no
    ``equal_parameters`` hook and instead carries the proven exclusions into
    its generated setup file. ``netgen_exclusions``, when given, collects one
    ``(layout class name, reference class name, parameter)`` triple per
    excluded class for that purpose; nothing is ever inferred from netgen's
    reported ``value ... 0`` text.
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

        if mutate_classes:
            reference_class.equal_parameters = kdb.EqualDeviceParameters.ignore(
                reference_parameter_id
            )
            layout_class.equal_parameters = kdb.EqualDeviceParameters.ignore(
                layout_parameter_id
            )
        if netgen_exclusions is not None:
            netgen_exclusions.append(
                (layout_class.name, reference_class.name, parameter)
            )

        if excluded_parameters is not None:
            # Issue #2461: record it under *both* sides' own class spelling
            # (`NetlistSpiceReader` upper-cases a SPICE-read class name while
            # a deck-declared one keeps its own casing, and the lookups
            # downstream key off whichever object the comparer handed them).
            excluded_parameters.add((reference_class.name.lower(), parameter.lower()))
            excluded_parameters.add((layout_class.name.lower(), parameter.lower()))

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


def _record_excluded_parameters(
    device_class: Any,
    wanted_lower: set[str],
    excluded_parameters: set[tuple[str, str]] | None,
    narrowed_parameters: set[tuple[str, str]] | None = None,
    primary_before: AbstractSet[str] = frozenset(),
) -> None:
    """Note every parameter ``_apply_compare_parameters`` just disabled on
    ``device_class``, keyed by that side's own class spelling -- the two
    sides' spellings can differ, see
    :func:`_apply_reference_placeholder_values`.

    **Two sets, because they answer two different questions** (issue #2654):

    * ``excluded_parameters`` (issue #2461) -- *every* parameter this option
      just disabled, whatever its state beforehand. This is the set the
      classification pass downstream reads to keep itself from reporting a
      ``device.property`` error on, or vetoing an
      ``options.parameter_tolerance`` snap over, a parameter this run does
      not compare. It has to stay complete: that pass deliberately walks
      *secondary* parameters too (see
      :func:`~klayout_tools.lvs_mismatch._param_pair_is_comparable`), so
      dropping one from this set would put a difference the engine never
      looked at back into ``mismatches[]``.
    * ``narrowed_parameters`` (issue #2654) -- only the parameters this
      option genuinely *took out of* the compare: the ones whose
      ``is_primary`` was ``True`` before ``enable_parameter`` ran
      (``primary_before``, lower-cased). A parameter the device class itself
      already declared secondary was never going to be compared, so naming a
      sibling parameter did not exclude it and the report must not say it
      did -- it is disclosed as ``reason: "secondary"`` instead (see
      :data:`PARAMETER_NOT_COMPARED_SECONDARY`).

    A no-op for whichever set the caller did not ask for.
    """
    excluded = {
        (device_class.name.lower(), param.name.lower())
        for param in device_class.parameter_definitions()
        if param.name.lower() not in wanted_lower
    }
    if excluded_parameters is not None:
        excluded_parameters.update(excluded)
    if narrowed_parameters is not None:
        narrowed_parameters.update(
            pair for pair in excluded if pair[1] in primary_before
        )


def _apply_compare_parameters(
    spec: dict[str, list[str]] | None,
    layout_netlist: Any,
    reference_netlist: Any,
    excluded_parameters: set[tuple[str, str]] | None = None,
    narrowed_parameters: set[tuple[str, str]] | None = None,
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
    entry per parameter this option **narrowed away** from a named class (one
    entry regardless of how many sides that class resolves on, ``side:
    "both"``), which ``run_lvs`` appends to ``mismatches[]``. The disclosure
    is the point -- same discipline as ``device.bulk_reconciled``/
    ``device.placeholder_value``: a ``"match"`` reached with a parameter
    scoped out is never silently indistinguishable from one where every
    parameter actually agreed.

    **"Narrowed away", not "unnamed"** (issue #2654). Because the option can
    only narrow, enabling one secondary parameter of a class necessarily
    disables every other parameter that class declares -- so naming
    ``["R", "L", "W"]`` on a resistor also disables its ``A``/``P``. Those
    two were ``is_primary == False`` by ``DeviceClassResistor``'s own
    definition and therefore never took part in any default compare either:
    the caller's option changed nothing about them, and crediting it with
    excluding them attributed to the caller an exclusion that is KLayout's
    own class default (the caller asked for *more* coverage and the report
    answered with two warnings saying it dropped two axes). Only a parameter
    that was primary -- actually being compared -- immediately before this
    function ran yields an entry; the rest are disclosed, accurately, as
    ``reason: "secondary"`` by :func:`_device_parameter_coverage` and by the
    ``device.geometry_not_compared`` warning beside it.

    ``excluded_parameters`` (issue #2461) and ``narrowed_parameters``
    (issue #2654), when given, collect ``(class name, parameter name)`` pairs
    disabled here, lower-cased, under each resolved side's own class
    spelling -- the first every one of them, the second only the ones that
    were primary beforehand; see :func:`_record_excluded_parameters` for why
    the two differ, and
    :data:`~klayout_tools.lvs_mismatch.ExcludedParameters`. Before the first
    set existed, the classification pass downstream still walked *every* declared
    parameter and reported a ``device.property`` **error** for each one that
    differed, including the very parameters disclosed as excluded right here
    (issue #2462's measurement: a class scoped to ``["L", "W"]`` against a
    placeholder-excluded reference reported error findings on ``R``/``A``/
    ``P`` in the same report that said all three were excluded).
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

        # Issue #2654: which parameters were *actually taking part* in the
        # compare before this option ran, per resolved side.
        # `enable_parameter` flips `is_primary` in place, so the snapshot has
        # to be taken before the loop below -- and only a parameter that was
        # primary here is one this option genuinely narrowed away (see the
        # docstring's "Narrowed away, not unnamed").
        primary_before: list[tuple[Any, frozenset[str]]] = [
            (
                device_class,
                frozenset(
                    param.name.lower()
                    for param in device_class.parameter_definitions()
                    if param.is_primary
                ),
            )
            for device_class in resolved_classes
        ]

        for device_class, was_primary in primary_before:
            for param in device_class.parameter_definitions():
                device_class.enable_parameter(
                    param.name, param.name.lower() in wanted_lower
                )
            _record_excluded_parameters(
                device_class,
                wanted_lower,
                excluded_parameters,
                narrowed_parameters,
                was_primary,
            )

        # Primary on *either* resolved side is enough to disclose: the two
        # sides' class objects are distinct (and a class resolving on one side
        # only is legitimate), while the entry below is a single `side:
        # "both"` disclosure per parameter.
        narrowed_lower = {
            name
            for _, was_primary in primary_before
            for name in was_primary
            if name not in wanted_lower
        }

        for param_lower, param_name in sorted(known_params.items()):
            if param_lower in wanted_lower or param_lower not in narrowed_lower:
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
# device.geometry_not_compared / device_parameter_coverage (issue #2461):
# say which of a resistor/capacitor class's parameters the compare covered
# --------------------------------------------------------------------------- #

#: Issue #2461: the device-class families whose *geometry* sits entirely
#: outside a default compare, keyed by the `klayout.db` class name (resolved
#: lazily, the way :data:`_COMBINE_PARAMETER_CONSERVATION_RULES` is) to the
#: `device_kind` label reported for them. Both are matched with `isinstance`,
#: so each one's `*WithBulk` flavour -- a subclass declaring the identical
#: parameter set -- is covered without being listed.
#:
#: Deliberately *only* these two. A MOS class's own geometry (`L`/`W`) is
#: primary and therefore compared; its secondary `AS`/`AD`/`PS`/`PD` diffusion
#: parameters are a genuinely narrower gap and reporting them here would add a
#: warning to essentially every `klt lvs` run for no new information about
#: whether the device was *sized* correctly. For a resistor/capacitor there is
#: no such consolation: `R`/`C` is the only primary parameter, so no dimension
#: of the drawn device is compared at all.
_GEOMETRY_DISCLOSURE_CLASS_KINDS: tuple[tuple[str, str], ...] = (
    ("DeviceClassResistor", "resistor"),
    ("DeviceClassCapacitor", "capacitor"),
)

#: Issue #2461: the stable ``device_parameter_coverage[].not_compared[].reason``
#: values -- why one declared parameter of a participating class took no part
#: in the compare.
#:
#: * ``"secondary"`` -- the class's own built-in definition marks it
#:   non-primary (``is_primary == False``) and nothing in the request changed
#:   that, so ``kdb.NetlistComparer`` never compares it. This is the case
#:   issue #2461 was filed about: it has no other disclosure anywhere in the
#:   report, which is why it also yields a
#:   :data:`CATEGORY_DEVICE_GEOMETRY_NOT_COMPARED` warning. Issue #2654: this
#:   covers a parameter ``options.compare_parameters`` *disabled* while
#:   scoping a class to a different subset, too -- the option can only
#:   narrow, so enabling one parameter necessarily disables the siblings the
#:   caller did not name, and a sibling that was already secondary was never
#:   in the compare for the option to remove it from.
#: * ``"placeholder_value"`` -- excluded by
#:   :func:`_apply_reference_placeholder_values` (issue #1907); already
#:   disclosed as :data:`CATEGORY_DEVICE_PLACEHOLDER_VALUE`.
#: * ``"compare_parameters"`` -- *narrowed away* by
#:   :func:`_apply_compare_parameters` (issue #1928): it was being compared
#:   (``is_primary == True``) until that option scoped the class to a subset
#:   that does not name it. Already disclosed as
#:   :data:`CATEGORY_DEVICE_PARAMETER_EXCLUDED`. Reserved for exactly that
#:   case since issue #2654 -- never a parameter the class itself declared
#:   secondary.
PARAMETER_NOT_COMPARED_SECONDARY = "secondary"
PARAMETER_NOT_COMPARED_PLACEHOLDER_VALUE = "placeholder_value"
PARAMETER_NOT_COMPARED_COMPARE_PARAMETERS = "compare_parameters"


def _geometry_disclosure_kind(device_class: Any, kdb_module: Any) -> str | None:
    """The :data:`_GEOMETRY_DISCLOSURE_CLASS_KINDS` label for ``device_class``
    (``"resistor"``/``"capacitor"``), or ``None`` for every other class."""
    for class_name, kind in _GEOMETRY_DISCLOSURE_CLASS_KINDS:
        klayout_class = getattr(kdb_module, class_name, None)
        if klayout_class is not None and isinstance(device_class, klayout_class):
            return kind
    return None


def _participating_device_classes(netlist: Any) -> dict[str, tuple[Any, int]]:
    """``{<lower-cased class name>: (<class object>, <instance count>)}`` for
    every device class this netlist actually *instantiates*.

    Keyed off the devices rather than ``Netlist.each_device_class()`` on
    purpose: an extraction deck registers every class it knows about, so the
    class list alone would claim a compare covered classes the design never
    draws.
    """
    found: dict[str, tuple[Any, int]] = {}
    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            device_class = device.device_class()
            key = device_class.name.lower()
            existing = found.get(key)
            found[key] = (
                device_class if existing is None else existing[0],
                1 if existing is None else existing[1] + 1,
            )
    return found


def _device_parameter_coverage(
    layout_netlist: Any,
    reference_netlist: Any,
    placeholder_excluded: AbstractSet[tuple[str, str]],
    compare_scoped_narrowed: AbstractSet[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Which parameters of each participating resistor/capacitor class this
    compare actually covered (issue #2461).

    Returns ``(mismatch entries, coverage block)``:

    * one ``severity: "warning"``
      :data:`CATEGORY_DEVICE_GEOMETRY_NOT_COMPARED` entry per class that has
      at least one *secondary* parameter -- one nothing else in this report
      already discloses as excluded -- so the human-readable half of the
      report stops implying the geometry was checked;
    * the machine-checkable ``device_parameter_coverage`` block, built from
      the same determination so the two renderings cannot disagree (the same
      one-source-of-truth discipline ``body_verification``/
      ``device.body_unverified`` follow, issue #1983).

    **The gap.** ``kdb.DeviceClassResistor`` declares ``R``, ``L``, ``W``,
    ``A``, ``P`` and marks only ``R`` primary; ``kdb.DeviceClassCapacitor``
    declares ``C``, ``A``, ``P`` and marks only ``C`` primary.
    ``NetlistComparer`` compares primary parameters, so for either class the
    drawn device's whole geometry is outside every default compare -- a
    reference resistor whose ``W`` is an order of magnitude off the layout's
    still reports ``status: "match"`` with ``error_count: 0`` (verified
    against ``klayout 0.30.10``). Under ``reference.form: "subckt-call"`` the
    primary is excluded too, so *nothing* about the device is compared and
    the pairing rests on topology alone.

    **Read at compare time, never from the class's static defaults.**
    ``options.compare_parameters``' ``enable_parameter(name, wanted)`` flips
    ``is_primary`` in place, so a class scoped to include ``W`` has a
    *primary* ``W`` by the time this runs and is correctly reported as
    comparing it.

    **Never duplicates another disclosure.** A parameter in
    ``placeholder_excluded``/``compare_scoped_narrowed`` is already reported
    as ``device.placeholder_value``/``device.parameter_excluded``; it appears
    in the coverage block (with that ``reason``) but never in the warning,
    which is reserved for the case that had no disclosure at all.

    ``compare_scoped_narrowed`` is :func:`_apply_compare_parameters`'
    *narrowed* set, not its full excluded set (issue #2654): a parameter that
    option disabled but which the device class already declared secondary was
    never in any compare to begin with, so it gets no
    ``device.parameter_excluded`` entry and must not read
    ``reason: "compare_parameters"`` here either. It falls through to the
    ``"secondary"`` branch below -- and therefore back into the warning,
    which is the one disclosure that is accurate about it.

    Never raises and never touches the verdict: this is a read-only pass over
    two already-prepared netlists, run after every request-side hook has been
    applied and before the comparer is built.
    """
    import klayout.db as kdb

    layout_classes = _participating_device_classes(layout_netlist)
    reference_classes = _participating_device_classes(reference_netlist)

    entries: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []

    for key in sorted(set(layout_classes) | set(reference_classes)):
        layout_entry = layout_classes.get(key)
        reference_entry = reference_classes.get(key)
        device_class = (layout_entry or reference_entry)[0]  # type: ignore[index]
        kind = _geometry_disclosure_kind(device_class, kdb)
        if kind is None:
            continue

        class_name = device_class.name
        compared: list[str] = []
        not_compared: list[dict[str, str]] = []
        secondary: list[str] = []
        for param in device_class.parameter_definitions():
            pair = (key, param.name.lower())
            if pair in placeholder_excluded:
                reason = PARAMETER_NOT_COMPARED_PLACEHOLDER_VALUE
            elif pair in compare_scoped_narrowed:
                reason = PARAMETER_NOT_COMPARED_COMPARE_PARAMETERS
            elif param.is_primary:
                compared.append(param.name)
                continue
            else:
                reason = PARAMETER_NOT_COMPARED_SECONDARY
                secondary.append(param.name)
            not_compared.append({"parameter": param.name, "reason": reason})

        coverage.append(
            {
                "class": class_name,
                "device_kind": kind,
                "layout_devices": layout_entry[1] if layout_entry else 0,
                "reference_devices": reference_entry[1] if reference_entry else 0,
                "compared": compared,
                "not_compared": not_compared,
            }
        )

        if not secondary:
            # Every uncompared parameter of this class is already disclosed
            # by its own, more specific warning -- nothing left to say.
            continue

        entries.append(
            _mismatch(
                CATEGORY_DEVICE_GEOMETRY_NOT_COMPARED,
                "warning",
                f"device class '{class_name}' is a {kind} class, whose "
                f"{sorted(secondary)} parameter(s) KLayout declares "
                f"*secondary*: kdb.NetlistComparer compares primary "
                f"parameters only, so none of them took part in this compare "
                f"and each is NOT verified -- this {kind}'s geometry could "
                f"differ between the two sides by any amount and this run "
                f"would still report 'match'. "
                + (
                    f"The compared parameter(s) were {compared}. "
                    if compared
                    else "No parameter of this class was compared at all: "
                    "the remaining parameter(s) were separately excluded "
                    "from this run (see the device.placeholder_value/"
                    "device.parameter_excluded entries), so the two sides "
                    "were paired on topology alone. "
                )
                + "Name them in options.compare_parameters to compare them "
                "(see docs/cli/lvs.md, 'device.geometry_not_compared')",
                "both",
                device={
                    "layout": None,
                    "reference": None,
                    "class": class_name,
                },
                details={
                    "device_kind": kind,
                    "compared_parameters": compared,
                    "not_compared_parameters": sorted(secondary),
                    "layout_devices": layout_entry[1] if layout_entry else 0,
                    "reference_devices": reference_entry[1] if reference_entry else 0,
                },
            )
        )

    return entries, coverage


# --------------------------------------------------------------------------- #
# Environment / reproducibility
# --------------------------------------------------------------------------- #


def _engine_version() -> str | None:
    import klayout

    return getattr(klayout, "__version__", None)
