"""Run a cocotb regression against Icarus/Verilator, headless.

Pure library: :func:`run_functional_verification` returns plain Python data
(a ``dict`` of JSON-serialisable primitives) and never prints, mirroring
``lvs.py``/``sim.py``/``synthesize.py``. Serialisation and human-readable
formatting live in the CLI command module
(``cli/functional_verification_cmd.py``).

This is Phase 3 of [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class -- Yosys + OpenROAD -- RTL->GDS as a
first-class ``klt`` flow"), the build carried by two accepted Phase 1
spikes -- read them first:

- ``docs/design/cocotb-verification-spike.md`` (#398) settles the engine
  survey, the invocation surface, and the request/response contract.
- ``docs/design/digital-flow-contracts-spike.md`` section 6 (#399) restates
  that contract alongside the synthesis/place-and-route ones.

Five findings from those spikes are load-bearing here, and each is
implemented deliberately rather than rediscovered:

1. **Never trust a raw subprocess/``make`` exit code** (spike section 4).
   The same deliberately-failing regression was observed exiting ``2``,
   ``1``, and ``0`` through three different invocation paths. This module
   invokes cocotb's first-party Python ``Runner`` API (never the Makefile
   flow), and derives *every* count and the final verdict from the
   ``results.xml`` artifact it parses itself. ``cocotb_tools.runner``'s own
   ``Runner.test()`` can either ``sys.exit()`` on a simulator's nonzero
   return *or* return normally on a run with failing tests (both observed
   live) -- so the :class:`SystemExit` it may raise is caught and discarded
   in favour of the parsed artifact.
2. **``get_results()`` is not the source of truth** (spike section 4). Its
   ``num_tests`` counts skipped tests too, so it cannot produce the
   passed/failed/skipped breakdown the contract reports. This module parses
   ``results.xml`` directly: a ``<testcase>`` with no child is a pass, a
   ``<failure>`` child is a failure, a ``<skipped>`` child is a skip.
3. **``timescale`` must be passed to both ``Runner.build()`` and
   ``Runner.test()``** (spike section 3b) -- Icarus elaboration otherwise
   fails the moment a testbench's ``Clock(..., unit="ns")`` meets an unset
   (default 1 s) simulator precision.
4. **``options.coverage: true`` requires ``engine: "verilator"``** (spike
   sections 1/5) -- Icarus has no coverage path through this flow at all, so
   the combination is rejected up front rather than silently no-op'ing.
5. **``random_seed`` reproducibility** (spike's own "open questions", issue
   #423). ``request.options.random_seed``, when given, is forwarded to
   ``Runner.test()``'s own ``seed`` parameter (which sets
   ``COCOTB_RANDOM_SEED`` in the simulator subprocess's environment) so a
   pinned seed reproduces cocotb's own seeded ``random`` module state
   run-to-run. Whether pinned or left to cocotb's own generator, the
   *effective* seed is always echoed back in ``environment.random_seed`` --
   read from ``results.xml``'s own ``<property name="random_seed">``
   element (spike section 4), the same artifact-derived-truth discipline as
   every other count this module reports -- matching the reproducibility
   bar ``klt sim``'s Monte Carlo seeding and ``klt lvs``'s ``environment``
   hashes already set.

6. **SDF back-annotation is Icarus-only, and every one of its failure modes
   is non-fatal to the simulator's own exit code** (issue #1002,
   ``docs/design/sdf-annotate-feasibility-spike.md``, verified live on Icarus
   13.0). ``request.options.sdf`` re-runs a gate-level regression with real
   post-route delays back-annotated from an SDF file (the one
   ``klt place-and-route``'s ``request.post_route_sdf`` writes). Four things
   that spike established are implemented here rather than rediscovered:
   the ``$sdf_annotate`` call rides in a *generated second elaboration root*
   (:func:`_write_sdf_annotate_shim`) because a cocotb regression's
   ``hdl_toplevel`` is the DUT itself and has nowhere to put an ``initial``
   block; ``-gspecify`` and ``-ginterconnect`` are both mandatory and their
   absence is a **silent** zero-delay run; min/typ/max corner selection is
   the compile-time ``iverilog -T`` flag, not an ``$sdf_annotate`` argument;
   and the SDF path must be **absolute**, since a relative one resolves
   against ``vvp``'s own working directory. Above all, ``vvp`` exits ``0``
   after skipping an annotation it could not apply -- so
   :func:`_scan_sdf_diagnostics` treats an ``SDF WARNING``/``SDF ERROR`` line
   in either transcript as a failed run. This is the same "never trust a raw
   exit code, derive the verdict from the artifact" discipline as finding 1,
   one level deeper.

7. **A top-level-port-attached ``INTERCONNECT`` entry needs the DUT nested,
   not rooted** (issue #1056, live on Icarus 13.0). Every real post-route
   SDF's top design ``CELL`` block mixes purely-internal
   ``INTERCONNECT <inst>.<pin> <inst>.<pin>`` entries (always resolved fine)
   with entries touching a bare top-level port name, e.g.
   ``INTERCONNECT a buffer0.in`` (a primary input) or
   ``INTERCONNECT buffer2.out b`` (a primary output). The latter always
   failed with ``SDF ERROR: ... Could not find intermodpath!`` /
   ``Could not find net`` -- live testing ruled out the previously-suspected
   mechanism (this module's own generated-sibling-elaboration-root shim):
   calling ``$sdf_annotate`` from *inside* the DUT's own scope reproduces the
   identical failure, so the shim's placement was never the cause. The real
   mechanism: Icarus cannot resolve a bare port identifier against a module
   elaborated as its *own* ``-s`` root at all -- only against a module
   elaborated as a *nested child instance* of another root (confirmed against
   Icarus's own ``ivtest`` SDF regression fixtures, which use exactly that
   shape). :func:`_write_sdf_dut_wrapper` supplies it: a generated,
   transparent pass-through wrapper re-declares ``hdl_toplevel``'s exact port
   list, instantiates the real (unmodified) DUT as a nested child, and
   becomes the new elaboration root in ``hdl_toplevel``'s place -- verified
   live end-to-end through real cocotb + Icarus 13.0, not just raw
   ``iverilog``/``vvp``. Every other build path (baseline gate-level, RTL,
   coverage) is unaffected: the wrapper is only generated when
   ``request.options.sdf`` is present.

8. **The transcript the SDF gate reads can itself be corrupted** (issue
   #1136). Icarus's C-level diagnostic ``printf``s and cocotb's Python
   logging share one stdout file descriptor; under a non-tty capture both
   are commonly fully- rather than line-buffered, so a flush from one lands
   *mid-line* inside a not-yet-flushed line from the other. The observed
   splice ate the tail of a benign ``TIMINGCHECK`` warning -- including the
   substring finding 6's exemption keys on -- and a fully-applied
   annotation was reported as failed. :func:`_scan_sdf_diagnostics`
   therefore classifies a line only once it matches
   :data:`SDF_DIAGNOSTIC_LINE_RE` (the marker *plus* Icarus's own
   ``<file>:<line>:`` locator); a marker-bearing line without that shape is
   a splice, not evidence, and counts as neither actionable nor benign. A
   line that has the shape but embeds a cocotb log header
   (:data:`SDF_SPLICED_LOG_HEADER_RE`, issue #2996) is the same splice
   landing after the locator and is dropped likewise.

9. **A bit-selected top-level-port ``INTERCONNECT`` entry can poison a
   sibling entry on the same net** (issue #1619, live on Icarus 13.0) --
   distinct from finding 7's *scalar* bare-port fix, and only reachable
   once that fix let a *vector* port bit (``y[0]``, ``a[0]``) resolve.
   Two separate mechanisms, depending on which side of the entry carries
   the bit-selected port -- full write-up in the module comment above
   :func:`_split_sdf_bus_port_interconnects` and
   `docs/design/sdf-annotate-feasibility-spike.md` §3.7:
   (a) as a *destination*, order-dependent -- fixed by deferring every
   such entry to its own, later ``$sdf_annotate`` call; (b) as a *source*
   -- the one-modpath-per-port-net cap (issue #5705, raw
   ``iverilog``/``vvp`` 13.0): on a fanout-above-1 net ``$sdf_annotate``
   annotates only the last-declared instance's destination and rejects
   the rest, deterministically -- unfixable by reordering, left unfixed,
   pinned down by ``test_integration_real_icarus_sdf_bus_port_input_
   fanout_stays_unresolvable`` rather than silently reported as
   "annotated" or masked. The "wrapper double-binds the net identity"
   hypothesis is refuted by shape (a): reordering (bit-selected entry
   last) resolves everything, which a genuine double-bind would not.

10. **A run that fails on every test case under ``options.sdf`` -- often
    with the design's outputs reading a constant zero -- while the
    byte-identical run without it passes is (usually) a real timing result,
    not a broken annotation** (issue #1854, live on Icarus 13.0 against a
    real sky130 post-route design; `docs/design/sdf-annotate-feasibility-
    spike.md` §3.8). #1619 suspected Icarus's own ``-gspecify`` fallback
    ("Delayed reference and data signals become copies...") of being
    disabled once *any* ``$sdf_annotate`` call exists; measured directly,
    that fallback fires an identical 479 times with and without an annotate
    call in the design (it is gated on ``-gspecify``, which ``options.sdf``
    also adds), and an annotated run whose testbench clock clears the
    design's annotated critical path is verdict-for-verdict identical to
    the unannotated one. The constant-zero shape is instead what a design
    clocked *past* that critical path does: it never advances out of reset,
    so every output register holds its reset value on every case.
    Independent of annotation success in both directions -- runs with 16
    and 19 genuinely unresolved ``INTERCONNECT`` entries produced correct
    results, and the constant-zero runs had **zero** actionable
    diagnostics. Nothing to fix here or upstream; the deliverable is the
    ability to tell the two apart, pinned by
    ``test_integration_real_icarus_sdf_constant_zero_is_a_timing_outcome``.

11. **An ``INTERCONNECT`` entry whose destination is a top-level port bit
    the netlist drives through an ``assign`` alias cannot be annotated at
    all -- but when it is zero-delay, it does not need to be** (issue
    #2285, live on Icarus 13.0). This is the shape every P&R backend
    produces for a constant-driven output (a tie cell driving ``net0``,
    plus ``assign uio_oe[0] = net0;``), so a real post-route SDF could not
    pass finding 3's diagnostic gate at all even though every failing entry
    carried ``(0.000:0.000:0.000)``. Rewriting the endpoint through the
    netlist's own alias map does **not** help (the rewritten entry names
    the net its own source pin drives and fails with a different
    diagnostic, ``Could not find handles for both ports!``), so the entry
    is instead dropped from the SDF text handed to ``$sdf_annotate`` and
    counted as an ``environment.sdf.dropped`` class
    (:data:`SDF_ALIAS_PORT_DROPPED_CLASS`,
    :func:`_drop_sdf_zero_delay_alias_port_interconnects`). The exemption
    is bounded by the entry's own delay value, not by the diagnostic text:
    a nonzero delay at any corner on a separately assigned output bit
    is refused before the build. See the module comment above
    :func:`_drop_sdf_zero_delay_alias_port_interconnects` for the full
    write-up, including why Icarus's ``<file>:<line>:`` locator cannot be
    mapped back to an entry (its line counter is cumulative across
    ``$sdf_annotate`` calls, not reset per file).

Engines: ``"icarus"`` (default -- the CI-cheap interpreter) and
``"verilator"`` (opt-in, required for coverage). cocotb itself is an
*optional* runtime dependency, deliberately not in ``pyproject.toml``'s
``dependencies`` but in its own ``functional-verification`` extra: it is a
compiled package with a simulator toolchain behind it, and the large
majority of ``klt`` installs never verify anything. It is discovered at call
time and a missing install is a clear, actionable error -- exactly the
posture ``klt synthesize`` takes toward a missing ``yosys`` binary. (Until
issue #2593 the extra also carried a ``python_version < "3.14"`` marker,
because cocotb 2.0.1 refused to run on 3.14+; cocotb 2.1.0 added 3.14
support, the marker is gone, and CI now covers 3.10 through 3.14.)
"""

from __future__ import annotations

import functools
import importlib.metadata
import json  # noqa: F401 -- tests reach it as fv.json
import os
import re
import subprocess
import sys
from typing import Any

from ._artifact_envelope import ANCHOR_CWD as ANCHOR_CWD
from ._artifact_envelope import RepoRoot
from ._artifact_envelope import hashed_path_envelope as _hashed_path_envelope
from ._artifact_envelope import path_envelope as _path_envelope
from ._artifact_envelope import source_defines_envelope as _source_defines_envelope
from ._jsonnum import is_int
from ._paths import _load_request_json
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape
from .env_provenance import find_repo_root
from .env_provenance import repo_relative_path as repo_relative_path
from .functional_verification_base import (  # noqa: F401 - public re-exports
    DEFAULT_SDF_CORNER,
    DEFAULT_SDF_ENTRIES,
    SDF_ALIAS_PORT_DROPPED_CLASS,
    SDF_ANNOTATE_MODULE,
    SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS,
    SDF_CORNERS,
    SDF_DIAGNOSTIC_LINE_RE,
    SDF_DIAGNOSTIC_MARKERS,
    SDF_ENGINES,
    SDF_ENTRIES_MODES,
    SDF_MIN_ICARUS_MAJOR,
    SDF_OMITTED_ANNOTATION_MARKER,
    SDF_SPLICED_LOG_HEADER_RE,
    SDF_WRAPPER_DUT_INSTANCE,
    SDF_WRAPPER_MODULE,
    FunctionalVerificationError,
)
from .functional_verification_from_synthesize import (  # noqa: F401
    _resolve_sources,
    has_from_synthesize,
)
from .functional_verification_initial_state import (
    INITIAL_STATE_MODULE,
    SEED_PLUSARG,
    build_initial_state_verilog,
    count_d_nets,
)
from .functional_verification_pdk import (
    _resolve_defines,
)
from .functional_verification_python_runtime import (
    _check_icarus_python_runtime,
    _interpreter_mismatch_hint,
)
from .functional_verification_results import (  # noqa: F401 -- preserve earlier import paths
    _SIM_TIME_UNIT_NS_EXPONENT,
    _executed_result_counts,
    _extract_random_seed_property,
    _maybe_float,
    _testcase_properties,
    _testcase_sim_time_ns,
    parse_results_xml,
)
from .functional_verification_run_inputs import resolve_run_inputs, with_run_inputs
from .functional_verification_sdf import (
    _check_sdf_engine_capability,
    _collect_sdf_alias_port_bits,  # noqa: F401 -- tests reach it as fv._collect_sdf_alias_port_bits
    _collect_sdf_physical_only_instances,
    _drop_sdf_bit_selected_bus_iopaths,
    _drop_sdf_non_interconnect_entries,
    _drop_sdf_unsupported_edge_modpath_iopaths,
    _drop_sdf_zero_delay_alias_port_interconnects,  # noqa: F401 -- tests reach it as fv._drop_sdf_zero_delay_alias_port_interconnects
    _drop_sdf_zero_delay_bus_port_source_interconnects,  # noqa: F401 -- tests reach it as fv._drop_sdf_zero_delay_bus_port_source_interconnects
    _drop_sdf_zero_delay_physical_only_interconnects,
    _normalize_sdf_header_triples,  # noqa: F401 -- tests reach it as fv._normalize_sdf_header_triples
    _parse_toplevel_parameters,
    _parse_toplevel_ports,
    _reject_sdf_escaped_divider_interconnects,
    _reject_sdf_mis_scoped_top_cell,
    _reject_sdf_with_functional_models,
    _resolve_sdf_option,
    _scan_sdf_diagnostics,
    _sdf_scope_reference,  # noqa: F401 -- reached via _fv by the verilog_top module
    _split_sdf_bus_port_interconnects,
    _write_sdf_annotate_shim,
    _write_sdf_dut_wrapper,  # noqa: F401
)
from .functional_verification_sdf_instance import _reject_sdf_unresolvable_scopes
from .functional_verification_sdf_internal_bus import (
    SDF_INTERNAL_BUS_NET_REASONS,
    _apply_sdf_internal_bus_net_drop,
)
from .functional_verification_sdf_rename import _apply_sdf_escaped_divider_rename
from .functional_verification_sdf_steps import (
    SDF_BUS_PORT_SOURCE_DROPPED_CLASS,
    SDF_IOPATH_SYNTAX_REASONS,
    _apply_sdf_alias_port_drop,
    _apply_sdf_bus_port_source_drop,
    _apply_sdf_header_normalization,
    _apply_sdf_iopath_syntax_drops,
    _bus_input_port_names,
    _sdf_refusal_attribution,
    _sdf_refusal_error,
    _write_sdf_drop_step,
)
from .functional_verification_sdf_units import (
    _check_sdf_time_units,
    _sdf_annotation_report,
)
from .functional_verification_tools import (
    _ENGINE_VERSION_COMMANDS,
    _check_required_tool,
)
from .request_paths import RequestPaths
from .verilog_cell_models import sequential_elements

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 4

#: ``"icarus"`` first: it is the documented default, per the spike's own
#: CI-cost finding (~0.7 s vs. Verilator's ~8 s for the worked example).
SUPPORTED_ENGINES = ("icarus", "verilator")
DEFAULT_ENGINE = "icarus"

#: Only Verilator has a coverage path through this flow (spike section 1).
COVERAGE_ENGINES = ("verilator",)

#: Verilator build args for a coverage run, per the spike's section 5 recipe.
COVERAGE_BUILD_ARGS = ("--coverage", "--trace")

#: ``[unit, precision]``, passed to *both* build and test (see gotcha 3).
DEFAULT_TIMESCALE = ("1ns", "1ps")


#: ``iverilog`` flags an SDF-annotated build requires, all three verified
#: live (spike §3.1/§3.2/§2.1): without ``-gspecify`` Icarus omits every
#: specify block *and* the ``$sdf_annotate`` call itself, then runs at zero
#: delay and exits ``0``; without ``-ginterconnect`` every ``INTERCONNECT``
#: entry -- which is exactly what a post-route SDF carries -- fails at run
#: time; and ``-s <module>`` is what makes the generated shim a second
#: elaboration root rather than dead code.
SDF_BUILD_ARGS = ("-gspecify", "-ginterconnect", "-s", SDF_ANNOTATE_MODULE)


#: The ``environment.sdf.dropped`` class counting the zero-delay
#: ``INTERCONNECT`` entries removed before annotation because they run from
#: a bit-selected top-level input port onto a pin of a *physical-only*
#: instance -- a router-inserted antenna diode (or any filler/tapcell whose
#: model is timing-empty), whose Verilog has no ``specify`` block or output
#: pin. Icarus can annotate only the last-declared load on the port bit's net
#: (#5705). Zero delay at every corner makes the drop timing-neutral even
#: when the physical-only load is the survivor. Issue #2363's exemption
#: counts the entries removed from the SDF text up front
#: (:func:`_drop_sdf_zero_delay_physical_only_interconnects`) separately from
#: :data:`SDF_ALIAS_PORT_DROPPED_CLASS`.
SDF_PHYSICAL_ONLY_DROPPED_CLASS = "zero_delay_physical_only_interconnect"

#: The ``environment.sdf.dropped`` class counting the non-``INTERCONNECT``
#: delay entries -- on a real ``write_sdf`` output, the ``IOPATH`` population
#: -- removed before annotation because the request selected
#: ``options.sdf.entries: "interconnect"`` (issue #2364's supported, counted
#: exemption). Unlike the two zero-delay classes above this drop is
#: *requested* rather than derived from an Icarus limitation: the caller
#: asked for net-delay back-annotation only, so the count tells a consumer
#: how many module-path delay entries that choice set aside. Like them it is
#: not derived from a diagnostic substring -- the entries are dropped from
#: the SDF text up front
#: (:func:`_drop_sdf_non_interconnect_entries`), so Icarus never emits a
#: diagnostic for them at all. Only present on a run that selected the mode
#: *and* whose SDF carried at least one non-``INTERCONNECT`` delay entry.
SDF_IOPATH_DROPPED_CLASS = "iopath_interconnect_only"

#: The ``environment.sdf.dropped`` class counting the ``IOPATH`` entries
#: removed before annotation because their arc's only module paths in the
#: cell's own Verilog model are ``ifnone`` + edge-sensitive -- the gf180mcu
#: models' XOR/XNOR/MUX-select shape, ``ifnone (posedge A1 => (Z:A1))``.
#: Icarus discards every such path at elaboration (``sorry: ifnone with an
#: edge-sensitive path is not supported``), so the arc has no modpath an
#: ``IOPATH`` could match and each entry would otherwise fail with ``Unable
#: to match ModPath`` (issue #2885). Like the zero-delay classes it is decided
#: from the SDF text and the run's own sources up front
#: (:func:`_drop_sdf_unsupported_edge_modpath_iopaths`), so Icarus never
#: emits a diagnostic for these entries. Unlike them, the caller does lose
#: real timing -- the arc simulates with no module-path delay -- so this
#: class alone also carries an ``arcs`` list naming every dropped
#: ``{celltype, from, to}`` arc.
SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS = "iopath_ifnone_edge_modpath"

#: The ``environment.sdf.dropped`` class counting the bit-selected ``IOPATH``
#: entries removed before annotation because the cell's Verilog model reaches
#: that bus output only through a whole-vector module path (a hard macro's
#: ``(A_CLK *> A_DOUT)``), which registers no per-bit modpath for OpenSTA's
#: per-bit ``IOPATH A_CLK A_DOUT[0]`` entries to match (issue #2958). Like
#: :data:`SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS` it carries an ``arcs`` list
#: naming every dropped ``{celltype, from, to}`` arc, since those arcs simulate
#: without the SDF's delay.
SDF_BUS_BIT_MODPATH_DROPPED_CLASS = "iopath_bus_bit_modpath"

#: A human-readable reason per dropped class, surfaced in
#: ``environment.sdf.dropped`` (issue #1102) so a caller can explain *why* a
#: class was dropped without re-deriving it from this module's own comments.
#: Keyed by the lowercased :data:`SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS` member
#: whose diagnostics were filtered out, plus every ``SDF_*_DROPPED_CLASS``
#: constant (a class handled in the SDF text, not the transcript); the class
#: table in docs/cli/functional-verification.md lists them all.
SDF_BENIGN_DIAGNOSTIC_REASONS: dict[str, str] = {
    "timingcheck": (
        "Icarus Verilog implements SDF delay annotation (IOPATH/INTERCONNECT) "
        "but not SDF TIMINGCHECK -- every TIMINGCHECK section in the SDF is "
        "dropped, so $setup/$hold/$width checks run against the cell "
        "library's own placeholder timing, not the characterised limits in "
        "the SDF"
    ),
    SDF_ALIAS_PORT_DROPPED_CLASS: (
        "Icarus Verilog cannot insert an intermodpath onto a top-level port "
        "bit the gate-level netlist drives through a Verilog 'assign' alias "
        "(the shape a P&R backend produces for a constant/tie-cell-driven "
        "output: 'assign uio_oe[0] = net0;'), so every INTERCONNECT entry "
        "with such a destination fails with 'Could not find intermodpath!'. "
        "Entries whose every min:typ:max delay value is zero model no delay "
        "at any corner, so they are removed before annotation and counted "
        "here instead of failing the run -- a non-zero-delay entry on the "
        "same destination is left in place and still fails loudly"
    ),
    SDF_PHYSICAL_ONLY_DROPPED_CLASS: (
        "Icarus Verilog can insert an intermodpath only for the last-declared "
        "load on a bit-selected top-level input port net. INTERCONNECT entries "
        "for other loads fail with 'Could not find intermodpath!'. A physical-only "
        "destination (such as a router-inserted antenna diode or a timing-empty "
        "filler/tapcell) has no specify block or output pin and can be the "
        "surviving load. Its entries are removed before annotation only when "
        "every min:typ:max delay value is zero, which preserves simulated "
        "timing at every corner. Non-zero entries remain subject to the "
        "annotation diagnostic gate"
    ),
    SDF_BUS_PORT_SOURCE_DROPPED_CLASS: (
        "Icarus Verilog can fail to insert an intermodpath for an "
        "INTERCONNECT entry whose source is a bit-selected top-level input "
        "port -- such an entry fails with 'Could not find intermodpath!'. "
        "Entries whose every min:typ:max delay value is zero model no delay "
        "at any corner, so they are removed before annotation and counted "
        "here instead of failing the run -- a non-zero-delay entry on the "
        "same source is left in place and still fails loudly"
    ),
    SDF_IOPATH_DROPPED_CLASS: (
        "The request selected options.sdf.entries: 'interconnect' "
        "(net-delay back-annotation only), so every non-INTERCONNECT delay "
        "entry -- on a real write_sdf output, the IOPATH module-path "
        "delays -- was removed from the SDF text before annotation and "
        "counted here. This drop is requested, not derived from an Icarus "
        "limitation: on a timing-clean design (positive setup/hold slack at "
        "the SDF's own corner) IOPATH annotation on the sky130 "
        "specify-branch models kills the design in Icarus even at a "
        "10x-relaxed clock period, while INTERCONNECT-only annotation "
        "simulates correctly (issue #2364) -- this mode makes that "
        "configuration first-class and honestly reported instead of "
        "hand-filtered. A run that did not select the mode never carries "
        "this class"
    ),
    SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS: (
        "The cell's own Verilog model declares this arc only through "
        "'ifnone' edge-sensitive module paths (e.g. 'ifnone (posedge A1 => "
        "(Z:A1))', the shape gf180mcu models give XOR/XNOR inputs and MUX "
        "selects), which Icarus Verilog does not support: it discards every "
        "such path at elaboration ('sorry: ifnone with an edge-sensitive path "
        "is not supported'), so no IOPATH entry -- unqualified or "
        "edge-qualified -- can match it ('Unable to match ModPath'). Those "
        "IOPATH entries were removed before annotation and counted here, and "
        "'arcs' names every affected cell arc. Removing them cannot change "
        "simulated timing relative to annotating them, but the listed arcs "
        "simulate with NO module-path delay at all (neither the SDF's nor "
        "the model's placeholder): their characterised cell delay is not "
        "represented in this run. An IOPATH that fails to match any other "
        "arc is left in place and still fails loudly"
    ),
    SDF_BUS_BIT_MODPATH_DROPPED_CLASS: (
        "The cell's own Verilog model declares this output as a bus and "
        "reaches it only through a whole-vector module path (e.g. '(A_CLK *> "
        "A_DOUT)', a hard macro's shape), which registers no per-bit modpath "
        "in Icarus Verilog: a per-bit 'IOPATH A_CLK A_DOUT[0]' entry (OpenSTA's "
        "write_sdf output) is a syntax error unescaped and 'Unable to match "
        "ModPath' escaped, so it can never annotate. Those IOPATH entries "
        "were removed before annotation and counted here, and 'arcs' names "
        "every affected cell arc (the bus, bit-select dropped). The listed "
        "arcs simulate with the model's own module-path delay, not the SDF's "
        "per-bit delay: their characterised cell delay is not represented in "
        "this run. An IOPATH onto a bus with a bit-indexed model path, a "
        "scalar port, or a cell with no model is left in place and still "
        "fails loudly"
    ),
    **SDF_IOPATH_SYNTAX_REASONS,
    **SDF_INTERNAL_BUS_NET_REASONS,
}


#: Transcript markers of the Python cocotb *loaded* inside the simulator
#: disagreeing with its compiled VPI module (issue #1103: a ``cp312`` wheel in
#: a CPython 3.14 simulator subprocess). The second is only the embed
#: function's name in older cocotb log lines, so neither is proof alone. The
#: pre-flights (wheel-tag check, Icarus runtime probe) run first; these cover
#: what they leave open, scanned once ``results.xml`` is confirmed missing.
INTERPRETER_MISMATCH_MARKERS = (
    "Unexpected sys.executable value",
    "_embed_init_python",
)

#: `mutation_testing`'s own nested schema version (spike §3c: "versioned per
#: *feature*", independent of the outer envelope's `SCHEMA_VERSION` -- the
#: same posture `klt extract`'s `parasitics` sub-block takes).
MUTATION_SCHEMA_VERSION = 1

#: Wall-clock bound on one isolated per-mutant build+test, enforced by this
#: module itself (issue #1592, spike "Open questions") -- independent of, and
#: in addition to, whatever cycle bound the testbench's own code happens to
#: have. cocotb 2.0.1's own `Runner.test()` has no `timeout` parameter at all
#: (verified against `cocotb_tools/runner.py`: every simulator invocation is
#: a bare `subprocess.run(cmd, cwd=cwd, env=self.env, check=True, ...)`, no
#: timeout kwarg anywhere in the call chain) -- so an FSM-wedging mutation
#: paired with a testbench that `await`s an event with no cycle bound at all
#: would otherwise hang this process forever. 600s is generous for this
#: flow's own documented per-run costs (~1-2s Icarus, ~8s Verilator on the
#: worked examples) while still bounding the worst case.
DEFAULT_MUTATION_TIMEOUT_S = 600.0

#: How long to wait, after sending SIGKILL to a timed-out variant's process
#: group, for the OS to actually reap it before giving up and moving on --
#: never itself a source of an unbounded wait.
_MUTATION_KILL_GRACE_S = 5.0

_COVERAGE_SUMMARY_RE = re.compile(
    r"^\s*(line|toggle|branch|expr)\s*:\s*([0-9.]+)%", re.MULTILINE
)


_rpath = RequestPaths(FunctionalVerificationError)


# ---------------------------------------------------------------------------
# request loading (same file / "-" / inline-JSON convention as `klt lvs`)
# ---------------------------------------------------------------------------


# "sources" is checked in _resolve_sources: options.from_synthesize may omit it.
_REQUIRED_REQUEST_FIELDS = ("hdl_toplevel", "testbench")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared shape check for a JSON-decoded request, however sourced;
    ``source`` is folded into the "must be a JSON object" error."""
    doc = _shared_validate_request_shape(
        data,
        source,
        error_cls=FunctionalVerificationError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )
    return _require_sources(doc)


def _require_sources(doc: dict[str, Any]) -> dict[str, Any]:
    if "sources" not in doc and not has_from_synthesize(doc.get("options")):
        raise FunctionalVerificationError("request is missing required field: sources")
    return doc


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt functional-verification`` request
    JSON file.

    Raises :class:`FunctionalVerificationError` if the file is missing/
    unreadable, not valid JSON, or missing a required top-level field
    (``sources``, ``hdl_toplevel``, ``testbench``). Does not require a
    ``schema`` field (user-authored input, never emitted by this tool).
    """
    request = _load_request_json(request_path, FunctionalVerificationError)
    return _validate_request_shape(request, "request file")


def load_request_arg(
    value: str, base_dir: str | None = None
) -> tuple[dict[str, Any], str]:
    """Resolve the CLI ``request`` argument into a request dict plus the
    directory relative paths inside it should resolve against -- the same
    three forms ``klt lvs``'s own ``request`` argument accepts, so this
    verb composes into ``klt eval``'s descriptor unchanged. ``base_dir``
    optionally overrides that anchor so a request archived beside an
    evidence record re-runs from the directory its paths were written
    against (issue #5673); it must exist and be a directory.
    """
    doc, request_dir = _shared_load_request_arg(
        value,
        error_cls=FunctionalVerificationError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
        base_dir=base_dir,
    )
    return _require_sources(doc), request_dir


# ---------------------------------------------------------------------------
# request field validation
# ---------------------------------------------------------------------------


def _resolve_testbench(
    testbench: Any, request_dir: str, *, require_module: bool = True
) -> tuple[str, list[str], str | list[str] | None]:
    """Resolve a cocotb module name, ordered search directories, and selected testcases.

    A run needs the module in a directory on ``testbench.search_path`` (defaulting
    to the request directory). An offline check only validates the declaration.
    During execution this check catches a missing module before cocotb imports
    it inside the simulator. In that case cocotb can exit zero and write no
    ``results.xml``, leaving no test results to grade.
    """
    if not isinstance(testbench, dict):
        raise FunctionalVerificationError("request.testbench must be a JSON object")

    module = testbench.get("module")
    if not isinstance(module, str) or not module:
        raise FunctionalVerificationError(
            "request.testbench.module must be a non-empty string"
        )
    if module.endswith(".py") or os.sep in module or "/" in module:
        raise FunctionalVerificationError(
            f"request.testbench.module must be a Python module name, not a "
            f"path: {module!r}"
        )

    module_dirs = resolve_testbench_search_dirs(
        testbench.get("search_path"), request_dir, module, require_module=require_module
    )

    testcase = testbench.get("testcase")
    if testcase is not None:
        if isinstance(testcase, str):
            if not testcase:
                raise FunctionalVerificationError(
                    "request.testbench.testcase must be a non-empty string when given"
                )
        elif isinstance(testcase, list):
            if not testcase or not all(
                isinstance(entry, str) and entry for entry in testcase
            ):
                raise FunctionalVerificationError(
                    "request.testbench.testcase array entries must be non-empty strings"
                )
        else:
            raise FunctionalVerificationError(
                "request.testbench.testcase must be a string, an array of "
                "strings, or null"
            )

    return module, module_dirs, testcase


def _resolve_options(
    options: Any, engine: str, request_dir: str, *, require_include_dirs: bool = True
) -> tuple[
    bool,
    tuple[str, str],
    int | None,
    dict[str, str | None],
    list[str],
    list[str],
    dict[str, str] | None,
    bool,
]:
    """Validate ``request.options`` and return
    ``(coverage, timescale, random_seed, defines, build_args, includes, sdf,
    trace)``.

    Enforces the spike's hard constraint that coverage is a Verilator-only
    capability -- ``options.coverage: true`` with ``engine: "icarus"`` is a
    request error (exit 1), never a silently-ignored flag -- and the exactly
    symmetric constraint for SDF back-annotation, which only Icarus has a
    path for (``options.sdf`` + ``engine: "verilator"`` is a request error,
    never a silent no-op).

    ``sdf`` is ``None`` when the block is absent, else
    ``{"file": <abs path>, "corner": "min"|"typ"|"max"}``. ``file`` is
    resolved relative to ``request_dir`` like every other path field, and is
    returned **absolute** because the generated ``$sdf_annotate`` shim
    embeds it verbatim and ``vvp`` resolves a relative path against its own
    working directory rather than the request's (spike §4.2).

    ``trace`` (Epic #1585 Phase 3, issue #1845) is a plain boolean, unlike
    ``coverage``/``sdf``: cocotb's own ``Runner.build()``/``Runner.test()``
    already expose a single ``waves`` switch that both Icarus and Verilator
    understand, each writing its own engine-native format (Icarus: FST;
    Verilator: VCD) to a location this module does not control -- there is
    no per-request knob to validate beyond "on or off". Unlike ``coverage``
    and ``sdf``, ``options.trace`` is not engine-restricted: both supported
    engines have a waveform-dump path through cocotb's ``Runner``.
    """
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise FunctionalVerificationError("request.options must be a JSON object")

    coverage = options.get("coverage", False)
    if not isinstance(coverage, bool):
        raise FunctionalVerificationError("request.options.coverage must be a boolean")
    if coverage and engine not in COVERAGE_ENGINES:
        raise FunctionalVerificationError(
            f"options.coverage requires engine 'verilator' -- engine "
            f"'{engine}' has no coverage path"
        )

    trace = options.get("trace", False)
    if not isinstance(trace, bool):
        raise FunctionalVerificationError("request.options.trace must be a boolean")

    timescale = options.get("timescale")
    if timescale is None:
        resolved_timescale = DEFAULT_TIMESCALE
    elif (
        not isinstance(timescale, (list, tuple))
        or len(timescale) != 2
        or not all(isinstance(entry, str) and entry for entry in timescale)
    ):
        raise FunctionalVerificationError(
            "request.options.timescale must be a [unit, precision] pair of "
            'non-empty strings, e.g. ["1ns", "1ps"]'
        )
    else:
        resolved_timescale = (timescale[0], timescale[1])

    random_seed = options.get("random_seed")
    if random_seed is not None and (not is_int(random_seed)):
        raise FunctionalVerificationError(
            "request.options.random_seed must be an integer when given"
        )

    defines = _resolve_defines(options.get("defines"))
    build_args = _resolve_build_args(options.get("build_args"))
    includes = _resolve_includes(
        options.get("includes"), request_dir, require_dirs=require_include_dirs
    )
    sdf = _resolve_sdf_option(options.get("sdf"), engine, request_dir)
    _reject_sdf_with_functional_models(sdf, defines)

    return (
        coverage,
        resolved_timescale,
        random_seed,
        defines,
        build_args,
        includes,
        sdf,
        trace,
    )


INITIAL_STATE_MODES = ("x", "random")


def _resolve_initial_state_seeds(mode: str, seeds: Any) -> list[int]:
    """``initial_state.seeds``: absent for ``x``, a non-empty duplicate-free
    integer array for ``random``."""
    if mode == "x":
        if seeds is not None:
            raise FunctionalVerificationError(
                "request.options.initial_state.seeds is only valid with mode 'random'"
            )
        return []
    if not isinstance(seeds, list) or not seeds or any(not is_int(s) for s in seeds):
        raise FunctionalVerificationError(
            "request.options.initial_state.seeds must be a non-empty "
            "array of integers when mode is 'random'"
        )
    if len(set(seeds)) != len(seeds):
        raise FunctionalVerificationError(
            "request.options.initial_state.seeds must not repeat a seed"
        )
    return list(seeds)


def _resolve_initial_state(
    options: Any, engine: str, request_dir: str, *, require_netlist: bool = True
) -> dict[str, Any] | None:
    """Validate ``request.options.initial_state`` (issue #2998); ``None`` when
    the key is absent. Shape: ``{"mode": "x" | "random", "seeds": [int, ...],
    "netlist": "<gate-level Verilog netlist>", "clock": "<net>"}``.

    ``clock`` (optional) names the net of ``hdl_toplevel`` whose first full
    cycle captures the forced values; when absent it is the one net every
    state element's clock/enable pin connects to, and a netlist with several
    (or none) is a request error asking for it.
    ``netlist`` (resolved against ``request_dir``) is the flat gate-level
    netlist of ``hdl_toplevel`` the state elements are derived from;
    ``request.sources`` supply the PDK cell models. ``seeds`` is required and
    non-empty for ``random`` (one regression runs per seed) and rejected for
    ``x`` (a single run). Icarus only: the drive relies on hierarchical
    ``force`` of cell-model pins, which Verilator does not model -- a request
    error, never a silently-ignored option, like ``options.sdf``.
    """
    if not isinstance(options, dict) or "initial_state" not in options:
        return None
    raw = options["initial_state"]
    if not isinstance(raw, dict):
        raise FunctionalVerificationError(
            "request.options.initial_state must be a JSON object"
        )
    unknown = sorted(set(raw) - {"mode", "seeds", "netlist", "clock"})
    if unknown:
        raise FunctionalVerificationError(
            f"request.options.initial_state has unknown key(s): {', '.join(unknown)}"
        )
    mode = raw.get("mode")
    if mode not in INITIAL_STATE_MODES:
        raise FunctionalVerificationError(
            "request.options.initial_state.mode must be one of "
            f"{', '.join(repr(m) for m in INITIAL_STATE_MODES)}"
        )
    seeds = _resolve_initial_state_seeds(mode, raw.get("seeds"))
    netlist = raw.get("netlist")
    if not isinstance(netlist, str) or not netlist:
        raise FunctionalVerificationError(
            "request.options.initial_state.netlist must be a non-empty path "
            "to the gate-level Verilog netlist"
        )
    clock = raw.get("clock")
    if clock is not None and (not isinstance(clock, str) or not clock):
        raise FunctionalVerificationError(
            "request.options.initial_state.clock must be a non-empty net name"
        )
    if engine != "icarus":
        raise FunctionalVerificationError(
            f"options.initial_state requires engine 'icarus' -- engine "
            f"'{engine}' cannot force cell-model pins hierarchically"
        )
    netlist_path = (
        _rpath(netlist, request_dir, "request.options.initial_state.netlist")
        if require_netlist
        else netlist
    )
    if require_netlist and not os.path.isfile(netlist_path):
        raise FunctionalVerificationError(
            f"request.options.initial_state.netlist '{netlist_path}' not found"
        )
    return {
        "mode": mode,
        "seeds": seeds,
        "netlist": os.path.abspath(netlist_path),
        "clock": clock,
    }


class _Report(dict):
    """A report ``dict`` that also carries the resolved absolute artifact
    paths (``local_paths``) for ``--format text``. The attribute is never
    serialised: the JSON contract only ever sees the repo-relative
    ``{path, scope}`` envelope (docs/json-contract.md, #2224)."""

    local_paths: dict[str, str | None]


def _initial_state_run_specs(
    initial_state: dict[str, Any] | None,
    plusargs: list[str],
    *,
    engine: str,
    output_dir: str,
    results_xml: str,
    test_log: str,
) -> list[dict[str, Any]]:
    """One regression normally; one per seed for ``initial_state.mode:
    "random"``. Per-spec ``plusargs``: user entries, then ``+klt_init_seed``."""
    if initial_state is None or initial_state["mode"] != "random":
        single = dict(
            seed=None, results_xml=results_xml, test_log=test_log, plusargs=plusargs
        )
        return [single]
    return [
        {
            "seed": seed,
            "results_xml": os.path.join(
                output_dir, f"results_{engine}_init_seed{seed}.xml"
            ),
            "test_log": os.path.join(output_dir, f"test_{engine}_init_seed{seed}.log"),
            "plusargs": [*plusargs, f"+{SEED_PLUSARG}={seed}"],
        }
        for seed in initial_state["seeds"]
    ]


def _parse_run_results(
    spec: dict[str, Any], *, engine: str, build_log: str
) -> list[dict[str, Any]]:
    """``parse_results_xml`` for one run, with the ABI-mismatch augmentation
    (issue #1103) on a missing results file."""
    spec_xml = spec["results_xml"]
    try:
        return parse_results_xml(spec_xml)
    except FunctionalVerificationError as exc:
        # Fallback for what the pre-flights (wheel-tag check, Icarus runtime
        # probe) leave open; only when `results.xml` is confirmed missing, so
        # a genuinely unparseable results file keeps its own message.
        diagnostic = None
        if not os.path.isfile(spec_xml):
            diagnostic = _scan_interpreter_mismatch_diagnostics(
                build_log, spec["test_log"]
            )
        if diagnostic is None:
            raise
        raise FunctionalVerificationError(
            f"{exc} -- {_interpreter_mismatch_hint(engine, diagnostic)}"
        ) from exc


def _run_spec_test(engine_runner, spec: dict[str, Any], **kwargs: Any) -> None:
    """One :func:`_run_test` for a run spec, after clearing its stale results
    file (a leftover must never be mistaken for this run's evidence)."""
    try:
        os.remove(spec["results_xml"])
    except OSError:
        pass
    _run_test(
        engine_runner,
        results_xml=spec["results_xml"],
        log_path=spec["test_log"],
        plusargs=spec["plusargs"],
        **kwargs,
    )


def _initial_state_echo(
    initial_state: dict[str, Any] | None,
    info: dict[str, Any],
    runs: list[dict[str, Any]],
    repo_root: RepoRoot,
) -> dict[str, Any] | None:
    """``environment.initial_state`` (additive, issue #2998): ``None`` unless
    requested; ``seeds`` the effective seeds (empty for "x"), ``runs`` per seed."""
    if initial_state is None:
        return None
    return {
        "mode": initial_state["mode"],
        "seeds": initial_state["seeds"],
        "netlist": _hashed_path_envelope(initial_state["netlist"], repo_root=repo_root),
        "clock": info["clock"],
        "state_elements": info["state_elements"],
        "d_nets": info["d_nets"],
        "runs": runs,
    }


def _reject_initial_state_with_mutations(
    options: Any, engine: str, request_dir: str
) -> None:
    """The baseline would be driven from a chosen power-up state while each
    isolated per-mutant build (which does not replicate the generated shim)
    would not, so a killed/survived verdict would compare unlike runs."""
    if _resolve_initial_state(options, engine, request_dir) is not None:
        raise FunctionalVerificationError(
            "options.initial_state cannot currently be combined with "
            "--mutations -- not yet designed"
        )


def _choose_initial_state_clock(
    elements: list[dict[str, Any]], requested: str | None
) -> str:
    """The net whose first full cycle captures the forced values: the
    request's ``clock``, else the one net every state element's clock/enable
    pin connects to."""
    if requested is not None:
        return requested
    nets = {e["capture_net"] for e in elements}
    if len(nets) == 1 and isinstance(next(iter(nets)), str):
        return next(iter(nets))
    raise FunctionalVerificationError(
        "options.initial_state: the state elements' clock pins connect to "
        f"{len(nets)} different nets -- name the capture clock net in "
        "options.initial_state.clock"
    )


def _write_initial_state_shim(
    initial_state: dict[str, Any],
    *,
    sources: list[str],
    hdl_toplevel: str,
    output_dir: str,
    scope: str,
    renamed: list[dict[str, str]],
) -> tuple[str, dict[str, Any]]:
    """Find flops in the original netlist and drive their elaborated names."""
    elements, unsupported = sequential_elements(
        initial_state["netlist"],
        sources,
        hdl_toplevel,
        error_cls=lambda m: FunctionalVerificationError(f"options.initial_state: {m}"),
    )
    if unsupported:
        shown = ", ".join(
            f"{u['instance']} ({u['cell']}: {u['reason']})" for u in unsupported[:5]
        )
        raise FunctionalVerificationError(
            f"options.initial_state cannot drive {len(unsupported)} state "
            f"element(s): {shown}"
        )
    if not elements:
        raise FunctionalVerificationError(
            f"options.initial_state: netlist module '{hdl_toplevel}' has no "
            "sequential cell (per request.sources' cell models)"
        )
    clock = _choose_initial_state_clock(elements, initial_state["clock"])
    aliases = {entry["from"]: entry["to"] for entry in renamed}
    if aliases:
        for element in elements:
            instance = element["instance"]
            element["instance"] = aliases.get(instance, instance)
    path = os.path.join(output_dir, f"{INITIAL_STATE_MODULE}.v")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            build_initial_state_verilog(
                mode=initial_state["mode"],
                elements=elements,
                scope=scope,
                clock=aliases.get(clock, clock),
            )
        )
    info = {
        "state_elements": len(elements),
        "d_nets": count_d_nets(elements),
        "clock": clock,
    }
    return path, info


def _resolve_source_defines(
    options: Any, sources: list[str], request_dir: str
) -> dict[str, dict[str, str | None]]:
    """Validate ``request.options.source_defines`` (issue #3290) and return it
    keyed by the **resolved absolute path** of the ``request.sources`` entry
    it names (``{}`` when omitted).

    Each key must name an entry of ``request.sources`` (matched after the same
    path resolution :func:`_resolve_sources` applies); each value is validated
    like ``options.defines``. The defines apply to that one source only, via a
    generated wrapper (:func:`_write_scoped_define_wrappers`), so a vendor
    hard-macro model can take ``FUNCTIONAL`` while the PDK std-cell sources --
    and the ``options.sdf`` + global ``FUNCTIONAL`` rejection that protects
    them -- are untouched.
    """
    raw = (options or {}).get("source_defines") if isinstance(options, dict) else None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FunctionalVerificationError(
            "request.options.source_defines must be a JSON object of "
            "source path -> defines object"
        )
    resolved: dict[str, dict[str, str | None]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise FunctionalVerificationError(
                "request.options.source_defines keys must be non-empty strings"
            )
        path = os.path.abspath(
            _rpath(key, request_dir, "request.options.source_defines key")
        )
        if path not in sources:
            raise FunctionalVerificationError(
                f"request.options.source_defines names '{key}', which is not "
                f"an entry of request.sources"
            )
        if not isinstance(value, dict):
            raise FunctionalVerificationError(
                f"request.options.source_defines[{key!r}] must be a JSON "
                f"object of string -> string|null"
            )
        for name, define_value in value.items():
            if not isinstance(name, str) or not name:
                raise FunctionalVerificationError(
                    f"request.options.source_defines[{key!r}] keys must be "
                    f"non-empty strings"
                )
            if define_value is not None and not isinstance(define_value, str):
                raise FunctionalVerificationError(
                    f"request.options.source_defines[{key!r}][{name!r}] must "
                    f"be a string or null -- got a {type(define_value).__name__}"
                )
        resolved[path] = value
    return resolved


def _write_scoped_define_wrappers(
    sources: list[str],
    source_defines: dict[str, dict[str, str | None]],
    wrapper_dir: str,
) -> tuple[list[str], list[str]]:
    """Substitute a generated one-file wrapper for each source named in
    ``source_defines``, **in the same list position** (issue #3290).

    The wrapper is ``` `define K [V] ``` / ``` `include "<abs path>" ``` /
    ``` `undef K ```, so the defines are visible to that one file and cannot
    leak into later sources. Returns ``(build_sources, include_dirs)``;
    ``include_dirs`` holds the directory of each wrapped source so a nested
    relative `` `include `` inside the vendor model still resolves. Sources not
    named are returned untouched (byte-identical build to a run without
    ``source_defines``).
    """
    if not source_defines:
        return list(sources), []
    try:
        os.makedirs(wrapper_dir, exist_ok=True)
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not create output directory '{wrapper_dir}': {exc}"
        ) from exc
    build_sources: list[str] = []
    include_dirs: list[str] = []
    for index, source in enumerate(sources):
        defines = source_defines.get(source)
        if defines is None:
            build_sources.append(source)
            continue
        lines = [
            f"`define {name}" if value is None else f"`define {name} {value}"
            for name, value in defines.items()
        ]
        lines.append(f'`include "{source}"')
        lines.extend(f"`undef {name}" for name in defines)
        wrapper_path = os.path.join(
            wrapper_dir, f"scoped_defines_{index}_{os.path.basename(source)}"
        )
        try:
            with open(wrapper_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as exc:
            raise FunctionalVerificationError(
                f"could not write scoped-defines wrapper '{wrapper_path}': {exc}"
            ) from exc
        build_sources.append(wrapper_path)
        source_dir = os.path.dirname(source)
        if source_dir not in include_dirs:
            include_dirs.append(source_dir)
    return build_sources, include_dirs


def _resolve_build_args(build_args: Any) -> list[str]:
    """Validate the optional ``request.options.build_args`` array and return
    it unchanged (or ``[]`` when omitted).

    Composed with -- never replacing -- the fixed :data:`COVERAGE_BUILD_ARGS`
    list when ``options.coverage: true``: the caller appends these *after*
    the coverage args, so a user-supplied flag can still override a coverage
    default if the two conflict (see :func:`run_functional_verification`).
    """
    if build_args is None:
        return []
    if not isinstance(build_args, list) or not all(
        isinstance(entry, str) and entry for entry in build_args
    ):
        raise FunctionalVerificationError(
            "request.options.build_args must be an array of non-empty strings"
        )
    return list(build_args)


def _resolve_includes(
    includes: Any, request_dir: str, *, require_dirs: bool = True
) -> list[str]:
    """Validate the optional ``request.options.includes`` array and resolve
    each entry to an absolute directory path (relative to ``request_dir`` --
    the same convention :func:`_resolve_sources`/:func:`_resolve_testbench`
    already use), or ``[]`` when omitted.

    Forwarded to ``Runner.build(includes=...)`` for a cell library split
    across multiple files with `` `include `` directives.
    """
    if includes is None:
        return []
    if not isinstance(includes, list) or not all(
        isinstance(entry, str) and entry for entry in includes
    ):
        raise FunctionalVerificationError(
            "request.options.includes must be an array of non-empty strings"
        )

    if not require_dirs:
        return includes
    resolved: list[str] = []
    for entry in includes:
        path = _rpath(entry, request_dir, "request.options.includes entry")
        if not os.path.isdir(path):
            raise FunctionalVerificationError(f"include directory not found: {entry}")
        resolved.append(os.path.abspath(path))
    return resolved


def _scan_interpreter_mismatch_diagnostics(*log_paths: str) -> str | None:
    """The first line in ``log_paths`` naming cocotb's ABI-mismatch failure
    mode (issue #1103), or ``None``.

    Fallback for whatever the pre-flights (the ``WHEEL``-tag check, the Icarus
    runtime probe) don't settle. This diagnostic-only fallback never raises: a
    missing/unreadable transcript contributes nothing, the same posture
    :func:`_log_tail` takes; the SDF annotation gate needs readable
    transcripts (issue #2130)."""
    for log_path in log_paths:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if any(marker in stripped for marker in INTERPRETER_MISMATCH_MARKERS):
                return stripped
    return None


def _resolve_parameters(parameters: Any) -> dict[str, Any]:
    """Validate the optional ``request.parameters`` object and return it
    unchanged (or ``{}`` when omitted).

    Forwarded verbatim to both ``Runner.build(parameters=...)`` and
    ``Runner.test(parameters=...)`` -- cocotb's own per-simulator backends
    already translate the mapping into the elaboration-time override syntax
    (Icarus: ``-P<toplevel>.<name>=<value>``; Verilator: ``-G<name>=<value>``),
    so this module never needs to know that syntax itself.
    """
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise FunctionalVerificationError(
            "request.parameters must be a JSON object (string key -> scalar value)"
        )

    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise FunctionalVerificationError(
                "request.parameters keys must be non-empty strings"
            )
        if not isinstance(value, (bool, int, float, str)):
            raise FunctionalVerificationError(
                f"request.parameters[{key!r}] must be a scalar (integer, "
                "float, string, or boolean) -- got a "
                f"{type(value).__name__}"
            )

    return parameters


def _resolve_env(env: Any, *, verilog_top: bool = False) -> dict[str, str]:
    """Validate the optional ``request.env`` object and return it unchanged
    (or ``{}`` when omitted). ``verilog_top`` rejects a non-empty env: the
    Verilog-bench path has no cocotb test process to apply it to.

    Forwarded to ``Runner.test(extra_env=...)`` so the cocotb test process
    sees each entry in ``os.environ`` (issue #4338). Echoed verbatim as
    ``environment.env``, so it must never carry credentials.
    """
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise FunctionalVerificationError(
            "request.env must be a JSON object (string key -> string value)"
        )

    for key, value in env.items():
        if not isinstance(key, str) or not key or "=" in key:
            raise FunctionalVerificationError(
                "request.env keys must be non-empty strings without '='"
            )
        if not isinstance(value, str):
            raise FunctionalVerificationError(
                f"request.env[{key!r}] must be a string -- got a {type(value).__name__}"
            )

    _reject_env_pythonpath(env)

    if verilog_top and env:
        raise FunctionalVerificationError(
            "request.env cannot be combined with testbench.verilog_top -- "
            "the Verilog-bench path has no cocotb test process to apply it to"
        )
    return env


_REQUIRES_TOOLS_KEYS = frozenset({"name", "version", "sha256"})


def _resolve_requires_tools(
    requires_tools: Any, env: dict[str, str]
) -> list[dict[str, Any]]:
    """Validate the optional ``request.requires_tools`` array and resolve each
    tool before any build starts (issue #4473).

    Each entry is ``{"name": str, "version": str|null, "sha256": str|null}``.
    ``name`` is looked up with ``shutil.which`` on ``request.env["PATH"]`` when
    that is set (else the ambient ``PATH``); ``version`` is a substring match on
    the first non-empty line from the probe (``-V`` for Icarus, otherwise
    ``--version``; issue #7094); ``sha256`` is
    compared with the resolved binary's digest. Every unmet tool is listed in
    one ``missing_tool`` error. Returns the ``environment.tools`` echo (``[]``
    when omitted).
    """
    entries = _requires_tool_entries(requires_tools)
    search_path = env.get("PATH")
    run_env = {**os.environ, **env}
    tools: list[dict[str, Any]] = []
    unmet: list[dict[str, Any]] = []
    for name, want_version, want_sha256 in entries:
        tool, shortfall = _check_required_tool(
            name, want_version, want_sha256, search_path, run_env
        )
        if shortfall is not None:
            unmet.append(shortfall)
        if tool is not None:
            tools.append(tool)

    if unmet:
        raise _MissingToolError(unmet)
    return tools


def _requires_tool_entries(
    requires_tools: Any,
) -> list[tuple[str, str | None, str | None]]:
    if requires_tools is None:
        return []
    if not isinstance(requires_tools, list):
        raise FunctionalVerificationError(
            "request.requires_tools must be a JSON array of "
            '{"name": ..., "version": ..., "sha256": ...} objects'
        )

    return [
        _parse_requires_tool_entry(index, entry)
        for index, entry in enumerate(requires_tools)
    ]


def _parse_requires_tool_entry(
    index: int, entry: Any
) -> tuple[str, str | None, str | None]:
    """Validate one ``request.requires_tools`` entry and return
    ``(name, version, sha256)``."""
    where = f"request.requires_tools[{index}]"
    if not isinstance(entry, dict):
        raise FunctionalVerificationError(f"{where} must be a JSON object")
    unknown = sorted(set(entry) - _REQUIRES_TOOLS_KEYS)
    if unknown:
        raise FunctionalVerificationError(
            f"{where} has unknown field(s) {unknown} (allowed: name, version, sha256)"
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise FunctionalVerificationError(
            f"{where}.name is required and must be a non-empty string"
        )
    for field in ("version", "sha256"):
        value = entry.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise FunctionalVerificationError(
                f"{where}.{field} must be a non-empty string or null"
            )
    return name, entry.get("version"), entry.get("sha256")


class _MissingToolError(FunctionalVerificationError):
    """``missing_tool``: one or more ``request.requires_tools`` unmet. The CLI
    surfaces ``details`` flat on ``error`` (``error.unmet_tools``)."""

    def __init__(self, unmet: list[dict[str, Any]]) -> None:
        listing = "; ".join(f"{u['name']} ({u['reason']})" for u in unmet)
        super().__init__(
            f"{len(unmet)} required tool(s) unmet before build: {listing}",
            code="missing_tool",
        )
        self.details = {"unmet_tools": unmet}


# ---------------------------------------------------------------------------
# engine invocation
# ---------------------------------------------------------------------------


def _import_runner():
    """Import ``cocotb_tools.runner`` lazily, turning a missing cocotb install
    into an actionable :class:`FunctionalVerificationError` rather than an
    ImportError traceback at ``klt`` startup."""
    try:
        from cocotb_tools import runner
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise FunctionalVerificationError(
            f"cocotb is not installed (import failed: {exc}; interpreter: "
            f"{sys.executable}) -- for a tool install (`uv tool install` / "
            "`pipx install` / `uvx --from`) reinstall with the extra on the "
            "install spec: `klayout-tools[functional-verification] @ "
            "<url-or-version>`; for a source checkout run `uv sync --extra "
            "functional-verification`; `pip install cocotb` only works if "
            "that `pip` belongs to the interpreter above",
            code="missing_optional_dependency",
        ) from exc
    return runner


def _check_cocotb_abi_compatibility() -> None:
    """Reject an installed cocotb wheel whose compiled ABI tag does not match
    the running interpreter (issue #1103).

    ``import cocotb_tools.runner`` (:func:`_import_runner`) succeeds even when
    cocotb was installed as e.g. a ``cp312`` wheel into a CPython 3.14
    environment -- pure Python still loads fine. The mismatch only manifests
    later, when cocotb's compiled VPI module is ``dlopen``'d *inside the
    simulator subprocess* and pulls in a ``libpython`` that does not match the
    interpreter actually running, which surfaces as the simulator exiting
    without ever writing ``results.xml`` -- four layers below anything this
    module otherwise sees. Reading the installed distribution's ``WHEEL``
    metadata (the same two-line check the issue's own diagnosis settled on)
    catches this before the runner is ever invoked.

    Never raises for a reason *other than* a confirmed mismatch: missing or
    malformed ``WHEEL`` metadata (e.g. a pure-Python wheel, an editable
    install, or a packaging layout this check doesn't recognise) degrades to
    "skip this check", the same discipline ``_engine_version()``/
    ``_cocotb_version()`` already follow -- an unreadable probe must never
    itself crash a run the runner might still serve.
    """
    try:
        wheel_text = importlib.metadata.distribution("cocotb").read_text("WHEEL")
    except Exception:  # pragma: no cover - defensive, any lookup failure
        return
    if not wheel_text:
        return
    tags = [
        line.split(":", 1)[1].strip()
        for line in wheel_text.splitlines()
        if line.startswith("Tag:")
    ]
    if not tags:
        return
    interpreter_tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    if any(interpreter_tag in tag for tag in tags):
        return

    try:
        installed_version = importlib.metadata.version("cocotb")
    except Exception:  # pragma: no cover - defensive
        installed_version = "unknown"
    running_version = (
        f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    )
    raise FunctionalVerificationError(
        f"cocotb {installed_version} is installed as a {'/'.join(tags)} wheel "
        f"but klt is running on CPython {running_version} -- its compiled VPI "
        f"module was built for {interpreter_tag} and will dlopen a mismatched "
        "libpython, so the simulator will exit without writing results.xml. "
        "Install a cocotb build for this interpreter, or run klt on an "
        "interpreter cocotb publishes a wheel for."
    )


def _log_tail(log_path: str, lines: int = 8) -> str:
    """The last few lines of a captured engine transcript, for folding into
    an error message. Never raises -- a missing/unreadable log degrades to a
    fixed placeholder, since it is only ever used to *explain* another
    failure."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            captured = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return "no output captured"
    if not captured:
        return "no output captured"
    return " | ".join(captured[-lines:])


def _run_build(
    runner_module,
    *,
    engine: str,
    sources: list[str],
    hdl_toplevel: str,
    build_dir: str,
    build_args: list[str],
    defines: dict[str, str | None],
    includes: list[str],
    timescale: tuple[str, str],
    parameters: dict[str, Any],
    log_path: str,
    waves: bool = False,
):
    """``Runner.build()``, with every failure mode collapsed into
    :class:`FunctionalVerificationError`. Returns the constructed ``Runner``
    so the caller can reuse it for the test step. ``timescale`` is passed
    here *and* in :func:`_run_test` -- see this module's docstring, gotcha 3.
    ``parameters``, when non-empty, overrides Verilog ``parameter``/VHDL
    ``generic`` values at elaboration time (see :func:`_resolve_parameters`).
    ``defines`` and ``includes`` are forwarded verbatim to cocotb's own
    ``Runner.build(defines=..., includes=...)`` (see :func:`_resolve_defines`/
    :func:`_resolve_includes`). ``waves`` (``options.trace``, issue #1845)
    is forwarded to cocotb's own ``Runner.build(waves=...)`` -- cocotb's own
    contract requires ``waves=True`` at build time for ``test()``'s own
    ``waves=True`` to take effect (see :func:`_run_test`).
    """
    try:
        # `get_runner` raises ValueError for an unknown name; an engine
        # class's own __init__ may raise anything at all while probing for a
        # missing simulator binary, so the arm is deliberately broad.
        engine_runner = runner_module.get_runner(engine)
    except Exception as exc:
        raise FunctionalVerificationError(
            f"could not initialise the '{engine}' simulator: {exc}"
        ) from exc

    # cocotb >= 2.0 serialises every ``defines`` value as an SV literal and
    # raises TypeError for ``None`` (issue #2884), so valueless defines are
    # passed as a plain ``-DNAME`` build arg (icarus and verilator both take it).
    valued_defines = {k: v for k, v in defines.items() if v is not None}
    valueless_args = [f"-D{k}" for k, v in defines.items() if v is None]
    try:
        engine_runner.build(
            sources=sources,
            hdl_toplevel=hdl_toplevel,
            build_dir=build_dir,
            build_args=[*build_args, *valueless_args],
            defines=valued_defines,
            includes=includes,
            always=True,
            timescale=timescale,
            parameters=parameters,
            log_file=log_path,
            waves=waves,
        )
    except subprocess.CalledProcessError as exc:
        raise FunctionalVerificationError(
            f"{engine} build/elaboration failed (exit {exc.returncode}): "
            f"{_log_tail(log_path)}"
        ) from exc
    except SystemExit as exc:
        raise FunctionalVerificationError(
            f"{engine} build/elaboration failed (exit {exc.code}): "
            f"{_log_tail(log_path)}"
        ) from exc
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch the {engine} build: {exc}"
        ) from exc
    except Exception as exc:
        raise FunctionalVerificationError(
            f"{engine} build/elaboration failed: {exc}"
        ) from exc

    return engine_runner


def _run_test(
    engine_runner,
    *,
    engine: str,
    module: str,
    module_dirs: list[str],
    hdl_toplevel: str,
    testcase: str | list[str] | None,
    random_seed: int | None,
    build_dir: str,
    test_dir: str,
    results_xml: str,
    timescale: tuple[str, str],
    parameters: dict[str, Any],
    log_path: str,
    waves: bool = False,
    plusargs: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    probe_dir: str | None = None,
) -> None:
    """``Runner.test()``, with the simulator's own exit code deliberately
    discarded (spike section 4): a :class:`SystemExit` here means only "the
    simulator process returned nonzero", which was observed to mean anything
    from "one test failed" to "nothing ran". The verdict comes from
    :func:`parse_results_xml` instead, which the caller runs next.

    Cocotb forwards ``sys.path`` as the simulator's ``PYTHONPATH``. Prepend
    ``module_dirs`` in order, except that a single directory already on the
    path keeps its position. The runtime probe adds its scratch directory
    ahead of that same path for its own test module.

    ``random_seed``, when given, is forwarded to ``Runner.test()``'s own
    ``seed`` parameter, which sets ``COCOTB_RANDOM_SEED`` in the simulator
    subprocess's environment (spike's "open question" on `random_seed`
    reproducibility, issue #423) -- cocotb seeds its regression manager's
    ``random`` module from this value, so a pinned seed reproduces the same
    randomized-test content run-to-run, not merely the same *logged* value
    after the fact. ``None`` leaves cocotb to generate (and itself log) its
    own seed, still recovered afterwards from ``results.xml``'s own
    ``<property name="random_seed">`` element (see
    :func:`_extract_random_seed_property`) so every run's effective seed is
    echoed in the response regardless of whether the request pinned one.

    ``parameters``, when non-empty, is forwarded unchanged -- see
    :func:`_resolve_parameters` and :func:`_run_build` (the same mapping is
    passed to both the build and test steps, matching cocotb's own
    ``Runner`` contract).

    ``extra_env`` (``request.env``, issue #4338), when non-empty, is forwarded
    to ``Runner.test(extra_env=...)`` -- the test step only, never the build.

    ``waves`` (``options.trace``, issue #1845) is forwarded to cocotb's own
    ``Runner.test(waves=...)``, which is what actually triggers the
    simulator to dump a waveform this run -- see :func:`_find_trace_artifact`
    for where each engine writes it.
    """
    original_path = sys.path
    if len(module_dirs) > 1 or (module_dirs and module_dirs[0] not in original_path):
        sys.path = [
            *module_dirs,
            *(path for path in original_path if path not in module_dirs),
        ]
    try:
        if probe_dir is not None:
            sys.path = [probe_dir, *sys.path]
        engine_runner.test(
            test_module=module,
            hdl_toplevel=hdl_toplevel,
            hdl_toplevel_lang="verilog",
            testcase=testcase,
            seed=random_seed,
            build_dir=build_dir,
            test_dir=test_dir,
            results_xml=results_xml,
            timescale=timescale,
            parameters=parameters,
            log_file=log_path,
            waves=waves,
            plusargs=plusargs or [],
            extra_env=extra_env or {},
        )
    except (SystemExit, subprocess.CalledProcessError):
        # Expected on a failing regression -- the results file is the truth.
        pass
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch the {engine} simulation: {exc}"
        ) from exc
    except Exception as exc:
        raise FunctionalVerificationError(
            f"{engine} simulation failed: {exc} ({_log_tail(log_path)})"
        ) from exc
    finally:
        sys.path = original_path


def _engine_version(engine: str) -> str | None:
    """The resolved simulator version string, or ``None`` if unresolvable --
    never raises, and never trusts the probe's exit code (``iverilog -V``
    prints its version *and* returns nonzero on some builds)."""
    command, pattern = _ENGINE_VERSION_COMMANDS[engine]
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return None
    match = pattern.search(f"{completed.stdout}\n{completed.stderr}")
    return match.group(1) if match else None


def _cocotb_version() -> str | None:
    """cocotb's own version string, or ``None`` -- never raises."""
    try:
        import cocotb

        return str(cocotb.__version__)
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# coverage extraction (Verilator only -- spike section 5)
# ---------------------------------------------------------------------------


def _find_coverage_dat(test_dir: str, build_dir: str) -> str | None:
    """Locate Verilator's ``coverage.dat``. It is written by the compiled
    model into the *simulation's* working directory (``test_dir``, verified
    live); ``build_dir`` and a recursive sweep are checked as fallbacks so a
    future Verilator/cocotb layout change degrades to a slower search rather
    than a false "no coverage" report."""
    for candidate in (
        os.path.join(test_dir, "coverage.dat"),
        os.path.join(test_dir, "logs", "coverage.dat"),
        os.path.join(build_dir, "coverage.dat"),
    ):
        if os.path.isfile(candidate):
            return candidate
    for root in (test_dir, build_dir):
        for dirpath, _dirnames, filenames in os.walk(root):
            if "coverage.dat" in filenames:
                return os.path.join(dirpath, "coverage.dat")
    return None


def collect_coverage(
    *, test_dir: str, build_dir: str, info_path: str, repo_root: str | None = None
) -> dict[str, Any]:
    """Post-process Verilator's ``coverage.dat`` into the contract's
    ``coverage`` block (spike section 5).

    Two ``verilator_coverage`` passes: ``--write-info`` emits the portable
    lcov ``.info`` artifact the contract references *by path* (never inlined),
    and ``--report summary`` gives the per-category percentages. Raises
    :class:`FunctionalVerificationError` when coverage was requested but
    cannot be produced -- a requested-and-missing coverage block would
    otherwise be indistinguishable from "not requested" (``null``).
    """
    coverage_dat = _find_coverage_dat(test_dir, build_dir)
    if coverage_dat is None:
        raise FunctionalVerificationError(
            "options.coverage was requested but the run produced no "
            "coverage.dat -- verify the Verilator build accepted --coverage"
        )

    try:
        info = subprocess.run(
            ["verilator_coverage", "--write-info", info_path, coverage_dat],
            capture_output=True,
            text=True,
        )
        summary = subprocess.run(
            ["verilator_coverage", "--report", "summary", coverage_dat],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch verilator_coverage: {exc}"
        ) from exc

    if info.returncode != 0 or not os.path.isfile(info_path):
        raise FunctionalVerificationError(
            "verilator_coverage --write-info failed: "
            f"{(info.stderr or info.stdout or '').strip() or 'no output captured'}"
        )
    if summary.returncode != 0:
        raise FunctionalVerificationError(
            "verilator_coverage --report summary failed: "
            f"{(summary.stderr or summary.stdout or '').strip() or 'no output'}"
        )

    percentages = {
        key: float(value)
        for key, value in _COVERAGE_SUMMARY_RE.findall(
            f"{summary.stdout}\n{summary.stderr}"
        )
    }
    return {
        "line_pct": percentages.get("line"),
        "toggle_pct": percentages.get("toggle"),
        "branch_pct": percentages.get("branch"),
        "expr_pct": percentages.get("expr"),
        "info_path": _hashed_path_envelope(info_path, repo_root=repo_root),
    }


# ---------------------------------------------------------------------------
# `options.trace` (Epic #1585 Phase 3, issue #1845): hand off a run's own
# waveform to `klt wave build`/`klt wave query` (docs/cli/wave.md)
# ---------------------------------------------------------------------------


def _find_trace_artifact(
    *, hdl_toplevel: str, build_dir: str, test_dir: str
) -> tuple[str, str] | None:
    """Locate the waveform trace cocotb's own ``waves=True`` writes when
    ``options.trace`` was requested (see :func:`run_functional_verification`
    and :func:`_run_build`/:func:`_run_test`).

    cocotb's own ``Runner`` implementations name and place this file
    per-engine, never accept a caller-chosen path: Icarus writes
    ``<hdl_toplevel>.fst`` into the *build* directory (the path is embedded,
    absolute, into the generated ``$dumpfile``/``$dumpvars`` shim --
    ``cocotb_tools.runner.Icarus._waves_file``/``_create_iverilog_dump_file``,
    verified against the installed cocotb), while Verilator writes
    ``dump.vcd`` into the simulation's own working directory (``test_dir``
    -- ``cocotb_tools.runner.Verilator._waves_file``). Both engines' own
    candidate locations are checked regardless of which engine actually ran
    -- cheap, and it means a future cocotb release that swaps a path (or
    changes which directory a given engine dumps into) degrades to a
    slightly slower fallback search rather than a false "no trace" report,
    the same posture :func:`_find_coverage_dat` already takes for
    ``coverage.dat``.

    Returns ``(path, format)``, or ``None`` if no trace file can be found.
    """
    candidates = (
        (os.path.join(build_dir, f"{hdl_toplevel}.fst"), "fst"),
        (os.path.join(test_dir, "dump.vcd"), "vcd"),
        (os.path.join(test_dir, f"{hdl_toplevel}.fst"), "fst"),
        (os.path.join(build_dir, "dump.vcd"), "vcd"),
    )
    for path, fmt in candidates:
        if os.path.isfile(path):
            return path, fmt
    for root in (test_dir, build_dir):
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if filename.endswith(".fst"):
                    return os.path.join(dirpath, filename), "fst"
                if filename == "dump.vcd":
                    return os.path.join(dirpath, filename), "vcd"
    return None


def _resolve_trace(
    *, hdl_toplevel: str, build_dir: str, test_dir: str
) -> dict[str, Any]:
    """Report waveform path, format and byte count. A wave-build request
    takes the path and format. A requested but missing waveform is an error.
    """
    found = _find_trace_artifact(
        hdl_toplevel=hdl_toplevel, build_dir=build_dir, test_dir=test_dir
    )
    if found is None:
        raise FunctionalVerificationError(
            "options.trace was requested but the run produced no waveform "
            "file -- verify the resolved engine (icarus/verilator) actually "
            "supports waveform dumping on this host"
        )
    path, fmt = found
    return {"path": path, "format": fmt, "size_bytes": os.path.getsize(path)}


def _apply_sdf_interconnect_only_drop(
    annotate_source_path: str,
    output_dir: str,
    sdf_pre_dropped_counts: dict[str, int],
    entries: str,
) -> str:
    """Issue #2364's ``options.sdf.entries: "interconnect"`` mode, applied as
    the first entry-level step (after :func:`_apply_sdf_header_normalization`)
    of :func:`run_functional_verification`'s SDF-normalization pipeline: when
    the request selected net-delay back-annotation only, every
    non-``INTERCONNECT`` delay entry (on a real ``write_sdf`` output, the
    ``IOPATH`` population) is removed from the SDF text before
    ``$sdf_annotate`` and counted under :data:`SDF_IOPATH_DROPPED_CLASS` --
    a requested, honestly-counted configuration rather than a hand-filtered
    SDF, validated on a timing-clean sky130A design where ``IOPATH``
    annotation kills the design even at a 10x-relaxed clock while
    ``INTERCONNECT``-only annotation simulates correctly.

    Returns the path :func:`run_functional_verification` should hand to the
    next normalization step -- ``annotate_source_path`` unchanged when the
    mode was not selected (or nothing matched), or the filtered-copy path
    otherwise. The count is recorded into ``sdf_pre_dropped_counts`` in place
    rather than returned separately, so this call site is a single expression
    with no branch of its own in ``run_functional_verification`` -- pulled
    out into its own function for the same cyclomatic-complexity reason
    ``_apply_sdf_physical_only_drop``'s own docstring records.
    """
    if entries != "interconnect":
        return annotate_source_path
    return _write_sdf_drop_step(
        _drop_sdf_non_interconnect_entries(annotate_source_path),
        annotate_source_path,
        output_dir,
        "klt_sdf_interconnect_only.sdf",
        SDF_IOPATH_DROPPED_CLASS,
        sdf_pre_dropped_counts,
    )


def _apply_sdf_unsupported_edge_modpath_drop(
    annotate_source_path: str,
    output_dir: str,
    sources: list[str],
    sdf_pre_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
) -> str:
    """Issue #2885's ``ifnone`` edge-sensitive-modpath ``IOPATH`` drop,
    applied as one step of :func:`run_functional_verification`'s
    SDF-normalization pipeline: an ``IOPATH`` entry onto a cell arc whose
    only module paths in the request's own cell models are ``ifnone`` +
    edge-sensitive (which Icarus discards at elaboration) cannot match any
    modpath, so it is removed from the SDF text before ``$sdf_annotate`` and
    counted under :data:`SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS`, with the
    distinct dropped arcs recorded alongside it.

    Returns the path the next normalization step should read --
    ``annotate_source_path`` unchanged when nothing was dropped, or a new
    normalized-copy path otherwise. The count and arcs are recorded into the
    two dicts in place, for the same cyclomatic-complexity reason
    :func:`_apply_sdf_physical_only_drop`'s own docstring records.
    """
    return _write_sdf_drop_step(
        _drop_sdf_unsupported_edge_modpath_iopaths(annotate_source_path, sources),
        annotate_source_path,
        output_dir,
        "klt_sdf_ifnone_edge_modpath_dropped.sdf",
        SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS,
        sdf_pre_dropped_counts,
        sdf_pre_dropped_arcs,
    )


def _apply_sdf_bus_bit_modpath_drop(
    annotate_source_path: str,
    output_dir: str,
    sources: list[str],
    sdf_pre_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
) -> str:
    """Issue #2958's bit-selected-bus ``IOPATH`` drop, applied as one step of
    :func:`run_functional_verification`'s SDF-normalization pipeline: a
    per-bit ``IOPATH`` onto a bus output the cell's model reaches only
    through a whole-vector module path cannot match any modpath, so it is
    removed from the SDF text before ``$sdf_annotate`` and counted under
    :data:`SDF_BUS_BIT_MODPATH_DROPPED_CLASS`, with the distinct dropped arcs
    recorded alongside it.

    Returns the path the next normalization step should read --
    ``annotate_source_path`` unchanged when nothing was dropped. Count and
    arcs are recorded into the two dicts in place, as in
    :func:`_apply_sdf_unsupported_edge_modpath_drop`.
    """
    return _write_sdf_drop_step(
        _drop_sdf_bit_selected_bus_iopaths(annotate_source_path, sources),
        annotate_source_path,
        output_dir,
        "klt_sdf_bus_bit_modpath_dropped.sdf",
        SDF_BUS_BIT_MODPATH_DROPPED_CLASS,
        sdf_pre_dropped_counts,
        sdf_pre_dropped_arcs,
    )


def _sdf_dropped_report(
    sdf_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, Any]]:
    """``environment.sdf.dropped``: per class, ``{count, reason}`` -- plus
    ``arcs`` for a class that recorded the specific cell arcs it dropped
    (any class whose pass populates ``sdf_pre_dropped_arcs``, e.g. issues
    #2885, #2958 and #5296; additive, absent on a class that records no
    arcs)."""
    report: dict[str, dict[str, Any]] = {}
    for klass, count in sorted(sdf_dropped_counts.items()):
        entry: dict[str, Any] = {
            "count": count,
            "reason": SDF_BENIGN_DIAGNOSTIC_REASONS.get(klass, ""),
        }
        if klass in sdf_pre_dropped_arcs:
            entry["arcs"] = sdf_pre_dropped_arcs[klass]
        report[klass] = entry
    return report


def _apply_sdf_physical_only_drop(
    annotate_source_path: str,
    output_dir: str,
    sources: list[str],
    hdl_toplevel: str,
    ports: list[tuple[str, str, str]],
    sdf_pre_dropped_counts: dict[str, int],
) -> str:
    """Issue #2363's physical-only-destination SDF-text drop, applied as one
    step of :func:`run_functional_verification`'s SDF-normalization pipeline
    (parallel to the inline #2285 alias-port drop just above its own call
    site): a zero-delay ``INTERCONNECT`` entry from a bit-selected top-level
    input port onto a pin of a *physical-only* instance (a router-inserted
    antenna diode, or any filler/tapcell whose model has no ``specify``
    block and no output pin) is removed before ``$sdf_annotate`` and counted
    under :data:`SDF_PHYSICAL_ONLY_DROPPED_CLASS`. Icarus can annotate only the
    last-declared load (#5705). Zero delay at every corner makes the drop
    timing-neutral even when that load is physical-only.

    Returns the path :func:`run_functional_verification` should hand to
    ``$sdf_annotate`` next -- ``annotate_source_path`` unchanged when
    nothing was dropped, or a new normalized-copy path otherwise. The count
    is recorded into ``sdf_pre_dropped_counts`` in place rather than
    returned separately, so this call site is a single expression with no
    branch of its own in ``run_functional_verification`` -- pulled out into
    its own function specifically to keep that already-baselined function's
    cyclomatic complexity (`complexity-baseline.json`) from growing further.
    """
    return _write_sdf_drop_step(
        _drop_sdf_zero_delay_physical_only_interconnects(
            annotate_source_path,
            _bus_input_port_names(ports),
            _collect_sdf_physical_only_instances(sources, hdl_toplevel),
        ),
        annotate_source_path,
        output_dir,
        "klt_sdf_physical_only_dropped.sdf",
        SDF_PHYSICAL_ONLY_DROPPED_CLASS,
        sdf_pre_dropped_counts,
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _resolve_output_dir(request_dir: str, output_dir: str | None) -> str:
    """Directory run artifacts are written to: ``output_dir`` (relative
    paths resolve against the cwd) or, by default,
    ``<request_dir>/.klt/functional-verification``."""
    if output_dir:
        return os.path.abspath(output_dir)
    return os.path.join(request_dir, ".klt", "functional-verification")


def _resolve_engine(request_doc: dict[str, Any]) -> str:
    engine = request_doc.get("engine", DEFAULT_ENGINE)
    if engine not in SUPPORTED_ENGINES:
        raise FunctionalVerificationError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )
    return engine


def run_functional_verification(
    request: str,
    output_dir: str | None = None,
    base_dir: str | None = None,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Run the cocotb regression declared by ``request`` -- accepting the
    same three forms every other ``klt`` request argument does (see
    :func:`load_request_arg`).

    ``base_dir`` optionally overrides that anchor (issue #5673): every
    relative path (and the default artifact directory) resolves against
    it, as the stdin form anchored there would; it must be a directory.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/functional-verification.md``). Raises
    :class:`FunctionalVerificationError` for anything that prevents a
    trustworthy verdict -- a failing *test* is ``status: "fail"`` (exit 3).

    Artifacts (build directory, raw ``results.xml``, engine transcripts,
    coverage ``.info``) are written to ``.klt/functional-verification/``
    next to the request file (or under ``base_dir``) -- the same default
    ``klt sim``/``klt synthesize`` use -- and kept, never deleted.
    """
    request_doc, request_dir = load_request_arg(request, base_dir=base_dir)
    output_dir = _resolve_output_dir(request_dir, output_dir)
    # Issue #6762: every `{path, scope}` envelope in the report is relative to
    # the *request's* repo, never the cwd's, so one run yields one report.
    repo_root = find_repo_root(request_dir)

    engine = _resolve_engine(request_doc)

    hdl_toplevel = request_doc["hdl_toplevel"]
    # Issue #5684: verify the synthesized netlist before anything is built.
    # Issue #5782: the CLI's --pdk/--pdk-root merge over request.pdk here.
    sources, run_inputs = resolve_run_inputs(
        request_doc,
        request_dir,
        hdl_toplevel,
        functools.partial(_path_envelope, repo_root=repo_root),
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
    )

    verilog_tb = _resolve_verilog_top(
        request_doc["testbench"], sources, engine, request_doc.get("options")
    )
    module, module_dirs, testcase, plusargs = _resolve_testbench_target(
        verilog_tb, request_doc["testbench"], request_dir
    )
    (
        coverage_requested,
        timescale,
        random_seed,
        defines,
        user_build_args,
        includes,
        sdf,
        trace_requested,
    ) = _resolve_options(request_doc.get("options"), engine, request_dir)
    run_inputs.pin_sdf(sdf)
    # Caller defines override PDK defaults (#5782). _resolve_options checked
    # only caller-supplied defines for a conflict with SDF. A derived
    # FUNCTIONAL exists only when sdf is None, so merging unconditionally
    # cannot reintroduce that conflict.
    defines = {**run_inputs.pdk_defines, **defines}
    _check_verilog_top_sdf(verilog_tb, sdf, request_doc.get("parameters"))
    initial_state = _resolve_initial_state(
        request_doc.get("options"), engine, request_dir
    )
    parameters = _resolve_parameters(request_doc.get("parameters"))
    env = _resolve_env(request_doc.get("env"), verilog_top=verilog_tb is not None)
    tools = _resolve_requires_tools(request_doc.get("requires_tools"), env)
    source_defines = _resolve_source_defines(
        request_doc.get("options"), sources, request_dir
    )

    build_dir = os.path.join(output_dir, f"sim_build_{engine}")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    results_xml = os.path.join(output_dir, f"results_{engine}.xml")
    build_log = os.path.join(output_dir, f"build_{engine}.log")
    test_log = os.path.join(output_dir, f"test_{engine}.log")
    # A stale artifact from a previous run must never be mistaken for this
    # run's evidence if the build below fails outright.
    try:
        os.remove(results_xml)
    except OSError:
        pass
    if coverage_requested:
        # Same staleness guard for coverage.dat: a leftover file from a prior
        # run must never be picked up by _find_coverage_dat() if this run's
        # test process crashes after results.xml is written but before a
        # fresh coverage.dat is flushed. Only the coverage-requested path
        # reads coverage.dat, so the removal only needs to happen here.
        for candidate in (
            os.path.join(output_dir, "coverage.dat"),
            os.path.join(output_dir, "logs", "coverage.dat"),
            os.path.join(build_dir, "coverage.dat"),
        ):
            try:
                os.remove(candidate)
            except OSError:
                pass

    # Build args are computed, not selected from a constant (spike §4.2's
    # one structural change): coverage and SDF annotation each contribute
    # their own flags, and they compose rather than overwrite -- even though
    # the two are mutually exclusive by engine today (coverage is
    # Verilator-only, SDF is Icarus-only), so is any later addition. The
    # caller's own `options.build_args` composes last, after both (see
    # :func:`_resolve_build_args`).
    build_args: list[str] = []
    # The Verilog toplevel actually passed to `-s`/cocotb's `hdl_toplevel`
    # for this build+test -- the real DUT unless `options.sdf` swaps in the
    # generated wrapper below (issue #1056).
    build_hdl_toplevel = hdl_toplevel
    # `environment.sdf.dropped` classes decided *before* the run, by the
    # SDF-text normalization passes below, rather than by the post-run
    # transcript scan (issue #2285). Merged with the scan's own counts once
    # the run is over.
    sdf_pre_dropped_counts: dict[str, int] = {}
    # Per-class dropped cell arcs, for a class that names them (issue #2885).
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]] = {}
    # Instances renamed by issue #2897's escaped-divider rewrite, reported
    # as `environment.sdf.renamed` (absent when empty).
    sdf_renamed: list[dict[str, str]] = []
    sdf_output_concat: list[dict[str, Any]] = []
    if coverage_requested:
        build_args += list(COVERAGE_BUILD_ARGS)
    if sdf is not None:
        # Before generating anything: is this host's Icarus new enough to
        # have `-ginterconnect` at all? (issue #1004 -- 12.0 does not, and
        # fails the build with a message that names neither SDF nor this
        # request field.)
        _check_sdf_engine_capability(_engine_version(engine))
        # Issue #1890: an escaped identifier containing a literal
        # occurrence of the SDF's own DIVIDER character in an INTERCONNECT
        # entry crashes vvp outright (a genuine Icarus upstream limitation)
        # -- caught here, before any build artifact is written, rather than
        # surfacing as a bare SIGABRT core dump four layers down. Narrowed
        # by issue #2897 to a *pin/port* of that shape: an *instance* of
        # that shape is renamed below instead.
        _reject_sdf_escaped_divider_interconnects(sdf["file"])
        # Issue #4521: an SDF rooted at a child of the annotation scope
        # (a bench wrapping the netlist) also aborts vvp -- named here.
        _reject_sdf_mis_scoped_top_cell(
            sdf["file"], sources, hdl_toplevel, sdf.get("scope")
        )
        # Issue #3740: an INTERCONNECT endpoint naming an instance the
        # netlist does not have aborts vvp the same way -- caught here too.
        _reject_sdf_unresolvable_scopes(
            sdf["file"], sources, hdl_toplevel, sdf.get("scope")
        )
        if parameters:
            # cocotb's own Icarus parameter-override syntax is always
            # `-P<hdl_toplevel>.<name>=<value>` (its Runner, not this
            # module, formats it) -- once `hdl_toplevel` becomes the
            # generated wrapper below, that would silently target the
            # wrapper (which declares no parameters) instead of the real
            # DUT rather than erroring, so the combination is rejected up
            # front instead of risking a parameter override that looks
            # applied but was not.
            raise FunctionalVerificationError(
                "request.parameters cannot currently be combined with "
                "options.sdf on Icarus: the top-level-port workaround "
                "(issue #1056) elaborates a generated wrapper module as the "
                "new -s root, and cocotb's own parameter-override syntax "
                "would then target that wrapper instead of the real DUT"
            )
        # The transparent pass-through wrapper (issue #1056) that makes the
        # DUT a nested child instance rather than its own elaboration root
        # -- required for Icarus to resolve a bare top-level-port
        # `INTERCONNECT` endpoint at all -- plus the generated second
        # elaboration root carrying the `$sdf_annotate` call itself, and the
        # three flags that make it do anything -- plus `-T <corner>`, which
        # is how Icarus selects a member of each SDF `min:typ:max` triplet
        # (a compile-time flag, not an `$sdf_annotate` argument; see
        # `SDF_CORNERS`).
        ports = _parse_toplevel_ports(sources, hdl_toplevel)
        wrapper_parameters = _parse_toplevel_parameters(sources, hdl_toplevel)
        wrapper_path = os.path.join(output_dir, f"{SDF_WRAPPER_MODULE}.v")

        # Issue #2364: `options.sdf.entries: "interconnect"` -- net-delay
        # back-annotation only, as the first entry-level normalization step.
        # On a timing-clean design (positive setup/hold slack at the SDF's own
        # corner) IOPATH annotation on the sky130 specify-branch models
        # kills the design in Icarus even at a 10x-relaxed clock period,
        # while INTERCONNECT-only annotation simulates correctly, so that
        # configuration is a first-class, counted mode rather than something
        # a caller hand-filters off-tool. A run that did not select the mode
        # never drops anything and is byte-identical to before.
        # Issue #1880: OpenSTA's `write_sdf` header carries VOLTAGE/
        # TEMPERATURE/PROCESS as `min::max` triples with an empty typ member,
        # which `iverilog -T typ` (the default corner) rejects as `Chosen
        # value not defined`. Always-on, header-only, and a no-op on an SDF
        # whose header is already complete.
        # Issue #2897: an instance name that is an escaped identifier
        # containing a literal `.` (or the SDF's own DIVIDER) -- a flattened
        # `generate` array's `\g[0].sub.u1` -- crashes vvp in an
        # INTERCONNECT endpoint and fails to resolve in a CELL's INSTANCE,
        # under either DIVIDER. Renamed to a plain identifier consistently
        # in build-dir copies of the SDF and of every source declaring it
        # (the caller's own files are untouched); reported as
        # `environment.sdf.renamed`. A no-op on any other SDF.
        annotate_source_path, sources, sdf_renamed = _apply_sdf_escaped_divider_rename(
            sdf["file"], sources, output_dir
        )
        annotate_source_path = _apply_sdf_header_normalization(
            annotate_source_path, output_dir
        )
        annotate_source_path = _apply_sdf_interconnect_only_drop(
            annotate_source_path, output_dir, sdf_pre_dropped_counts, sdf["entries"]
        )
        # Issue #2885: an IOPATH onto a cell arc whose only module paths are
        # `ifnone` + edge-sensitive (gf180mcu's XOR/XNOR/MUX-select shape)
        # has no modpath to match -- Icarus discards those paths at
        # elaboration -- so it is removed here and counted (with the arcs
        # named) in `environment.sdf.dropped` instead of failing the run as
        # `Unable to match ModPath`. Any other IOPATH mismatch is left in
        # place and still fails the diagnostic gate loudly. A no-op (the
        # SDF is not even read) when no source mentions `ifnone`.
        annotate_source_path = _apply_sdf_unsupported_edge_modpath_drop(
            annotate_source_path,
            output_dir,
            sources,
            sdf_pre_dropped_counts,
            sdf_pre_dropped_arcs,
        )
        # Issue #2958: a per-bit IOPATH onto a bus output the cell model
        # reaches only through a whole-vector module path (a hard macro's
        # `(CLK *> DOUT)`) has no per-bit modpath to match, so it is removed
        # here and counted (arcs named) like the #2885 drop above. A no-op
        # (the SDF is not even read) when no source declares a bus output
        # alongside a `specify` block.
        annotate_source_path = _apply_sdf_bus_bit_modpath_drop(
            annotate_source_path,
            output_dir,
            sources,
            sdf_pre_dropped_counts,
            sdf_pre_dropped_arcs,
        )
        # Drop operator conditions and valueless IOPATHs before Icarus parses the SDF.
        annotate_source_path = _apply_sdf_iopath_syntax_drops(
            annotate_source_path,
            output_dir,
            sdf_pre_dropped_counts,
            sdf_pre_dropped_arcs,
        )
        # Issue #1619: a bit-selected top-level-port INTERCONNECT entry (any
        # real design's bus ports produce these) can poison a sibling entry
        # on its own net within one $sdf_annotate call. Deferring those
        # entries to their own, later call -- the module comment above
        # :func:`_split_sdf_bus_port_interconnects` records which of the two
        # confirmed mechanisms this fixes, and the scalar-ports-only no-op
        # (#1069's own case) -- keeps `sdf_paths` single-element there.
        # Issue #2285: an `INTERCONNECT` onto a top-level port bit the netlist
        # drives through an `assign` alias cannot be annotated by Icarus at all
        # and cannot be made annotatable by rewriting the endpoint either; a
        # zero-delay-at-every-corner entry is dropped up front and counted in
        # `environment.sdf.dropped` instead of failing the run (the helper
        # refuses nonzero delays on separately assigned bits).
        annotate_source_path, sources, sdf_output_concat = _apply_sdf_alias_port_drop(
            annotate_source_path,
            output_dir,
            sources,
            hdl_toplevel,
            sdf_pre_dropped_counts,
        )

        vector_ports = {name for _, width, name in ports if width}

        # Issue #2363: an `INTERCONNECT` entry from a bit-selected top-level
        # input port onto a pin of a *physical-only* instance -- a
        # router-inserted antenna diode, or any filler/tapcell whose model
        # is timing-empty -- is shape (b) of the #1619/#1857 spike: on a
        # fanout-above-1 net, #5705's cap leaves every non-last-declared
        # load's entry unannotated. Unlike the general shape (b) case
        # (which stays failing, pinned by
        # `test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
        # unresolvable`), this destination has no timing content and no
        # logic observes the pin -- its entry can still be the cap's
        # survivor, so the drop rests on timing emptiness, not "no modpath
        # exists". A zero-delay-at-every-corner entry is timing-neutral, so
        # it is removed here and counted in `environment.sdf.dropped`
        # before annotation. Non-zero entries stay in the SDF.
        annotate_source_path = _apply_sdf_physical_only_drop(
            annotate_source_path,
            output_dir,
            sources,
            hdl_toplevel,
            ports,
            sdf_pre_dropped_counts,
        )

        # Issue #2984: the remaining zero-delay sub-shape of shape (b) -- the
        # same bit-selected-input-port source with a destination that is not
        # physical-only. Zero-delay entries model no delay at any corner, so
        # dropping them is timing-neutral; non-zero ones stay and still fail.
        annotate_source_path = _apply_sdf_bus_port_source_drop(
            annotate_source_path, output_dir, ports, sdf_pre_dropped_counts
        )

        # Issue #6075: the same cap also bites bit-selected INTERNAL nets,
        # where Icarus inserts no survivor at all (0-of-N at any fanout).
        # Both endpoints are instance pins here, so neither sibling pass
        # above can have claimed these entries; the decision reads the same
        # gate-level parse, and the zero-delay bound is the same.
        annotate_source_path = _apply_sdf_internal_bus_net_drop(
            annotate_source_path,
            output_dir,
            sources,
            hdl_toplevel,
            sdf_pre_dropped_counts,
        )

        split = _split_sdf_bus_port_interconnects(annotate_source_path, vector_ports)
        if split is None:
            sdf_paths = [annotate_source_path]
        else:
            safe_text, deferred_text = split
            safe_path = os.path.join(output_dir, "klt_sdf_safe.sdf")
            deferred_path = os.path.join(output_dir, "klt_sdf_deferred_bus_ports.sdf")
            with open(safe_path, "w", encoding="utf-8") as handle:
                handle.write(safe_text)
            with open(deferred_path, "w", encoding="utf-8") as handle:
                handle.write(deferred_text)
            sdf_paths = [safe_path, deferred_path]
        # Issue #5769: remember what Icarus actually reads -- a refusal
        # resolves its intermodpath locators against these files (Icarus's
        # line counter is cumulative across the calls above), never
        # against the request's own SDF path.
        sdf.update(_sdf_refusal_attribution(sdf_paths, ports))
        shim_path = os.path.join(output_dir, f"{SDF_ANNOTATE_MODULE}.v")
        _write_sdf_annotate_shim(
            shim_path,
            sdf_paths=sdf_paths,
            scope=_sdf_annotate_scope(verilog_tb, sdf.get("scope")),
        )
        sources, build_hdl_toplevel = _sdf_extend_sources(
            verilog_tb,
            sources,
            wrapper_path=wrapper_path,
            shim_path=shim_path,
            hdl_toplevel=hdl_toplevel,
            ports=ports,
            wrapper_parameters=wrapper_parameters,
        )
        build_args += [*SDF_BUILD_ARGS, "-T", sdf["corner"]]
    initial_state_info: dict[str, Any] = {}
    if initial_state is not None:
        shim_path, initial_state_info = _write_initial_state_shim(
            initial_state,
            sources=run_inputs.source_paths,
            hdl_toplevel=hdl_toplevel,
            output_dir=output_dir,
            scope=_sdf_annotate_scope(None) if sdf is not None else hdl_toplevel,
            renamed=sdf_renamed,
        )
        sources = [*sources, shim_path]
        build_args += ["-s", INITIAL_STATE_MODULE]
    build_args += user_build_args
    # Issue #3290: scoped defines are applied last, to the build source list
    # only -- the port parse and source-text scans above read the real files.
    sources, scoped_include_dirs = _write_scoped_define_wrappers(
        sources, source_defines, output_dir
    )
    includes = [*includes, *scoped_include_dirs]
    if sdf is not None:
        _check_sdf_time_units(
            sdf,
            sdf_paths,
            sources,
            verilog_tb["dut_instance"] if verilog_tb else hdl_toplevel,
            timescale[0] if verilog_tb is None else None,
            includes,
            defines,
            user_build_args,
            build_dir if verilog_tb is None else None,
        )

    if verilog_tb is not None:
        return with_run_inputs(
            _run_verilog_top(
                verilog_tb,
                engine=engine,
                hdl_toplevel=hdl_toplevel,
                sources=sources,
                build_args=build_args,
                defines=defines,
                includes=includes,
                build_dir=build_dir,
                build_log=build_log,
                test_log=test_log,
                sdf=sdf,
                sdf_pre_dropped_counts=sdf_pre_dropped_counts,
                sdf_pre_dropped_arcs=sdf_pre_dropped_arcs,
                sdf_renamed=sdf_renamed,
                source_defines=source_defines,
                tools=tools,
                repo_root=repo_root,
            ),
            run_inputs,
        )

    runner_module = _import_runner()
    _check_cocotb_abi_compatibility()
    _check_icarus_python_runtime(
        runner_module, engine, module_dirs, env, timescale, output_dir
    )
    engine_runner = _run_build(
        runner_module,
        engine=engine,
        sources=sources,
        hdl_toplevel=build_hdl_toplevel,
        build_dir=build_dir,
        build_args=build_args,
        defines=defines,
        includes=includes,
        timescale=timescale,
        parameters=parameters,
        log_path=build_log,
        waves=trace_requested,
    )
    run_specs = _initial_state_run_specs(
        initial_state,
        plusargs,
        engine=engine,
        output_dir=output_dir,
        results_xml=results_xml,
        test_log=test_log,
    )
    for spec in run_specs:
        _run_spec_test(
            engine_runner,
            spec,
            engine=engine,
            module=module,
            module_dirs=module_dirs,
            hdl_toplevel=build_hdl_toplevel,
            testcase=testcase,
            random_seed=random_seed,
            build_dir=build_dir,
            test_dir=output_dir,
            timescale=timescale,
            parameters=parameters,
            extra_env=env,
            waves=trace_requested,
        )
    results_xml = run_specs[0]["results_xml"]
    test_log = run_specs[0]["test_log"]

    sdf_dropped_counts: dict[str, int] = {}
    if sdf is not None:
        # Icarus exits successfully even when annotation fails. Check every
        # transcript before accepting the test verdict.
        diagnostics, scanned_dropped_counts = _scan_sdf_diagnostics(build_log, test_log)
        # Each seed annotates the same build. Count benign drops once and
        # retain actionable diagnostics from every seed.
        for spec in run_specs[1:]:
            extra, _ = _scan_sdf_diagnostics(spec["test_log"])
            diagnostics.extend(extra)
        # The two dropped-class sources are disjoint by construction: the
        # transcript scan counts diagnostic *lines* Icarus emitted, while
        # `sdf_pre_dropped_counts` counts SDF *entries* removed before the
        # run (issue #2285), for which Icarus emits nothing at all.
        sdf_dropped_counts = {**sdf_pre_dropped_counts, **scanned_dropped_counts}
        if diagnostics:
            raise _sdf_refusal_error(
                sdf,
                engine,
                diagnostics,
                _sdf_dropped_report(sdf_dropped_counts, sdf_pre_dropped_arcs),
            )

    tests, (passed_count, failed_count, skipped_count), run_reports = (
        _collect_run_results(
            run_specs,
            engine=engine,
            module=module,
            build_log=build_log,
            repo_root=repo_root,
        )
    )
    run_inputs.normalize_failure_messages(tests, request_dir)

    coverage = None
    coverage_info_path = None
    if coverage_requested:
        coverage_info_path = os.path.join(output_dir, "coverage.info")
        coverage = collect_coverage(
            test_dir=output_dir,
            build_dir=build_dir,
            info_path=coverage_info_path,
            repo_root=repo_root,
        )

    # Resolve a requested waveform after the run verdict and coverage are known.
    trace = None
    if trace_requested:
        trace = _resolve_trace(
            hdl_toplevel=build_hdl_toplevel, build_dir=build_dir, test_dir=output_dir
        )

    report = _Report(
        {
            "schema_version": SCHEMA_VERSION,
            "engine": engine,
            "hdl_toplevel": hdl_toplevel,
            "testbench": module,
            "status": "fail" if failed_count else "pass",
            "test_count": len(tests),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "tests": tests,
            "coverage": coverage,
            "trace": trace,
            "environment": {
                "engine": engine,
                "engine_version": _engine_version(engine),
                "tools": tools,
                "cocotb_version": _cocotb_version(),
                "results_xml": _hashed_path_envelope(results_xml, repo_root=repo_root),
                # Additive (issue #3013): the simulator/cocotb console and build
                # transcripts, named by path (never inlined) like `results_xml`.
                "test_log": _hashed_path_envelope(test_log, repo_root=repo_root),
                "build_log": _hashed_path_envelope(build_log, repo_root=repo_root),
                "random_seed": _extract_random_seed_property(results_xml),
                # Retyped (issue #5784) to [{source, defines}]; `[]` when absent.
                "source_defines": _source_defines_envelope(source_defines, repo_root),
                # Additive (issue #4338): `request.env` as given; `{}` when absent.
                "env": env,
                # Additive (issue #5697): `request.parameters` echo; `{}` when absent.
                "parameters": parameters,
                "plusargs": plusargs,  # additive, issue #6906; `[]` when absent
                # Additive (#2998): `null` unless `options.initial_state` was set;
                # `seeds` the effective seeds (empty for mode "x"), `runs` one per seed.
                "initial_state": _initial_state_echo(
                    initial_state, initial_state_info, run_reports, repo_root
                ),
                # Additive (issue #1002, spike §4.2 item 5): `null` on an
                # ordinary run, an object on an SDF-annotated one -- so a caller
                # can tell an annotated run from an unannotated one *from the
                # JSON*, without reading a transcript. `annotated: true` is
                # load-bearing: reaching this point means the diagnostic scan
                # above found nothing *actionable* (nothing non-benign left).
                # `annotated: true` alone cannot distinguish "every delay and
                # every timing check applied" from "every IOPATH applied and
                # every TIMINGCHECK section was dropped" -- the normal case on
                # Icarus, which has no SDF TIMINGCHECK support at all (issue
                # #1102). `partial`/`dropped` make that distinction
                # machine-readable: `partial` is true whenever any benign class
                # was filtered out of the diagnostic gate above, and `dropped`
                # names each class with its filtered-line count and a
                # human-readable reason (:data:`SDF_BENIGN_DIAGNOSTIC_REASONS`).
                "sdf": (
                    None
                    if sdf is None
                    else _sdf_annotation_report(
                        sdf,
                        repo_root,
                        sdf_dropped_counts,
                        sdf_pre_dropped_arcs,
                        sdf_renamed,
                        sdf_output_concat,
                    )
                ),
            },
        }
    )
    with_run_inputs(report, run_inputs)
    report.local_paths = {
        "coverage_info_path": coverage_info_path,
        "results_xml": results_xml,
        "test_log": test_log,
        "build_log": build_log,
        "sdf": None if sdf is None else sdf["file"],
        "initial_state_netlist": (
            None if initial_state is None else initial_state["netlist"]
        ),
    }
    return report


# Re-exports of the subsystems split out of this module. Imported last so the
# sibling modules (which reach back into this one lazily via ``_fv``) find
# every name they need already defined.
from .functional_verification_mutation import (  # noqa: E402, F401
    run_functional_verification_with_mutations,
)
from .functional_verification_mutation_proposals import (  # noqa: E402, F401
    _ProposalRecord,
    _validate_mutation_scope,
    _validate_proposal_indices,
    load_mutation_proposals,
)
from .functional_verification_mutation_variant import (  # noqa: E402, F401
    _build_and_test_variant,
    _run_isolated_variant,
)
from .functional_verification_run_results import (  # noqa: E402
    _collect_run_results,
)
from .functional_verification_search_path import (  # noqa: E402
    _reject_env_pythonpath,
    resolve_testbench_search_dirs,
)
from .functional_verification_verilog_top import (  # noqa: E402, F401
    _check_verilog_top_sdf,
    _reject_verilog_top_mutations,
    _resolve_testbench_target,
    _resolve_verilog_top,
    _run_verilog_top,
    _sdf_annotate_scope,
    _sdf_extend_sources,
)
