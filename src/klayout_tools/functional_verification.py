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
   distinct from finding 7's *scalar* bare-port fix, and only reachable once
   that fix let a *vector* port bit (``y[0]``, ``a[0]``) resolve at all. Two
   separate mechanisms, depending on which side of the entry carries the
   bit-selected port -- see the module comment above
   :func:`_split_sdf_bus_port_interconnects` for the full write-up and
   `docs/design/sdf-annotate-feasibility-spike.md` §3.7 for the live
   evidence: (a) as a *destination*, order-dependent -- fixed by deferring
   every such entry to its own, later ``$sdf_annotate`` call
   (:func:`_split_sdf_bus_port_interconnects`); (b) as a *source* feeding a
   ``specify``-bearing destination -- fails even in total isolation,
   unfixable by reordering, and confirmed sensitive to the testbench itself
   (a no-op test resolves cleanly; any test that actually drives the DUT and
   awaits a trigger does not) -- left unfixed and pinned down by
   ``test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
   unresolvable`` rather than silently reported as "annotated" or masked.
   The issue's own "wrapper double-binds the net identity" hypothesis is
   refuted by shape (a) alone: reordering the entries (bit-selected-port
   entry last instead of first) resolves everything, which a genuine
   identity double-bind would not fix.

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
    an entry with a non-zero value at *any* corner is left in place and
    still fails the gate loudly. See the module comment above
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

import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import queue as _queue_module
import re
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._paths import _load_request_json
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape
from ._vendor import mutation_variants as _mutation_variants
from .env_provenance import find_repo_root, repo_relative_path
from .functional_verification_initial_state import (
    INITIAL_STATE_MODULE,
    SEED_PLUSARG,
    build_initial_state_verilog,
    count_d_nets,
)
from .functional_verification_sdf import (
    _check_sdf_engine_capability,
    _collect_sdf_alias_port_bits,
    _collect_sdf_physical_only_instances,
    _drop_sdf_bit_selected_bus_iopaths,
    _drop_sdf_non_interconnect_entries,
    _drop_sdf_unsupported_edge_modpath_iopaths,
    _drop_sdf_zero_delay_alias_port_interconnects,
    _drop_sdf_zero_delay_bus_port_source_interconnects,
    _drop_sdf_zero_delay_physical_only_interconnects,
    _normalize_sdf_header_triples,
    _parse_toplevel_ports,
    _reject_sdf_escaped_divider_interconnects,
    _reject_sdf_unresolvable_interconnect_instances,
    _reject_sdf_with_functional_models,
    _resolve_sdf_option,
    _scan_sdf_diagnostics,
    _split_sdf_bus_port_interconnects,
    _write_sdf_annotate_shim,
    _write_sdf_dut_wrapper,
)
from .functional_verification_sdf_rename import (
    _apply_sdf_escaped_divider_rename,
    _sdf_renamed_report,
)
from .reset_coverage import ResetCoverageError, sequential_elements

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 2

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

#: Only Icarus has an SDF back-annotation path through this flow: Verilator
#: has no ``$sdf_annotate`` at all (spike §4.1). ``options.sdf`` on any other
#: engine is a request error, mirroring ``options.coverage`` + ``icarus``.
SDF_ENGINES = ("icarus",)

#: The extra elaboration root that carries the ``$sdf_annotate`` call. A
#: cocotb regression's ``hdl_toplevel`` *is* the DUT (driven from Python),
#: so there is no Verilog testbench module to host an ``initial`` block --
#: elaborating a second, otherwise-empty root alongside the DUT supplies one
#: without touching the netlist, the testbench, or the DUT's own hierarchy.
#: cocotb's own Icarus runner already uses this exact shape for its
#: ``cocotb_iverilog_dump`` waveform module.
SDF_ANNOTATE_MODULE = "klt_sdf_annotate"

#: The generated transparent pass-through wrapper module (issue #1056's
#: fix -- see :func:`_write_sdf_dut_wrapper`), and the name it instantiates
#: the real DUT under. Both are elaborated in place of ``hdl_toplevel``
#: *only* for an ``options.sdf`` build; every other build path (baseline
#: gate-level, RTL, coverage) still passes the real ``hdl_toplevel`` straight
#: through, unaffected.
SDF_WRAPPER_MODULE = "klt_sdf_dut_wrapper"
SDF_WRAPPER_DUT_INSTANCE = "klt_sdf_dut"

#: ``iverilog`` flags an SDF-annotated build requires, all three verified
#: live (spike §3.1/§3.2/§2.1): without ``-gspecify`` Icarus omits every
#: specify block *and* the ``$sdf_annotate`` call itself, then runs at zero
#: delay and exits ``0``; without ``-ginterconnect`` every ``INTERCONNECT``
#: entry -- which is exactly what a post-route SDF carries -- fails at run
#: time; and ``-s <module>`` is what makes the generated shim a second
#: elaboration root rather than dead code.
SDF_BUILD_ARGS = ("-gspecify", "-ginterconnect", "-s", SDF_ANNOTATE_MODULE)

#: ``options.sdf.corner`` values, mapping 1:1 onto ``iverilog -T``. Corner
#: selection is a *compile-time* flag for Icarus, not an ``$sdf_annotate``
#: argument: Icarus ignores every argument past the second ("$sdf_annotate
#: currently only uses the first two argument", its own wording), so a
#: request that named a corner there would be silently ignored (spike §3.5).
SDF_CORNERS = ("min", "typ", "max")

#: Icarus's own default when ``-T`` is not passed; named explicitly so the
#: response's echo is never ambiguous about which corner was simulated.
DEFAULT_SDF_CORNER = "typ"

#: ``options.sdf.entries`` values (issue #2364). ``"all"`` (the default, and
#: the only behavior before this field existed) annotates every delay entry
#: the SDF carries -- ``IOPATH`` module-path delays and ``INTERCONNECT`` net
#: delays alike. ``"interconnect"`` drops every non-``INTERCONNECT`` delay
#: entry from the SDF text before annotation, counted as its own
#: :data:`SDF_IOPATH_DROPPED_CLASS` ``environment.sdf.dropped`` class: the
#: net-delay back-annotation configuration #2364's isolation table validated
#: on a timing-clean sky130A design (positive setup/hold slack, TNS 0), where
#: ``INTERCONNECT``-only annotation simulates correctly while any SDF
#: carrying ``IOPATH`` entries kills the design even at a 10x-relaxed clock
#: period -- an annotation-mechanism kill, not a timing outcome.
SDF_ENTRIES_MODES = ("all", "interconnect")

#: See :data:`SDF_ENTRIES_MODES`; the explicit default keeps the response's
#: echo unambiguous about which entry population was annotated.
DEFAULT_SDF_ENTRIES = "all"

#: Every SDF problem Icarus hits -- an unopenable file, an instance it cannot
#: find, an ``IOPATH`` the cell's ``specify`` block does not declare, an
#: ``INTERCONNECT`` without ``-ginterconnect`` -- is reported with one of
#: these prefixes and then **survived**: the annotation is dropped, `vvp`
#: exits ``0``, and cocotb duly reports the zero-delay verdict as a pass.
#: Scanning for them is the only thing standing between "re-verified with
#: real post-route delays" and "re-ran at zero delay and nobody noticed"
#: (spike §3.3, where a 50-flop netlist annotated 0 flops and still reported
#: the expected verdicts).
SDF_DIAGNOSTIC_MARKERS = ("SDF WARNING", "SDF ERROR")

#: ...with two deliberate exemptions, both of which fire on *correct* input:
#:
#: - ``TIMINGCHECK not supported`` -- Icarus implements SDF delays but not
#:   SDF timing checks (spike §3.4), and a real ``write_sdf`` emits
#:   ``TIMINGCHECK`` sections alongside ``IOPATH``/``INTERCONNECT``. Failing
#:   on this would reject every real post-route SDF; the delays it *does*
#:   apply are unaffected. The consequence worth knowing (no setup/hold
#:   violation reporting on this path -- that is OpenSTA's job) is documented
#:   in ``docs/cli/functional-verification.md``.
#: - ``NEGATIVE_CONSTRAINT``/``PATHPULSE`` &c. are **not** exempted: only the
#:   timing-check family is, because only it is both benign and unavoidable.
SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS = ("TIMINGCHECK",)

#: The shape Icarus gives *every* SDF diagnostic it emits: the marker, then
#: a ``<file>:<line-number>:`` locator, then the message -- true of all four
#: failure modes the spike captured live (§3.3) and of the ``$sdf_annotate``
#: argument warning (§3.6). A marker-bearing transcript line that does *not*
#: have this shape is not a diagnostic Icarus wrote; it is a corrupted one
#: (issue #1136 -- see :func:`_scan_sdf_diagnostics`), so the classification
#: below is applied only to lines that match.
SDF_DIAGNOSTIC_LINE_RE = re.compile(r"^SDF (?:WARNING|ERROR): .+?:\d+: .+$")

#: cocotb's log-line header (``<sim-time><unit> <LEVEL> <logger>``), e.g.
#: ``590.00ns INFO     cocotb.klt_sdf_dut_wrapper``. A marker-bearing line that
#: *contains* one is a stdout splice even when it still matches
#: :data:`SDF_DIAGNOSTIC_LINE_RE` (issue #2996, extending #1136's rule past
#: the locator): the cut landed inside the message, or inside the path with
#: the spliced-in line's own ``<file>:<line>:`` satisfying the locator. Keying
#: on the interleaved writer's signature rather than on a second locator
#: makes the result independent of path length and of which log line lands.
SDF_SPLICED_LOG_HEADER_RE = re.compile(
    r"\s\d+(?:\.\d+)?\s?(?:fs|ps|ns|us|ms|s)\s+"
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\S+"
)

#: The ``environment.sdf.dropped`` class counting the zero-delay
#: ``INTERCONNECT`` entries removed before annotation because their
#: destination is a top-level port bit the netlist drives through an
#: ``assign`` alias -- issue #2285's counted exemption, the second member of
#: this shape after #1102's ``timingcheck``. Unlike ``timingcheck`` this
#: class is *not* derived from a diagnostic substring: the entries are
#: dropped from the SDF text up front
#: (:func:`_drop_sdf_zero_delay_alias_port_interconnects`), so Icarus never
#: emits a diagnostic for them at all.
SDF_ALIAS_PORT_DROPPED_CLASS = "zero_delay_alias_port_interconnect"

#: The ``environment.sdf.dropped`` class counting the zero-delay
#: ``INTERCONNECT`` entries removed before annotation because they run from
#: a bit-selected top-level input port onto a pin of a *physical-only*
#: instance -- a router-inserted antenna diode (or any filler/tapcell whose
#: model is timing-empty), whose elaborated Verilog has no ``specify`` block
#: and no output pin at all, so no modpath the entry could annotate exists.
#: Issue #2363's counted exemption, kept distinct from
#: :data:`SDF_ALIAS_PORT_DROPPED_CLASS` because the mechanism, the matched
#: endpoint, and the condition all differ -- a caller reading
#: ``environment.sdf.dropped`` needs to tell the two apart. Like the alias
#: class (and unlike ``timingcheck``) it is not derived from a diagnostic
#: substring: the entries are dropped from the SDF text up front
#: (:func:`_drop_sdf_zero_delay_physical_only_interconnects`), so Icarus
#: never emits a diagnostic for them at all.
SDF_PHYSICAL_ONLY_DROPPED_CLASS = "zero_delay_physical_only_interconnect"

#: ``INTERCONNECT`` entries removed before annotation because they run from a
#: bit-selected top-level input port (any destination not already claimed by
#: :data:`SDF_PHYSICAL_ONLY_DROPPED_CLASS`) and carry zero delay at every
#: corner. Icarus can fail to insert an intermodpath for such a source
#: (``Could not find intermodpath!``); an all-zero entry models no delay, so
#: dropping it cannot change simulated timing. Issue #2984's counted
#: exemption; a non-zero entry of the same shape is left in place and still
#: fails loudly. Pre-dropped from the SDF text, not derived from a
#: diagnostic substring.
SDF_BUS_PORT_SOURCE_DROPPED_CLASS = "zero_delay_bus_port_source_interconnect"

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
#: whose diagnostics were filtered out, plus
#: :data:`SDF_ALIAS_PORT_DROPPED_CLASS` (issue #2285) for the one class that
#: is dropped from the SDF text instead of from the transcript.
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
        "Icarus Verilog cannot insert an intermodpath for an INTERCONNECT "
        "entry that runs from a bit-selected top-level input port onto a pin "
        "of a physical-only instance (a router-inserted antenna diode, or a "
        "filler/tapcell whose model carries no timing) -- such an entry "
        "fails with 'Could not find intermodpath!'. The destination's "
        "elaborated model has no specify block and no output pin at all, so "
        "there is no modpath the entry could ever annotate and no logic that "
        "observes the pin; entries whose every min:typ:max delay value is "
        "zero are therefore removed before annotation and counted here "
        "instead of failing the run -- a non-zero-delay entry on the same "
        "destination is left in place and still fails loudly"
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
}

#: Not an ``SDF``-prefixed diagnostic at all, but the single most dangerous
#: line on this path (spike §3.1): with ``-gspecify`` missing, Icarus drops
#: the annotation call as an ordinary compile-time ``warning:`` among
#: hundreds of others and the run is otherwise indistinguishable from a
#: successful annotation. This module always passes ``-gspecify``, so it must
#: never appear -- and if it ever does (a cocotb backend that reorders or
#: drops build args, say), that is a silent zero-delay run, caught here.
SDF_OMITTED_ANNOTATION_MARKER = "Omitting $sdf_annotate"

#: Minimum Icarus *major* version that has ``-ginterconnect`` at all (issue
#: #1004's live finding on Icarus 12.0, folded in here rather than deferred).
#: The spike's GO verdict was captured on Icarus 13.0 -- this repo's own
#: pinned build -- and on 12.0 (Ubuntu noble's distro package) ``iverilog``
#: rejects the flag outright: ``Unknown/Unsupported Language generation
#: interconnect``, exit 255. Since ``-ginterconnect`` is *mandatory* for a
#: post-route SDF (every ``INTERCONNECT`` entry fails without it), an older
#: Icarus cannot serve this request at all; probing the resolved version and
#: saying so is strictly better than letting a raw compiler error surface
#: from four layers down.
SDF_MIN_ICARUS_MAJOR = 13

#: The exact transcript signature cocotb's ``libembed`` emits when its
#: compiled VPI module was ``dlopen``'d by a different CPython than the one
#: cocotb was built for (issue #1103's live finding) -- e.g. a ``cp312``
#: wheel loaded inside a CPython 3.14 simulator subprocess. The pre-flight
#: ``WHEEL``-tag check (:func:`_check_cocotb_abi_compatibility`) catches this
#: *before* the runner is invoked in the common case; these markers are the
#: fallback for whatever that check doesn't cover (unreadable wheel metadata,
#: a mismatch pattern the tag comparison misses), scanned only once
#: ``results.xml`` is confirmed missing.
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

_ENGINE_VERSION_COMMANDS = {
    "icarus": (["iverilog", "-V"], re.compile(r"Icarus Verilog version (\S+)")),
    "verilator": (["verilator", "--version"], re.compile(r"Verilator (\S+)")),
}

_COVERAGE_SUMMARY_RE = re.compile(
    r"^\s*(line|toggle|branch|expr)\s*:\s*([0-9.]+)%", re.MULTILINE
)


class FunctionalVerificationError(Exception):
    """Raised when a regression cannot be *trusted to have run*: a missing/
    malformed request, an unresolvable RTL source or testbench module, an
    unsupported engine, ``options.coverage`` requested on an engine that has
    no coverage path, a missing cocotb/simulator install, a build or
    elaboration error, a simulator crash, or a run that produced no
    ``results.xml``.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. A run that *did* complete and reported failing tests is not
    an error -- it is ``status: "fail"`` + exit code 3 (see this module's
    docstring and ``docs/cli/functional-verification.md``).

    ``code`` is an optional stable machine-readable classification the CLI
    passes through as ``error.code`` (``None`` for every error without one).
    """

    code: str | None = None

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


# ---------------------------------------------------------------------------
# request loading (same file / "-" / inline-JSON convention as `klt lvs`)
# ---------------------------------------------------------------------------


_REQUIRED_REQUEST_FIELDS = ("sources", "hdl_toplevel", "testbench")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared shape check for a JSON-decoded request, however it was sourced
    (file, inline JSON, stdin). ``source`` is folded into the "must be a JSON
    object" error for context."""
    return _shared_validate_request_shape(
        data,
        source,
        error_cls=FunctionalVerificationError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt functional-verification`` request
    JSON file.

    Raises :class:`FunctionalVerificationError` if the file is missing/
    unreadable, not valid JSON, or missing a required top-level field
    (``sources``, ``hdl_toplevel``, ``testbench``). Does not require a
    ``schema`` field, matching ``klt lvs``/``klt sim``/``klt synthesize``'s
    own ``load_request`` (user-authored input, never emitted by this tool).
    """
    request = _load_request_json(request_path, FunctionalVerificationError)
    return _validate_request_shape(request, "request file")


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve the CLI ``request`` argument into a request dict plus the
    directory relative paths inside it should resolve against -- the same
    three forms ``klt lvs``'s own ``request`` argument accepts (a path to a
    JSON file, ``"-"`` for stdin, or an inline JSON object string), so this
    verb composes into ``klt eval``'s descriptor unchanged.
    """
    return _shared_load_request_arg(
        value,
        error_cls=FunctionalVerificationError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


# ---------------------------------------------------------------------------
# request field validation
# ---------------------------------------------------------------------------


def _resolve_sources(sources: Any, request_dir: str) -> list[str]:
    """Validate ``request.sources`` and resolve each entry to an absolute,
    readable path (relative to ``request_dir`` -- the same convention every
    other request-taking verb uses)."""
    if not isinstance(sources, list) or not sources:
        raise FunctionalVerificationError(
            "request.sources must be a non-empty array of paths"
        )
    if not all(isinstance(entry, str) and entry for entry in sources):
        raise FunctionalVerificationError(
            "request.sources entries must be non-empty strings"
        )

    resolved: list[str] = []
    for entry in sources:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isfile(path):
            raise FunctionalVerificationError(f"RTL source not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise FunctionalVerificationError(
                f"could not read RTL source '{entry}': {exc}"
            ) from exc
        resolved.append(os.path.abspath(path))
    return resolved


def _resolve_testbench(
    testbench: Any, request_dir: str
) -> tuple[str, str, str | list[str] | None]:
    """Validate ``request.testbench`` and return
    ``(module, module_dir, testcase)``.

    ``module`` is a Python *module name* (``"test_gcd"``), not a path -- it is
    what cocotb's ``Runner.test(test_module=...)`` imports. By default the
    module's file is located next to the request
    (``<request_dir>/<module>.py``) so a missing testbench is a clear
    request-level error here rather than a ``ModuleNotFoundError`` raised deep
    inside the simulator process (where, verified live, cocotb still exits
    ``0`` and simply writes no ``results.xml``).

    An optional ``testbench.search_path`` overrides *where* ``module`` is
    looked up -- resolved absolute-or-relative-to-``request_dir`` the same way
    ``_resolve_sources()`` resolves each ``request.sources`` entry -- so one
    unmodified testbench module can be shared by several requests that live in
    different directories. When omitted, behavior is unchanged: the module is
    resolved next to the request.
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

    search_path = testbench.get("search_path")
    if search_path is not None:
        if not isinstance(search_path, str) or not search_path:
            raise FunctionalVerificationError(
                "request.testbench.search_path must be a non-empty string when given"
            )
        search_dir = (
            search_path
            if os.path.isabs(search_path)
            else os.path.join(request_dir, search_path)
        )
    else:
        search_dir = request_dir

    module_path = os.path.join(search_dir, f"{module}.py")
    if not os.path.isfile(module_path):
        location = (
            "request.testbench.search_path"
            if search_path is not None
            else "next to the request"
        )
        raise FunctionalVerificationError(
            f"testbench module not found: expected '{module_path}' "
            f"(request.testbench.module names a Python module resolved via {location})"
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

    return module, os.path.dirname(os.path.abspath(module_path)), testcase


def _resolve_options(
    options: Any, engine: str, request_dir: str
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
    if random_seed is not None and (
        isinstance(random_seed, bool) or not isinstance(random_seed, int)
    ):
        raise FunctionalVerificationError(
            "request.options.random_seed must be an integer when given"
        )

    defines = _resolve_defines(options.get("defines"))
    build_args = _resolve_build_args(options.get("build_args"))
    includes = _resolve_includes(options.get("includes"), request_dir)
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
    if (
        not isinstance(seeds, list)
        or not seeds
        or any(isinstance(s, bool) or not isinstance(s, int) for s in seeds)
    ):
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
    options: Any, engine: str, request_dir: str
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
        netlist if os.path.isabs(netlist) else os.path.join(request_dir, netlist)
    )
    if not os.path.isfile(netlist_path):
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


def _path_envelope(path: str | None) -> dict[str, Any]:
    """``path`` as the committable ``{path, scope}`` shape, relative to the
    repo the process was invoked from (never the absolute host path)."""
    root = find_repo_root()
    if path is not None and root is None:
        return {"path": None, "scope": "external"}
    return repo_relative_path(path, repo_root=root)


def _initial_state_run_specs(
    initial_state: dict[str, Any] | None,
    *,
    engine: str,
    output_dir: str,
    results_xml: str,
    test_log: str,
) -> list[dict[str, Any]]:
    """One regression normally; one per seed for ``initial_state.mode:
    "random"`` -- same build, a different ``+klt_init_seed`` plusarg and its
    own results/transcript files."""
    if initial_state is None or initial_state["mode"] != "random":
        return [
            {
                "seed": None,
                "results_xml": results_xml,
                "test_log": test_log,
                "plusargs": [],
            }
        ]
    return [
        {
            "seed": seed,
            "results_xml": os.path.join(
                output_dir, f"results_{engine}_init_seed{seed}.xml"
            ),
            "test_log": os.path.join(output_dir, f"test_{engine}_init_seed{seed}.log"),
            "plusargs": [f"+{SEED_PLUSARG}={seed}"],
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
        # The pre-flight ABI check catches the common case up front; this
        # is the fallback for whatever it doesn't -- only fires when
        # `results.xml` is confirmed missing, so a genuinely unparseable or
        # unreadable results file keeps its own unaugmented message.
        diagnostic = None
        if not os.path.isfile(spec_xml):
            diagnostic = _scan_interpreter_mismatch_diagnostics(
                build_log, spec["test_log"]
            )
        if diagnostic is None:
            raise
        raise FunctionalVerificationError(
            f"{exc} -- probable cause: the {engine} transcript contains "
            f"{diagnostic!r}, which usually means the installed cocotb was "
            "built for a different CPython than the one running klt (an "
            "ABI-mismatched wheel)"
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


def _collect_run_results(
    run_specs: list[dict[str, Any]], *, engine: str, module: str, build_log: str
) -> tuple[list[dict[str, Any]], tuple[int, int, int], list[dict[str, Any]]]:
    """Parse every run's results: ``(tests, (passed, failed, skipped),
    per-seed run reports)``. A seeded run's tests carry ``initial_state_seed``
    (issue #2998)."""
    tests: list[dict[str, Any]] = []
    totals = [0, 0, 0]
    run_reports: list[dict[str, Any]] = []
    for spec in run_specs:
        spec_tests = _parse_run_results(spec, engine=engine, build_log=build_log)
        counts = _executed_result_counts(spec_tests, module)
        totals = [total + count for total, count in zip(totals, counts, strict=True)]
        if spec["seed"] is not None:
            for test in spec_tests:
                test["initial_state_seed"] = spec["seed"]
            run_reports.append(
                {
                    "seed": spec["seed"],
                    "status": "fail" if counts[1] else "pass",
                    "results_xml": _path_envelope(spec["results_xml"]),
                }
            )
        tests.extend(spec_tests)
    return tests, (totals[0], totals[1], totals[2]), run_reports


def _initial_state_echo(
    initial_state: dict[str, Any] | None,
    info: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """``environment.initial_state`` (additive, issue #2998): ``None`` unless
    requested; ``seeds`` is the effective seed list (empty for mode "x") and
    ``runs`` has one entry per seed."""
    if initial_state is None:
        return None
    return {
        "mode": initial_state["mode"],
        "seeds": initial_state["seeds"],
        "netlist": initial_state["netlist"],
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
) -> tuple[str, dict[str, Any]]:
    """Derive the state elements of ``hdl_toplevel`` from the netlist and
    write the generated ``klt_initial_state`` shim; returns ``(path, info)``
    with ``info = {"state_elements", "d_nets", "clock"}``. A sequential cell
    or hard macro the force-D drive cannot reach is a request error naming it
    (the issue's "all flops" contract), never a silent skip."""
    try:
        elements, unsupported = sequential_elements(
            initial_state["netlist"], sources, hdl_toplevel
        )
    except ResetCoverageError as exc:
        raise FunctionalVerificationError(f"options.initial_state: {exc}") from exc
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
    path = os.path.join(output_dir, f"{INITIAL_STATE_MODULE}.v")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            build_initial_state_verilog(
                mode=initial_state["mode"],
                elements=elements,
                scope=hdl_toplevel,
                clock=clock,
            )
        )
    info = {
        "state_elements": len(elements),
        "d_nets": count_d_nets(elements),
        "clock": clock,
    }
    return path, info


def _resolve_defines(defines: Any) -> dict[str, str | None]:
    """Validate the optional ``request.options.defines`` object and return it
    unchanged (or ``{}`` when omitted).

    Forwarded to ``Runner.build(defines=...)`` -- cocotb's own ``Runner``
    accepts string values; a ``None`` value defines the macro with no value
    (e.g. ``` `define USE_POWER_PINS ```). cocotb 2.x cannot serialise
    ``None`` itself, so :func:`_run_build` emits those as ``-DNAME`` build args.
    """
    if defines is None:
        return {}
    if not isinstance(defines, dict):
        raise FunctionalVerificationError(
            "request.options.defines must be a JSON object of string -> string|null"
        )
    for key, value in defines.items():
        if not isinstance(key, str) or not key:
            raise FunctionalVerificationError(
                "request.options.defines keys must be non-empty strings"
            )
        if value is not None and not isinstance(value, str):
            raise FunctionalVerificationError(
                f"request.options.defines[{key!r}] must be a string or null "
                f"-- got a {type(value).__name__}"
            )
    return defines


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
            key if os.path.isabs(key) else os.path.join(request_dir, key)
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


def _resolve_includes(includes: Any, request_dir: str) -> list[str]:
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

    resolved: list[str] = []
    for entry in includes:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isdir(path):
            raise FunctionalVerificationError(f"include directory not found: {entry}")
        resolved.append(os.path.abspath(path))
    return resolved


def _scan_interpreter_mismatch_diagnostics(*log_paths: str) -> str | None:
    """The first line in ``log_paths`` naming cocotb's ABI-mismatch failure
    mode (issue #1103), or ``None``.

    Fallback for whatever :func:`_check_cocotb_abi_compatibility`'s pre-flight
    ``WHEEL``-tag check doesn't catch. This diagnostic-only fallback never
    raises: a missing/unreadable transcript contributes nothing, the same
    posture :func:`_log_tail` takes. The affirmative SDF annotation gate
    separately requires readable transcripts (issue #2130).
    """
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
    the tool's ``--version`` output; ``sha256`` is compared with the resolved
    binary's digest. Every unmet tool is listed in one ``missing_tool`` error.
    Returns the ``environment.tools`` echo (``[]`` when omitted).
    """
    if requires_tools is None:
        return []
    if not isinstance(requires_tools, list):
        raise FunctionalVerificationError(
            "request.requires_tools must be a JSON array of "
            '{"name": ..., "version": ..., "sha256": ...} objects'
        )

    entries: list[tuple[str, str | None, str | None]] = []
    for index, entry in enumerate(requires_tools):
        where = f"request.requires_tools[{index}]"
        if not isinstance(entry, dict):
            raise FunctionalVerificationError(f"{where} must be a JSON object")
        unknown = sorted(set(entry) - _REQUIRES_TOOLS_KEYS)
        if unknown:
            raise FunctionalVerificationError(
                f"{where} has unknown field(s) {unknown} "
                "(allowed: name, version, sha256)"
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
        entries.append((name, entry.get("version"), entry.get("sha256")))

    search_path = env.get("PATH")
    run_env = {**os.environ, **env}
    tools: list[dict[str, Any]] = []
    unmet: list[dict[str, Any]] = []
    for name, want_version, want_sha256 in entries:
        resolved = shutil.which(name, path=search_path)
        if resolved is None:
            unmet.append(
                {
                    "name": name,
                    "wanted": "on PATH",
                    "found": None,
                    "reason": "not found",
                }
            )
            continue
        observed = _tool_version_output(resolved, run_env)
        digest = _file_sha256(resolved)
        problems = []
        if want_version is not None and (
            observed is None or want_version not in observed
        ):
            problems.append(f"version {want_version!r} not in --version output")
        if want_sha256 is not None and digest != want_sha256.lower():
            problems.append(f"sha256 {want_sha256} != {digest}")
        if problems:
            unmet.append(
                {
                    "name": name,
                    "wanted": {"version": want_version, "sha256": want_sha256},
                    "found": {"path": resolved, "version": observed, "sha256": digest},
                    "reason": "; ".join(problems),
                }
            )
        tools.append(
            {"name": name, "path": resolved, "version": observed, "sha256": digest}
        )

    if unmet:
        raise _MissingToolError(unmet)
    return tools


def _file_sha256(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _tool_version_output(path: str, env: dict[str, str]) -> str | None:
    """First non-empty line of ``<path> --version`` (stdout, else stderr);
    ``None`` if it cannot run. A non-zero exit still counts when it printed."""
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return None


class _MissingToolError(FunctionalVerificationError):
    """``missing_tool``: one or more ``request.requires_tools`` unmet. The CLI
    surfaces ``details`` as ``error.details``."""

    def __init__(self, unmet: list[dict[str, Any]]) -> None:
        listing = "; ".join(f"{u['name']} ({u['reason']})" for u in unmet)
        super().__init__(
            f"{len(unmet)} required tool(s) unmet before build: {listing}",
            code="missing_tool",
        )
        self.details = {"unmet_tools": unmet}


# ---------------------------------------------------------------------------
# `--mutations`: the `<proposals>` document (spike section 3b)
# ---------------------------------------------------------------------------


#: `<proposals>` top-level required fields (spike §3b). `schema` is not
#: required or validated -- user-authored input, the same posture every
#: other request-taking verb's own `schema` field already takes.
_MUTATION_PROPOSALS_REQUIRED_FIELDS = ("scope", "proposals")

#: `proposals[]` entry required fields (spike §3b). `detectability_argument`
#: is optional and never validated beyond "string or absent" -- free-text,
#: echoed back unmodified, never acted on programmatically.
_MUTATION_PROPOSAL_REQUIRED_FIELDS = (
    "index",
    "category",
    "file",
    "line",
    "original_code",
    "mutated_code",
)


@dataclass(frozen=True)
class _ProposalRecord:
    """One validated `<proposals>.proposals[]` entry -- satisfies the ported
    `_mutation_variants.MutationProposal` protocol (`index`/`file`/`line`/
    `original_code`/`mutated_code`) by construction, plus the two fields
    (`category`, `detectability_argument`) that protocol doesn't need but the
    response's `results[]` echoes back."""

    index: int
    category: str
    file: str
    line: int
    original_code: str
    mutated_code: str
    detectability_argument: str | None = None


def _load_mutation_proposals_json(path: str) -> Any:
    """Read ``path`` and decode it as JSON, raising
    :class:`FunctionalVerificationError` for every failure mode a caller
    needs to report -- the `<proposals>`-specific analogue of
    :func:`klayout_tools._paths._load_request_json`, kept separate only so
    the error text says "mutation proposals file", not "request file"."""
    if not os.path.exists(path):
        raise FunctionalVerificationError(f"mutation proposals file not found: {path}")
    if os.path.isdir(path):
        raise FunctionalVerificationError(
            f"mutation proposals path is not a file: {path}"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise FunctionalVerificationError(
            f"could not read mutation proposals file: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FunctionalVerificationError(
            f"mutation proposals file is not valid JSON: {exc}"
        ) from exc


def load_mutation_proposals(path: str) -> tuple[list[str], list[_ProposalRecord]]:
    """Load and validate a ``<proposals>`` document (spike §3b): ``schema``
    (unvalidated), ``scope`` (array of RTL source paths, matched against
    `<request>.sources` by the caller -- see
    :func:`_validate_mutation_scope`), and ``proposals[]`` (booley's own
    byte-exact shape, §1a).

    Structural/type errors here are always whole-document failures (exit 1)
    -- a malformed `<proposals>` document can produce no trustworthy
    mutation run at all, the same "fail the whole request, don't run half a
    plan" posture ``options.sdf`` validation already takes in the base
    contract. Per-proposal *anchor* validation (does ``original_code``
    actually occur once on the declared line of the *pristine* source) is
    deliberately **not** done here -- that needs the request's own resolved
    ``sources``, and belongs to :func:`_resolve_mutation_proposals`, whose
    failures are individual ``"rejected"`` results, not whole-document ones.

    Returns ``(scope, proposals)``.
    """
    document = _load_mutation_proposals_json(path)
    if not isinstance(document, dict):
        raise FunctionalVerificationError(
            f"mutation proposals file must contain a JSON object: {path}"
        )
    for field in _MUTATION_PROPOSALS_REQUIRED_FIELDS:
        if field not in document:
            raise FunctionalVerificationError(
                f"mutation proposals file is missing required field: {field}"
            )

    scope_raw = document["scope"]
    if not isinstance(scope_raw, list) or not all(
        isinstance(entry, str) and entry for entry in scope_raw
    ):
        raise FunctionalVerificationError(
            "mutation proposals 'scope' must be an array of non-empty strings"
        )
    scope = list(scope_raw)

    proposals_raw = document["proposals"]
    if not isinstance(proposals_raw, list):
        raise FunctionalVerificationError(
            "mutation proposals 'proposals' must be an array"
        )

    proposals: list[_ProposalRecord] = []
    for position, entry in enumerate(proposals_raw):
        if not isinstance(entry, dict):
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}] must be a JSON object"
            )
        for field in _MUTATION_PROPOSAL_REQUIRED_FIELDS:
            if field not in entry:
                raise FunctionalVerificationError(
                    f"mutation proposals.proposals[{position}] is missing "
                    f"required field: {field}"
                )

        index = entry["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].index must be an integer"
            )
        category = entry["category"]
        if not isinstance(category, str) or not category:
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].category must be "
                "a non-empty string"
            )
        file_field = entry["file"]
        if not isinstance(file_field, str) or not file_field:
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].file must be a "
                "non-empty string"
            )
        line = entry["line"]
        if not isinstance(line, int) or isinstance(line, bool):
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].line must be an integer"
            )
        original_code = entry["original_code"]
        if not isinstance(original_code, str):
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].original_code "
                "must be a string"
            )
        mutated_code = entry["mutated_code"]
        if not isinstance(mutated_code, str):
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].mutated_code "
                "must be a string"
            )
        detectability_argument = entry.get("detectability_argument")
        if detectability_argument is not None and not isinstance(
            detectability_argument, str
        ):
            raise FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].detectability_argument "
                "must be a string when given"
            )

        proposals.append(
            _ProposalRecord(
                index=index,
                category=category,
                file=file_field,
                line=line,
                original_code=original_code,
                mutated_code=mutated_code,
                detectability_argument=detectability_argument,
            )
        )

    return scope, proposals


def _validate_proposal_indices(proposals: list[_ProposalRecord]) -> None:
    """Reject a duplicate or non-positive ``index`` before any build runs
    (spike §3b: "the same 'fail the whole request, don't run half a plan'
    posture ``options.sdf`` validation already takes"). Whole-document exit
    1, unlike a bad ``file``/anchor mismatch on one proposal, which is a
    per-proposal ``"rejected"`` result instead (see
    :func:`_resolve_mutation_proposals`)."""
    seen: set[int] = set()
    for proposal in proposals:
        if proposal.index <= 0 or proposal.index in seen:
            raise FunctionalVerificationError(
                "mutation proposals[].index must be unique and positive "
                f"(got {proposal.index!r})"
            )
        seen.add(proposal.index)


def _validate_mutation_scope(scope: list[str], request_sources: list[str]) -> None:
    """``<proposals>.scope`` must be a subset of `<request>.sources` (spike
    §3b) -- a proposal targeting a file the baseline build never compiles
    cannot be meaningfully tested. Whole-document exit 1, checked before the
    (expensive) baseline ever runs.
    """
    missing = [entry for entry in scope if entry not in request_sources]
    if missing:
        raise FunctionalVerificationError(
            "mutation proposals 'scope' must be a subset of request.sources "
            f"-- not present in request.sources: {', '.join(missing)}"
        )


def _resolve_mutation_proposals(
    proposals: list[_ProposalRecord], scope: list[str], request_dir: str
) -> tuple[_mutation_variants.MutationVariantPlan, dict[int, str]]:
    """Anchor-validate every proposal against the *pristine* scope source
    (the ported seam, spike §1a), splitting them into a
    :class:`~klayout_tools._vendor.mutation_variants.MutationVariantPlan`
    of the ones that resolved cleanly and a ``{index: reason}`` map of the
    ones that did not.

    Deliberately does **not** call the ported module's own
    ``MutationVariantPlan.resolve()`` classmethod, which aborts the entire
    batch on the *first* proposal that fails ``file``-in-scope or anchor
    validation -- booley's own atomic-batch posture (§1a). This module's own
    contract is per-proposal (spike §3b/§3c): a bad ``file`` or an
    anchor mismatch on one proposal is that proposal's own ``"rejected"``
    result, not a whole-document failure -- "the same 'one bad entry doesn't
    poison the batch' posture ``klt drc``'s per-violation reporting already
    takes." So this function drives the ported module's own building blocks
    (``_read_originals``, ``_resolve_one``) directly, one proposal at a time,
    instead.
    """
    allowed = set(scope)
    try:
        originals = _mutation_variants.MutationVariantPlan._read_originals(
            Path(request_dir), allowed
        )
    except _mutation_variants.MutationVariantError as exc:
        # Every `scope` entry is already a `request.sources` member (see
        # `_validate_mutation_scope`), and every `request.sources` entry was
        # already confirmed readable by `_resolve_sources` -- so this should
        # not fire in practice. Kept as a whole-document exit 1 (not a
        # per-proposal rejection) rather than assumed unreachable: an
        # unreadable *scope* file affects every proposal naming it, not one.
        raise FunctionalVerificationError(str(exc)) from exc

    resolved: list[_mutation_variants.ResolvedMutation] = []
    rejected: dict[int, str] = {}
    for proposal in proposals:
        if proposal.file not in allowed:
            rejected[proposal.index] = (
                f"file '{proposal.file}' is outside the declared scope"
            )
            continue
        try:
            resolved.append(
                _mutation_variants.MutationVariantPlan._resolve_one(
                    proposal, originals[proposal.file]
                )
            )
        except _mutation_variants.MutationVariantError as exc:
            rejected[proposal.index] = str(exc)

    plan = _mutation_variants.MutationVariantPlan(
        Path(request_dir), originals, tuple(resolved)
    )
    return plan, rejected


# ---------------------------------------------------------------------------
# `--mutations`: isolated per-mutant build+test, bounded by a klt-level
# subprocess timeout (issue #1592, spike "Open questions")
# ---------------------------------------------------------------------------


def _build_and_test_variant(
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
    env: dict[str, str],
    build_log: str,
    module: str,
    module_dir: str,
    testcase: str | list[str] | None,
    random_seed: int | None,
    test_dir: str,
    results_xml: str,
    test_log: str,
) -> None:
    """``_run_build`` then ``_run_test`` against whatever source bytes are on
    disk right now -- the isolated per-mutant build+test this module runs
    once per resolved proposal, inside :func:`plan.applied(index)
    <klayout_tools._vendor.mutation_variants.MutationVariantPlan.applied>`
    so the mutated bytes are only ever on disk for the duration of this call.

    Always runs inside the forked child process :func:`_run_isolated_variant`
    starts -- never called directly from the parent process -- so that a
    mutation that wedges the simulator (spike §2's mutant #4: an FSM that
    never reaches its terminal count) can be killed outright by the parent
    once :data:`DEFAULT_MUTATION_TIMEOUT_S` elapses, which is not possible
    for a blocking in-process call once cocotb's own ``Runner.test()`` has
    handed control to a synchronous ``subprocess.run()`` with no timeout
    parameter of its own (verified against cocotb 2.0.1's own
    ``cocotb_tools/runner.py``).
    """
    runner_module = _import_runner()
    engine_runner = _run_build(
        runner_module,
        engine=engine,
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        build_dir=build_dir,
        build_args=build_args,
        defines=defines,
        includes=includes,
        timescale=timescale,
        parameters=parameters,
        log_path=build_log,
    )
    _run_test(
        engine_runner,
        engine=engine,
        module=module,
        module_dir=module_dir,
        hdl_toplevel=hdl_toplevel,
        testcase=testcase,
        random_seed=random_seed,
        build_dir=build_dir,
        test_dir=test_dir,
        results_xml=results_xml,
        timescale=timescale,
        parameters=parameters,
        extra_env=env,
        log_path=test_log,
    )


def _mutation_subprocess_entry(kwargs: dict[str, Any], result_queue: Any) -> None:
    """Target of the forked child process :func:`_run_isolated_variant`
    starts. ``os.setpgrp()`` moves the child into its own process group
    *before* it can spawn cocotb's own simulator subprocess, so the parent
    can SIGKILL the whole group -- child plus simulator grandchild -- rather
    than orphaning the simulator process on a timeout kill.
    """
    try:
        os.setpgrp()
    except OSError:  # pragma: no cover - defensive; absent on non-POSIX
        pass
    try:
        _build_and_test_variant(**kwargs)
    except BaseException as exc:  # noqa: BLE001 - reported back, never raised across the process boundary
        try:
            result_queue.put(("error", f"{exc}"))
        except Exception:  # pragma: no cover - defensive: an unpicklable exc
            result_queue.put(("error", f"{type(exc).__name__}"))
        return
    result_queue.put(("ok", None))


def _kill_process_tree(pid: int | None) -> None:
    """SIGKILL ``pid``'s whole process group (see :func:`_mutation_subprocess_entry`),
    falling back to just ``pid`` if the group lookup itself fails. Never
    raises -- called only after a timeout has already been decided; a kill
    that fails because the process already exited on its own is not this
    function's problem to report."""
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_isolated_variant(
    *, timeout_s: float, **build_and_test_kwargs: Any
) -> tuple[bool, tuple[str, str | None]]:
    """Run :func:`_build_and_test_variant` in a forked child process bounded
    by ``timeout_s`` wall-clock seconds -- the ``klt``-level per-mutant
    subprocess timeout (issue #1592, spike "Open questions"), independent of
    and in addition to whatever cycle bound the testbench's own code has.

    Forking (rather than, say, a ``threading.Thread`` with a join timeout)
    is the load-bearing choice: a thread cannot forcibly terminate a
    blocking ``subprocess.run()`` several call frames down inside cocotb's
    own ``Runner.test()``, which has no timeout parameter to begin with
    (this module's own :func:`_build_and_test_variant` docstring). A forked
    child gives this function an OS process-group handle it can SIGKILL
    outright on a timeout -- see :func:`_kill_process_tree`.

    Returns ``(timed_out, (status, detail))``:

    - ``timed_out=True`` -- the child did not finish within ``timeout_s`` and
      was killed. Booley's own rule, ported verbatim (spike §1a): **"a
      timed-out mutant counts as detected because the mutation can wedge the
      design"** -- the caller classifies this as ``"killed"``, never
      ``"rejected"``.
    - ``timed_out=False, ("ok", None)`` -- the isolated build+test ran to
      completion (a failing *regression* is not an error here -- see
      :func:`_run_test`); the caller compares its ``results.xml`` against the
      baseline's own to classify ``"killed"``/``"survived"``.
    - ``timed_out=False, ("error", detail)`` -- the isolated build+test
      raised (a build/elaboration failure, a missing ``results.xml``, or any
      other exception) before producing a trustworthy verdict; the caller
      classifies this ``"rejected"``.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        # POSIX-only by construction (`os.setpgrp`/`os.killpg` above are
        # both POSIX-only too) -- a clear request-time error beats a cryptic
        # platform failure deep inside `multiprocessing`.
        raise FunctionalVerificationError(
            "--mutations requires OS process-fork support (POSIX) to bound "
            "each isolated per-mutant build+test with a klt-level subprocess "
            "timeout -- unavailable on this platform"
        )

    ctx = multiprocessing.get_context("fork")
    result_queue: Any = ctx.Queue()
    process = ctx.Process(
        target=_mutation_subprocess_entry,
        args=(build_and_test_kwargs, result_queue),
    )
    process.start()
    process.join(timeout_s)
    if process.is_alive():
        _kill_process_tree(process.pid)
        process.join(_MUTATION_KILL_GRACE_S)
        return True, ("error", f"timed out after {timeout_s}s")

    try:
        return False, result_queue.get_nowait()
    except _queue_module.Empty:
        return False, (
            "error",
            "isolated build/test process exited without a result "
            f"(exit code {process.exitcode})",
        )


# ---------------------------------------------------------------------------
# `results.xml` parsing -- the only source of truth for the verdict
# ---------------------------------------------------------------------------


def parse_results_xml(results_xml: str) -> list[dict[str, Any]]:
    """Parse a cocotb ``results.xml`` into the contract's ``tests[]`` list.

    Structure (spike section 4, captured live): a ``<testcase>`` with no
    child element is a pass, a ``<failure>`` child is a failure, a
    ``<skipped>`` child is a skip. ``error_type``/``error_message`` are taken
    verbatim from the ``<failure>`` element's own attributes and are present
    only on failing entries.

    Simulated time comes from :func:`_testcase_sim_time_ns`, which reads
    *both* JUnit shapes cocotb has emitted (the 2.0.x ``sim_time_ns``
    attribute and the 2.1.0 ``<properties>`` block) -- see that function.

    Raises :class:`FunctionalVerificationError` if the file is missing or
    unparseable -- either means the run produced no trustworthy evidence.
    """
    if not os.path.isfile(results_xml):
        raise FunctionalVerificationError(
            f"simulation produced no results file '{results_xml}' -- the "
            "regression did not run to completion"
        )
    try:
        tree = ElementTree.parse(results_xml)  # noqa: S314 - cocotb results.xml generated in the verb-owned sim build dir
    except ElementTree.ParseError as exc:
        raise FunctionalVerificationError(
            f"results file '{results_xml}' is not parseable XML: {exc}"
        ) from exc
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not read results file '{results_xml}': {exc}"
        ) from exc

    tests: list[dict[str, Any]] = []
    for testsuite in tree.iter("testsuite"):
        for testcase in testsuite.iter("testcase"):
            entry: dict[str, Any] = {
                "name": testcase.get("name"),
                "status": "passed",
                "sim_time_ns": _testcase_sim_time_ns(testcase),
                "real_time_s": _maybe_float(testcase.get("time")),
            }
            failure = testcase.find("failure")
            skipped = testcase.find("skipped")
            if failure is not None:
                entry["status"] = "failed"
                entry["error_type"] = failure.get("error_type") or failure.get("type")
                entry["error_message"] = (
                    failure.get("error_msg")
                    or failure.get("message")
                    or (failure.text or "").strip()
                    or None
                )
            elif skipped is not None:
                entry["status"] = "skipped"
            tests.append(entry)
    return tests


def _executed_result_counts(
    tests: list[dict[str, Any]], module: str
) -> tuple[int, int, int]:
    """Count parsed outcomes, refusing regressions that verified nothing."""
    if not tests:
        raise FunctionalVerificationError(
            f"testbench module '{module}' registered no tests -- the run "
            "verified nothing (a regression with zero @cocotb.test() "
            "functions is not a pass)"
        )
    passed = sum(1 for test in tests if test["status"] == "passed")
    failed = sum(1 for test in tests if test["status"] == "failed")
    skipped = sum(1 for test in tests if test["status"] == "skipped")
    if passed + failed == 0:
        raise FunctionalVerificationError(
            f"testbench module '{module}': all tests were skipped -- "
            "zero executed tests verified nothing; this regression is not a pass"
        )
    return passed, failed, skipped


def _maybe_float(value: str | None) -> float | None:
    """``float(value)``, or ``None`` when the attribute is absent or not a
    number -- an unparseable timing attribute must never sink a run whose
    pass/fail verdict is otherwise perfectly well defined."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# cocotb's own simulator-time unit tokens, as they appear in the
# `<property name="sim_time_unit" value="..."/>` element of a cocotb >= 2.1.0
# `results.xml`, mapped to the power of ten that converts that unit to
# nanoseconds. `sec` is cocotb's own spelling for seconds; `s` is accepted
# too so a future/alternate spelling still resolves rather than nulling out.
_SIM_TIME_UNIT_NS_EXPONENT: dict[str, int] = {
    "fs": -6,
    "ps": -3,
    "ns": 0,
    "us": 3,
    "ms": 6,
    "s": 9,
    "sec": 9,
}


def _testcase_properties(testcase: ElementTree.Element) -> dict[str, str]:
    """``{name: value}`` for the ``<properties>`` block cocotb >= 2.1.0 hangs
    off each ``<testcase>``.

    Only the testcase's *own* direct ``<properties>/<property>`` children are
    read -- never a ``<testsuite>``-level property (where cocotb 2.0.x put
    ``random_seed``, see :func:`_extract_random_seed_property`) -- so a
    per-suite property can never be mistaken for a per-test one. A property
    missing either attribute is skipped rather than raising.
    """
    properties: dict[str, str] = {}
    for prop in testcase.iterfind("properties/property"):
        name = prop.get("name")
        value = prop.get("value")
        if name is not None and value is not None:
            properties[name] = value
    return properties


def _testcase_sim_time_ns(testcase: ElementTree.Element) -> float | None:
    """Simulated time for one ``<testcase>``, in nanoseconds, across both
    JUnit XML shapes cocotb has emitted (issue #2592).

    cocotb 2.0.x put the value on the element itself::

        <testcase name="test_gcd_known_pairs" time="0.0056" sim_time_ns="520.0" />

    cocotb 2.1.0 restructured its JUnit writer and moved the timing facts into
    a child ``<properties>`` block instead, with the unit reported separately::

        <testcase name="test_gcd_known_pairs" time="0.014">
          <properties>
            <property name="sim_time_unit" value="ns" />
            <property name="sim_time_duration" value="520.0" />
          </properties>
        </testcase>

    The 2.0.x attribute is tried **first** and used unchanged when present, so
    nothing that parses today -- including stored historical ``results.xml``
    evidence -- changes behaviour. Only when the attribute is absent is
    ``sim_time_duration`` read and scaled by the sibling ``sim_time_unit``;
    the unit is never assumed to be ``ns``, since cocotb reports whatever
    unit the run actually used.

    Never raises. A ``<testcase>`` carrying neither the attribute nor a
    usable ``<properties>`` block (a test that never ran, or a unit token
    this mapping does not recognise -- guessing a scale would be worse than
    reporting nothing) degrades to ``None``, exactly as before: a purely
    informational timing field must never sink a run whose pass/fail verdict
    is otherwise perfectly well defined.
    """
    attribute = _maybe_float(testcase.get("sim_time_ns"))
    if attribute is not None:
        return attribute

    properties = _testcase_properties(testcase)
    duration = _maybe_float(properties.get("sim_time_duration"))
    if duration is None:
        return None
    unit = (properties.get("sim_time_unit") or "").strip().lower()
    exponent = _SIM_TIME_UNIT_NS_EXPONENT.get(unit)
    if exponent is None:
        return None
    if exponent == 0:
        return duration
    if exponent > 0:
        return duration * float(10**exponent)
    # Divide rather than multiply by a fractional scale: `520000 / 1000` is
    # exact where `520000 * 1e-3` is not.
    return duration / float(10**-exponent)


def _extract_random_seed_property(results_xml: str) -> int | None:
    """The effective ``random_seed`` cocotb's regression manager used for
    this run, read back out of ``results.xml``'s own
    ``<property name="random_seed" value="...">`` element (spike section 4,
    captured live -- every ``results.xml`` this verb produces carries one).

    This is the authoritative echo for ``environment.random_seed``: cocotb
    always logs the seed it actually used, whether or not
    ``request.options.random_seed`` pinned one explicitly (an unpinned run
    still gets a randomly-generated seed, itself worth knowing so that
    *specific* run can be reproduced later by feeding this value back in as
    ``request.options.random_seed``).

    Never raises -- a missing/unparseable/absent property degrades to
    ``None``, since this purely-informational field must never sink a run
    whose pass/fail verdict (from :func:`parse_results_xml`) is otherwise
    perfectly well defined.
    """
    try:
        tree = ElementTree.parse(results_xml)  # noqa: S314 - cocotb results.xml generated in the verb-owned sim build dir
    except (ElementTree.ParseError, OSError):
        return None
    for prop in tree.iter("property"):
        if prop.get("name") == "random_seed":
            value = prop.get("value")
            if value is None:
                return None
            try:
                return int(value)
            except ValueError:
                return None
    return None


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
    module_dir: str,
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
) -> None:
    """``Runner.test()``, with the simulator's own exit code deliberately
    discarded (spike section 4): a :class:`SystemExit` here means only "the
    simulator process returned nonzero", which was observed to mean anything
    from "one test failed" to "nothing ran". The verdict comes from
    :func:`parse_results_xml` instead, which the caller runs next.

    ``module_dir`` is prepended to ``sys.path`` for the duration of the call
    because ``Runner._set_env`` propagates the *calling* process's ``sys.path``
    to the simulator as ``PYTHONPATH`` (verified live) -- there is no
    ``test_module`` search-path parameter to use instead.

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
    inserted = module_dir not in sys.path
    if inserted:
        sys.path.insert(0, module_dir)
    try:
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
        if inserted:
            try:
                sys.path.remove(module_dir)
            except ValueError:  # pragma: no cover - defensive
                pass


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
    *, test_dir: str, build_dir: str, info_path: str
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
        "info_path": info_path,
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
    """Build the response's ``trace`` block -- ``{"path", "format",
    "size_bytes"}``, the identical shape ``klt wave build``'s own ``trace``
    field already uses (``docs/schemas/wave-build-response.schema.json``),
    so the object this verb hands back can be dropped straight into a
    ``klt wave build`` request's own ``trace`` field unchanged.

    Raises :class:`FunctionalVerificationError` when ``options.trace`` was
    requested but the run produced no waveform file -- a requested-and-
    missing trace would otherwise be indistinguishable from "not requested"
    (``null``), the same posture :func:`collect_coverage` already takes for
    a requested-and-missing ``coverage.dat``.
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


def _apply_sdf_header_normalization(sdf_path: str, output_dir: str) -> str:
    """Issue #1880's always-on first step of
    :func:`run_functional_verification`'s SDF-normalization pipeline: fill
    the empty member of any ``VOLTAGE``/``TEMPERATURE``/``PROCESS`` header
    triple (OpenSTA's ``write_sdf`` writes them as ``min::max``) so
    ``iverilog -T typ`` does not reject the header with ``Chosen value not
    defined``. See :func:`_normalize_sdf_header_triples`.

    Returns ``sdf_path`` unchanged when the header needed nothing (a run on
    such an SDF is byte-identical to before), else the normalized-copy path.
    Nothing is dropped, so no ``environment.sdf.dropped`` class is recorded:
    Icarus selects no delay from these fields. A separate function for the
    same cyclomatic-complexity reason ``_apply_sdf_physical_only_drop``'s
    docstring records.
    """
    normalized = _normalize_sdf_header_triples(sdf_path)
    if normalized is None:
        return sdf_path
    new_path = os.path.join(output_dir, "klt_sdf_header_normalized.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(normalized)
    return new_path


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
    iopath_drop = _drop_sdf_non_interconnect_entries(annotate_source_path)
    if iopath_drop is None:
        return annotate_source_path
    iopath_text, iopath_dropped_count = iopath_drop
    new_path = os.path.join(output_dir, "klt_sdf_interconnect_only.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(iopath_text)
    sdf_pre_dropped_counts[SDF_IOPATH_DROPPED_CLASS] = iopath_dropped_count
    return new_path


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
    edge_drop = _drop_sdf_unsupported_edge_modpath_iopaths(
        annotate_source_path, sources
    )
    if edge_drop is None:
        return annotate_source_path
    edge_text, edge_count, edge_arcs = edge_drop
    new_path = os.path.join(output_dir, "klt_sdf_ifnone_edge_modpath_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(edge_text)
    sdf_pre_dropped_counts[SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS] = edge_count
    sdf_pre_dropped_arcs[SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS] = edge_arcs
    return new_path


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
    bus_drop = _drop_sdf_bit_selected_bus_iopaths(annotate_source_path, sources)
    if bus_drop is None:
        return annotate_source_path
    bus_text, bus_count, bus_arcs = bus_drop
    new_path = os.path.join(output_dir, "klt_sdf_bus_bit_modpath_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(bus_text)
    sdf_pre_dropped_counts[SDF_BUS_BIT_MODPATH_DROPPED_CLASS] = bus_count
    sdf_pre_dropped_arcs[SDF_BUS_BIT_MODPATH_DROPPED_CLASS] = bus_arcs
    return new_path


def _sdf_dropped_report(
    sdf_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, Any]]:
    """``environment.sdf.dropped``: per class, ``{count, reason}`` -- plus
    ``arcs`` for a class that recorded the specific cell arcs it dropped
    (issue #2885's :data:`SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS` and issue
    #2958's :data:`SDF_BUS_BIT_MODPATH_DROPPED_CLASS`; additive,
    absent on every other class)."""
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
    ports: list[tuple[str, int, str]],
    sdf_pre_dropped_counts: dict[str, int],
) -> str:
    """Issue #2363's physical-only-destination SDF-text drop, applied as one
    step of :func:`run_functional_verification`'s SDF-normalization pipeline
    (parallel to the inline #2285 alias-port drop just above its own call
    site): a zero-delay ``INTERCONNECT`` entry from a bit-selected top-level
    input port onto a pin of a *physical-only* instance (a router-inserted
    antenna diode, or any filler/tapcell whose model has no ``specify``
    block and no output pin) cannot be annotated by Icarus at all, so it is
    removed from the SDF text before ``$sdf_annotate`` and counted under
    :data:`SDF_PHYSICAL_ONLY_DROPPED_CLASS` instead of failing the run.

    Returns the path :func:`run_functional_verification` should hand to
    ``$sdf_annotate`` next -- ``annotate_source_path`` unchanged when
    nothing was dropped, or a new normalized-copy path otherwise. The count
    is recorded into ``sdf_pre_dropped_counts`` in place rather than
    returned separately, so this call site is a single expression with no
    branch of its own in ``run_functional_verification`` -- pulled out into
    its own function specifically to keep that already-baselined function's
    cyclomatic complexity (`complexity-baseline.json`) from growing further.
    """
    physical_only_drop = _drop_sdf_zero_delay_physical_only_interconnects(
        annotate_source_path,
        {name for direction, width, name in ports if width and direction == "input"},
        _collect_sdf_physical_only_instances(sources, hdl_toplevel),
    )
    if physical_only_drop is None:
        return annotate_source_path
    physical_only_text, physical_only_count = physical_only_drop
    new_path = os.path.join(output_dir, "klt_sdf_physical_only_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(physical_only_text)
    sdf_pre_dropped_counts[SDF_PHYSICAL_ONLY_DROPPED_CLASS] = physical_only_count
    return new_path


def _apply_sdf_bus_port_source_drop(
    annotate_source_path: str,
    output_dir: str,
    ports: list[tuple[str, str, str]],
    sdf_pre_dropped_counts: dict[str, int],
) -> str:
    """Issue #2984's zero-delay bit-selected-input-source SDF-text drop, the
    sibling of :func:`_apply_sdf_physical_only_drop` and kept as its own
    function for the same reason (no new branch in
    :func:`run_functional_verification`). Runs after the physical-only pass,
    so entries that pass claimed keep their class and are not double counted.
    Counts under :data:`SDF_BUS_PORT_SOURCE_DROPPED_CLASS`.

    Returns ``annotate_source_path`` unchanged when nothing was dropped, or
    a new normalized-copy path otherwise.
    """
    drop = _drop_sdf_zero_delay_bus_port_source_interconnects(
        annotate_source_path,
        {name for direction, width, name in ports if width and direction == "input"},
    )
    if drop is None:
        return annotate_source_path
    text, count = drop
    new_path = os.path.join(output_dir, "klt_sdf_bus_port_source_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    sdf_pre_dropped_counts[SDF_BUS_PORT_SOURCE_DROPPED_CLASS] = count
    return new_path


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


VERILOG_TOP_FIELDS = (
    "verilog_top",
    "pass_marker",
    "fail_marker",
    "dut_instance",
)

#: ``options`` keys the Verilog-top path cannot honor (each depends on cocotb
#: or on a per-test result stream), mapped to the value that means "unset".
_VERILOG_TOP_REJECTED_OPTIONS = (
    ("coverage", False),
    ("initial_state", None),
    ("trace", False),
    ("random_seed", None),
    ("timescale", None),
)


def _verilog_top_fields(testbench: dict[str, Any]) -> dict[str, str | None]:
    """Shape-check the ``verilog_top`` testbench fields."""
    if "module" in testbench:
        raise FunctionalVerificationError(
            "request.testbench must give exactly one of 'module' (cocotb) or "
            "'verilog_top' (Verilog bench), not both"
        )
    unknown = sorted(
        set(testbench) - set(VERILOG_TOP_FIELDS) - {"search_path", "testcase"}
    )
    if unknown:
        raise FunctionalVerificationError(
            "request.testbench has unknown field(s) for verilog_top: "
            + ", ".join(unknown)
        )
    for cocotb_only in ("search_path", "testcase"):
        if testbench.get(cocotb_only) is not None:
            raise FunctionalVerificationError(
                f"request.testbench.{cocotb_only} applies to a cocotb "
                "testbench and cannot be combined with verilog_top"
            )
    resolved: dict[str, str | None] = {}
    for name in VERILOG_TOP_FIELDS:
        value = testbench.get(name)
        if value is None and name == "verilog_top":
            raise FunctionalVerificationError(
                "request.testbench.verilog_top must be a non-empty string"
            )
        if value is not None and (not isinstance(value, str) or not value):
            raise FunctionalVerificationError(
                f"request.testbench.{name} must be a non-empty string when given"
            )
        resolved[name] = value
    return resolved


def _reject_verilog_top_options(engine: str, options: Any) -> None:
    if engine != "icarus":
        raise FunctionalVerificationError(
            f"testbench.verilog_top requires engine 'icarus' -- engine "
            f"'{engine}' has no Verilog-bench path"
        )
    opts = options if isinstance(options, dict) else {}
    for key, unset in _VERILOG_TOP_REJECTED_OPTIONS:
        if opts.get(key, unset) != unset:
            raise FunctionalVerificationError(
                f"options.{key} cannot be combined with testbench.verilog_top "
                "-- not supported on the Verilog-bench path (it has no cocotb "
                "run to apply it to)"
            )


def _require_module_declared(top: str, sources: list[str]) -> None:
    module_re = re.compile(rf"^\s*module\s+{re.escape(top)}\b", re.MULTILINE)
    for source in sources:
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                if module_re.search(handle.read()):
                    return
        except OSError:
            continue
    raise FunctionalVerificationError(
        f"testbench.verilog_top '{top}' is not a module declared in request.sources"
    )


def _resolve_verilog_top(
    testbench: Any, sources: list[str], engine: str, options: Any
) -> dict[str, str | None] | None:
    """Validate the Verilog-bench form of ``request.testbench`` (issue #4083).

    Returns ``None`` when ``testbench.verilog_top`` is absent (the ordinary
    cocotb path), else ``{"verilog_top", "pass_marker", "fail_marker",
    "dut_instance"}``. ``testbench.module`` and ``testbench.verilog_top`` are
    mutually exclusive. The bench's verdict is derived without cocotb (see
    :func:`_run_verilog_top`), so every option that needs cocotb is rejected
    here rather than silently ignored.
    """
    if not isinstance(testbench, dict) or "verilog_top" not in testbench:
        return None
    resolved = _verilog_top_fields(testbench)
    _reject_verilog_top_options(engine, options)
    top = resolved["verilog_top"]
    assert top is not None
    _require_module_declared(top, sources)
    return resolved


def _resolve_testbench_target(
    verilog_tb: dict[str, str | None] | None, testbench: Any, request_dir: str
) -> tuple[str, str, str | list[str] | None]:
    """``(module, module_dir, testcase)`` for either testbench form."""
    if verilog_tb is None:
        return _resolve_testbench(testbench, request_dir)
    top = verilog_tb["verilog_top"]
    assert top is not None
    return top, request_dir, None


def _sdf_annotate_scope(verilog_tb: dict[str, str | None] | None) -> str:
    """Hierarchical scope the generated ``$sdf_annotate`` shim targets."""
    if verilog_tb is None:
        return f"{SDF_WRAPPER_MODULE}.{SDF_WRAPPER_DUT_INSTANCE}"
    dut_instance = verilog_tb["dut_instance"]
    assert dut_instance is not None
    return dut_instance


def _sdf_extend_sources(
    verilog_tb: dict[str, str | None] | None,
    sources: list[str],
    *,
    wrapper_path: str,
    shim_path: str,
    hdl_toplevel: str,
    ports: Any,
) -> tuple[list[str], str]:
    """Add the SDF scaffolding to ``sources``; return it with the build top.

    A Verilog bench already nests the DUT one level down, so there is no
    pass-through wrapper: the bench itself is the second root alongside the
    shim.
    """
    if verilog_tb is not None:
        top = verilog_tb["verilog_top"]
        assert top is not None
        return [*sources, shim_path], top
    _write_sdf_dut_wrapper(wrapper_path, hdl_toplevel=hdl_toplevel, ports=ports)
    return [*sources, wrapper_path, shim_path], SDF_WRAPPER_MODULE


def _check_verilog_top_sdf(
    verilog_tb: dict[str, str | None] | None, sdf: Any, parameters: Any
) -> None:
    """``testbench.dut_instance`` is the SDF scope, so it is required with
    ``options.sdf`` and meaningless without it. ``request.parameters`` is
    rejected: it would have to target the bench, not the DUT. A no-op on the
    cocotb path (``verilog_tb is None``)."""
    if verilog_tb is None:
        return
    if parameters:
        raise FunctionalVerificationError(
            "request.parameters cannot be combined with testbench.verilog_top"
        )
    if sdf is not None and not verilog_tb["dut_instance"]:
        raise FunctionalVerificationError(
            "options.sdf with testbench.verilog_top requires "
            "testbench.dut_instance (the DUT's hierarchical path inside the "
            "bench, e.g. 'tb.dut') -- it is the $sdf_annotate scope"
        )
    if sdf is None and verilog_tb["dut_instance"]:
        raise FunctionalVerificationError(
            "testbench.dut_instance only applies together with options.sdf"
        )


def _verilog_top_sdf_gate(
    sdf: dict[str, str] | None,
    engine: str,
    build_log: str,
    test_log: str,
    sdf_pre_dropped_counts: dict[str, int],
) -> dict[str, int]:
    """Raise on SDF back-annotation diagnostics; return the dropped counts."""
    if sdf is None:
        return {}
    diagnostics, scanned = _scan_sdf_diagnostics(build_log, test_log)
    if diagnostics:
        shown = " | ".join(diagnostics[:5])
        more = f" (+{len(diagnostics) - 5} more)" if len(diagnostics) > 5 else ""
        raise FunctionalVerificationError(
            f"SDF back-annotation of '{sdf['file']}' did not fully apply: "
            f"{len(diagnostics)} diagnostic(s) in the {engine} transcript "
            f"-- {shown}{more}. The simulator exits 0 after skipping an "
            "annotation it cannot apply, so this run's verdict would have "
            "been a zero-delay one reported as if it were annotated."
        )
    return {**sdf_pre_dropped_counts, **scanned}


def _verilog_top_error(
    returncode: int, transcript: str, pass_marker: str | None, fail_marker: str | None
) -> str | None:
    """Why the bench run failed, or ``None`` when it passed."""
    if returncode != 0:
        return f"vvp exited with status {returncode}"
    if fail_marker and fail_marker in transcript:
        return f"fail_marker {fail_marker!r} found in the transcript"
    if pass_marker and pass_marker not in transcript:
        return (
            f"pass_marker {pass_marker!r} not found in the transcript "
            "(vvp exited 0 without reporting a pass)"
        )
    return None


def _run_verilog_top(
    verilog_tb: dict[str, str | None],
    *,
    engine: str,
    hdl_toplevel: str,
    sources: list[str],
    build_args: list[str],
    defines: dict[str, str | None],
    includes: list[str],
    build_dir: str,
    build_log: str,
    test_log: str,
    sdf: dict[str, str] | None,
    sdf_pre_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
    sdf_renamed: list[dict[str, str]],
    source_defines: dict[str, Any],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the bench with ``iverilog``, run it with ``vvp`` and assemble the
    ordinary report (issue #4083). No cocotb is involved.

    The verdict never trusts an exit code alone: the run passes only when
    ``vvp`` exits 0, no ``fail_marker`` substring appears in the transcript
    and, when ``pass_marker`` is declared, that substring does appear. A
    ``$fatal`` exits nonzero and so fails. A bench that exits 0 without ever
    printing its ``pass_marker`` fails, so a bench that silently did nothing
    cannot read as green. The SDF diagnostic gate runs first, exactly as on
    the cocotb path.
    """
    top = verilog_tb["verilog_top"]
    pass_marker = verilog_tb["pass_marker"]
    fail_marker = verilog_tb["fail_marker"]
    assert top is not None

    os.makedirs(build_dir, exist_ok=True)
    vvp_path = os.path.join(build_dir, "sim.vvp")
    cmd = ["iverilog", "-o", vvp_path, "-s", top, "-g2012"]
    cmd += [f"-I{path}" for path in includes]
    for key, value in defines.items():
        cmd.append(f"-D{key}" if value is None else f"-D{key}={value}")
    cmd += build_args + sources
    try:
        with open(build_log, "w", encoding="utf-8") as handle:
            built = subprocess.run(
                cmd, stdout=handle, stderr=subprocess.STDOUT, check=False
            )
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch the icarus build: {exc}"
        ) from exc
    if built.returncode != 0:
        raise FunctionalVerificationError(
            f"icarus build/elaboration failed (exit {built.returncode}): "
            f"{_log_tail(build_log)}"
        )

    try:
        with open(test_log, "w", encoding="utf-8") as handle:
            ran = subprocess.run(
                ["vvp", vvp_path],
                stdout=handle,
                stderr=subprocess.STDOUT,
                cwd=build_dir,
                check=False,
            )
    except OSError as exc:
        raise FunctionalVerificationError(f"could not launch vvp: {exc}") from exc
    with open(test_log, encoding="utf-8", errors="replace") as handle:
        transcript = handle.read()

    sdf_dropped_counts = _verilog_top_sdf_gate(
        sdf, engine, build_log, test_log, sdf_pre_dropped_counts
    )
    error_message = _verilog_top_error(
        ran.returncode, transcript, pass_marker, fail_marker
    )
    entry: dict[str, Any] = {
        "name": top,
        "status": "failed" if error_message else "passed",
        "sim_time_ns": None,
        "real_time_s": None,
    }
    if error_message:
        entry["error_type"] = "VerilogBenchFailure"
        entry["error_message"] = error_message
    failed_count = 1 if error_message else 0

    report = _Report(
        {
            "schema_version": SCHEMA_VERSION,
            "engine": engine,
            "hdl_toplevel": hdl_toplevel,
            "testbench": top,
            "status": "fail" if failed_count else "pass",
            "test_count": 1,
            "passed_count": 1 - failed_count,
            "failed_count": failed_count,
            "skipped_count": 0,
            "tests": [entry],
            "coverage": None,
            "trace": None,
            "environment": {
                "engine": engine,
                "engine_version": _engine_version(engine),
                "tools": tools,
                "cocotb_version": None,
                "results_xml": _path_envelope(None),
                "test_log": _path_envelope(test_log),
                "build_log": _path_envelope(build_log),
                "random_seed": None,
                "source_defines": source_defines,
                "initial_state": None,
                "sdf": (
                    None
                    if sdf is None
                    else {
                        "file": sdf["file"],
                        "corner": sdf["corner"],
                        "entries": sdf["entries"],
                        "annotated": True,
                        "partial": bool(sdf_dropped_counts),
                        "dropped": _sdf_dropped_report(
                            sdf_dropped_counts, sdf_pre_dropped_arcs
                        ),
                        **_sdf_renamed_report(sdf_renamed),
                    }
                ),
            },
        }
    )
    report.local_paths = {
        "results_xml": None,
        "test_log": test_log,
        "build_log": build_log,
    }
    return report


def _reject_verilog_top_mutations(testbench: Any) -> None:
    if isinstance(testbench, dict) and "verilog_top" in testbench:
        raise FunctionalVerificationError(
            "testbench.verilog_top cannot be combined with --mutations -- "
            "not yet designed (the Verilog-top path has no per-test results "
            "to attribute a kill to)"
        )


def _resolve_output_dir(request_dir: str, output_dir: str | None) -> str:
    """Directory run artifacts are written to: ``output_dir`` (relative paths
    resolve against the cwd, not the request directory) or, by default,
    ``<request_dir>/.klt/functional-verification``.
    """
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
    request: str, output_dir: str | None = None
) -> dict[str, Any]:
    """Run the cocotb regression declared by ``request``.

    ``request`` accepts the same three forms every other ``klt`` request
    argument does (see :func:`load_request_arg`): a path to a request JSON
    file, ``"-"`` to read the request from stdin, or an inline JSON object
    string.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/functional-verification.md``). Raises
    :class:`FunctionalVerificationError` for anything that prevents a
    trustworthy verdict from being produced at all -- a failing *test* is
    never an error, it is ``status: "fail"`` in the returned report (which
    the CLI turns into exit code 3).

    Artifacts (the build directory, the raw ``results.xml``, the engine
    transcripts, and any coverage ``.info``) are written to
    ``.klt/functional-verification/`` next to the request file -- the same
    "next to the input" default ``klt sim``/``klt synthesize`` already use --
    and kept, never deleted.
    """
    request_doc, request_dir = load_request_arg(request)
    output_dir = _resolve_output_dir(request_dir, output_dir)

    engine = _resolve_engine(request_doc)

    sources = _resolve_sources(request_doc["sources"], request_dir)

    hdl_toplevel = request_doc["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise FunctionalVerificationError(
            "request.hdl_toplevel must be a non-empty string"
        )

    verilog_tb = _resolve_verilog_top(
        request_doc["testbench"], sources, engine, request_doc.get("options")
    )
    module, module_dir, testcase = _resolve_testbench_target(
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
    _check_verilog_top_sdf(verilog_tb, sdf, request_doc.get("parameters"))
    initial_state = _resolve_initial_state(
        request_doc.get("options"), engine, request_dir
    )
    if initial_state is not None and sdf is not None:
        raise FunctionalVerificationError(
            "options.initial_state cannot currently be combined with "
            "options.sdf -- not yet designed (the SDF build elaborates a "
            "generated wrapper as the root and scans a single transcript)"
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
        # Issue #3740: an INTERCONNECT endpoint naming an instance the
        # netlist does not have aborts vvp the same way -- caught here too.
        _reject_sdf_unresolvable_interconnect_instances(
            sdf["file"], sources, hdl_toplevel
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
        # Issue #1619: a bit-selected top-level-port INTERCONNECT entry
        # (any real design's bus ports produce these) can poison a sibling
        # INTERCONNECT entry on its own net within one $sdf_annotate call.
        # Deferring those entries to their own, later call (see the module
        # comment above :func:`_split_sdf_bus_port_interconnects` for which
        # of the two confirmed mechanisms this actually fixes) is a no-op
        # for a design with only scalar top-level ports (#1069's own case)
        # -- `sdf_paths` stays a single-element list in that case.
        # Issue #2285: an `INTERCONNECT` entry whose destination is a
        # top-level port bit the netlist drives through an `assign` alias
        # (a P&R backend's tie-cell shape, `assign uio_oe[0] = net0;`)
        # cannot be annotated by Icarus at all -- and cannot be made
        # annotatable by rewriting the endpoint either (verified live; see
        # the module comment above
        # :func:`_drop_sdf_zero_delay_alias_port_interconnects`). When such
        # an entry is zero-delay at every corner, dropping it cannot change
        # any simulated timing, so it is removed from the SDF text here and
        # counted in `environment.sdf.dropped` instead of failing the run.
        # A non-zero-delay entry is left in place and still fails the
        # diagnostic gate loudly.
        alias_port_bits = _collect_sdf_alias_port_bits(sources, hdl_toplevel)
        alias_drop = _drop_sdf_zero_delay_alias_port_interconnects(
            annotate_source_path, alias_port_bits
        )
        if alias_drop is not None:
            alias_text, alias_dropped_count = alias_drop
            annotate_source_path = os.path.join(output_dir, "klt_sdf_alias_dropped.sdf")
            with open(annotate_source_path, "w", encoding="utf-8") as handle:
                handle.write(alias_text)
            sdf_pre_dropped_counts[SDF_ALIAS_PORT_DROPPED_CLASS] = alias_dropped_count

        vector_ports = {name for _, width, name in ports if width}

        # Issue #2363: an `INTERCONNECT` entry from a bit-selected top-level
        # input port onto a pin of a *physical-only* instance -- a
        # router-inserted antenna diode, or any filler/tapcell whose model
        # is timing-empty -- is shape (b) of the #1619/#1857 spike and
        # cannot be annotated by Icarus at all. Unlike the general shape (b)
        # case (which stays failing, pinned by
        # `test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
        # unresolvable`), this destination's elaborated model carries no
        # `specify` block and no output pin, so there is no modpath the
        # entry could ever annotate. When such an entry is zero-delay at
        # every corner, dropping it cannot change any simulated timing, so
        # it is removed here and counted in `environment.sdf.dropped`
        # instead of failing the run. A non-zero-delay entry is left in
        # place and still fails the diagnostic gate loudly.
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

        shim_path = os.path.join(output_dir, f"{SDF_ANNOTATE_MODULE}.v")
        _write_sdf_annotate_shim(
            shim_path,
            sdf_paths=sdf_paths,
            scope=_sdf_annotate_scope(verilog_tb),
        )
        sources, build_hdl_toplevel = _sdf_extend_sources(
            verilog_tb,
            sources,
            wrapper_path=wrapper_path,
            shim_path=shim_path,
            hdl_toplevel=hdl_toplevel,
            ports=ports,
        )
        build_args += [*SDF_BUILD_ARGS, "-T", sdf["corner"]]
    initial_state_info: dict[str, Any] = {}
    if initial_state is not None:
        shim_path, initial_state_info = _write_initial_state_shim(
            initial_state,
            sources=sources,
            hdl_toplevel=hdl_toplevel,
            output_dir=output_dir,
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

    if verilog_tb is not None:
        return _run_verilog_top(
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
        )

    runner_module = _import_runner()
    _check_cocotb_abi_compatibility()
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
            module_dir=module_dir,
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

    sdf_dropped_counts: dict[str, int] = {}
    if sdf is not None:
        # The hard gate (spike §3.3): every SDF failure mode -- unopenable
        # file, unmatched instance, unmatched IOPATH, INTERCONNECT without
        # `-ginterconnect` -- is *non-fatal*. Icarus prints a line, drops
        # that annotation, and `vvp` exits 0; the regression then reports a
        # perfectly clean PASS derived from a zero-delay run. Scanning both
        # transcripts is the only signal there is, and it runs before the
        # verdict is parsed so an annotation failure is reported as one
        # rather than as a (misleadingly green) result.
        diagnostics, scanned_dropped_counts = _scan_sdf_diagnostics(build_log, test_log)
        # The two dropped-class sources are disjoint by construction: the
        # transcript scan counts diagnostic *lines* Icarus emitted, while
        # `sdf_pre_dropped_counts` counts SDF *entries* removed before the
        # run (issue #2285), for which Icarus emits nothing at all.
        sdf_dropped_counts = {**sdf_pre_dropped_counts, **scanned_dropped_counts}
        if diagnostics:
            shown = " | ".join(diagnostics[:5])
            more = f" (+{len(diagnostics) - 5} more)" if len(diagnostics) > 5 else ""
            raise FunctionalVerificationError(
                f"SDF back-annotation of '{sdf['file']}' did not fully apply: "
                f"{len(diagnostics)} diagnostic(s) in the {engine} transcript "
                f"-- {shown}{more}. The simulator exits 0 after skipping an "
                "annotation it cannot apply, so this run's verdict would have "
                "been a zero-delay one reported as if it were annotated."
            )

    tests, (passed_count, failed_count, skipped_count), run_reports = (
        _collect_run_results(
            run_specs, engine=engine, module=module, build_log=build_log
        )
    )

    coverage = None
    if coverage_requested:
        coverage = collect_coverage(
            test_dir=output_dir,
            build_dir=build_dir,
            info_path=os.path.join(output_dir, "coverage.info"),
        )

    # Additive (Epic #1585 Phase 3, issue #1845): `null` on a run that did
    # not ask for a trace, an object -- `{"path", "format", "size_bytes"}`,
    # the identical shape `klt wave build`'s own `trace` field already uses
    # -- on one that did. Resolved *after* the verdict/coverage above so a
    # missing waveform (options.trace requested but unproducible) is never
    # mistaken for the run's own pass/fail outcome.
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
                "results_xml": _path_envelope(results_xml),
                # Additive (issue #3013): the simulator/cocotb console transcript
                # and the build transcript the run wrote, named by path (never
                # inlined) like `results_xml`.
                "test_log": _path_envelope(test_log),
                "build_log": _path_envelope(build_log),
                "random_seed": _extract_random_seed_property(results_xml),
                # Additive (issue #3290): `options.source_defines` keyed by the
                # request.sources entry as resolved to an absolute path; `{}`
                # when absent.
                "source_defines": source_defines,
                # Additive (issue #4338): `request.env` as given; `{}` when absent.
                "env": env,
                # Additive (issue #2998): `null` unless the request set
                # `options.initial_state`. `seeds` echoes the effective seeds
                # (empty for mode "x"); `runs` has one entry per seed.
                "initial_state": _initial_state_echo(
                    initial_state, initial_state_info, run_reports
                ),
                # Additive (issue #1002, spike §4.2 item 5): `null` on an
                # ordinary run, an object on an SDF-annotated one -- so a caller
                # can tell an annotated run from an unannotated one *from the
                # JSON*, without reading a transcript. `annotated: true` is
                # load-bearing rather than redundant: reaching this point at all
                # means the diagnostic scan above found nothing *actionable*,
                # i.e. every non-benign diagnostic class applied cleanly.
                #
                # `annotated: true` alone cannot distinguish "every delay and
                # every timing check applied" from "every IOPATH applied and
                # every TIMINGCHECK section was dropped" -- the normal case on
                # Icarus, which has no SDF TIMINGCHECK support at all (issue
                # #1102). `partial`/`dropped` make that distinction
                # machine-readable: `partial` is true whenever any benign class
                # was filtered out of the diagnostic gate above, and `dropped`
                # names each such class with the count of diagnostic lines
                # filtered and a human-readable reason
                # (:data:`SDF_BENIGN_DIAGNOSTIC_REASONS`). An ordinary fully
                # -clean run reports `partial: false` and `dropped: {}`.
                "sdf": (
                    None
                    if sdf is None
                    else {
                        "file": sdf["file"],
                        "corner": sdf["corner"],
                        # Additive (issue #2364): echoes the request's entry
                        # population -- "all" (every delay entry) or
                        # "interconnect" (net delays only, every non-INTERCONNECT
                        # delay entry dropped and counted in `dropped`). Absent
                        # on no older reader's contract: the field is new, and a
                        # consumer that ignores unknown keys (the JSON contract's
                        # additive rule) is unaffected.
                        "entries": sdf["entries"],
                        "annotated": True,
                        "partial": bool(sdf_dropped_counts),
                        "dropped": _sdf_dropped_report(
                            sdf_dropped_counts, sdf_pre_dropped_arcs
                        ),
                        # Additive (issue #2897): `{from, to}` per escaped-
                        # divider instance renamed for annotation.
                        **_sdf_renamed_report(sdf_renamed),
                    }
                ),
            },
        }
    )
    report.local_paths = {
        "results_xml": results_xml,
        "test_log": test_log,
        "build_log": build_log,
    }
    return report


# ---------------------------------------------------------------------------
# `--mutations` entry point (issue #1592, spike section 3)
# ---------------------------------------------------------------------------


def _first_new_failure(
    baseline_tests: list[dict[str, Any]], mutant_tests: list[dict[str, Any]]
) -> str | None:
    """The first ``@cocotb.test()`` name in ``mutant_tests`` that failed
    where the baseline did not (spike §3c's ``first_killing_test``), or
    ``None`` if every mutant failure (if any) also failed on the baseline.

    The baseline-must-pass gate (:func:`run_functional_verification_with_mutations`)
    means ``baseline_tests`` never actually contains a ``"failed"`` entry by
    the time this is called -- so in practice this reduces to "the first
    mutant test that failed at all" -- but the lookup is written generally
    rather than assumed, since nothing about this function's own contract
    depends on that invariant holding.
    """
    baseline_status_by_name = {test["name"]: test["status"] for test in baseline_tests}
    for test in mutant_tests:
        baseline_status = baseline_status_by_name.get(test["name"])
        if test["status"] == "failed" and baseline_status != "failed":
            return test["name"]
    return None


def _write_mutant_combined_log(
    combined_path: str, build_log: str, test_log: str
) -> None:
    """Concatenate one mutant's build+test transcripts into the single
    ``log_path`` artifact the response references (spike §3c) -- `klt`'s own
    choice, not part of the ported seam: the baseline contract already keeps
    ``build_<engine>.log``/``test_<engine>.log`` as two separate files, but
    ``mutation_testing.results[].log_path`` is documented as one path per
    mutant, so this writes a single, clearly-labelled concatenation of both
    (the per-mutant ``build_<engine>.log``/``test_<engine>.log`` themselves
    are also kept on disk, unreferenced by the JSON, for anyone who wants
    them individually).

    Never raises -- a missing/unreadable per-step transcript degrades to
    "leave it out of the combined log", the same posture :func:`_log_tail`
    already takes toward a captured transcript it cannot read.
    """
    parts: list[str] = []
    for label, path in (("build", build_log), ("test", test_log)):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        parts.append(f"--- {label} log ({path}) ---\n{content}")
    try:
        with open(combined_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(parts))
    except OSError:  # pragma: no cover - defensive, mirrors _log_tail's own posture
        pass


def run_functional_verification_with_mutations(
    request: str, proposals_path: str, output_dir: str | None = None
) -> dict[str, Any]:
    """Run ``klt functional-verification --mutations`` end to end (issue
    #1592, spike section 3): the unmodified baseline run from
    :func:`run_functional_verification`, gated by the baseline-must-pass
    check (spike §3d), followed by one isolated, timeout-bounded build+test
    per resolved proposal in ``proposals_path`` (spike §1a's ported seam),
    classified ``killed``/``survived``/``rejected`` (spike §1a/§3c) into the
    additive ``mutation_testing`` response block (spike §3c).

    Raises :class:`FunctionalVerificationError` (CLI exit 1) for every
    whole-run failure mode: anything the base contract already raises for
    (bad request, missing sources, ...), a malformed ``<proposals>``
    document, a ``<proposals>.scope`` entry outside ``request.sources``,
    ``options.coverage``/``options.sdf`` combined with ``--mutations``
    (neither is designed yet -- see "Out of scope" in
    ``docs/cli/functional-verification.md``'s "`--mutations`" section), the
    baseline itself failing (spike §3d), or every proposal ending up
    ``"rejected"`` (``valid_count == 0`` with ``proposal_count > 0`` --
    mirrors the base contract's own "a regression that registered zero
    tests is exit 1, not a vacuous pass").

    A successful return is always a baseline "pass" (`status: "pass"`) --
    the returned dict is the ordinary :func:`run_functional_verification`
    response, unmodified, plus the additive ``mutation_testing`` block. The
    CLI (``cli/functional_verification_cmd.py``) derives exit 3 from
    ``mutation_testing.survived_count > 0``, exit 0 otherwise -- see this
    module's own docstring's "Exit codes" table replicated in
    ``docs/cli/functional-verification.md``.
    """
    # Cheap, whole-document validation first -- before the (expensive)
    # baseline ever runs. Order matters: a malformed proposals document or
    # an out-of-scope entry should never cost a full build+test cycle to
    # discover.
    scope, proposal_records = load_mutation_proposals(proposals_path)
    _validate_proposal_indices(proposal_records)

    request_doc, request_dir = load_request_arg(request)
    _reject_verilog_top_mutations(request_doc.get("testbench"))
    raw_sources = request_doc.get("sources")
    _validate_mutation_scope(
        scope, raw_sources if isinstance(raw_sources, list) else []
    )

    engine = request_doc.get("engine", DEFAULT_ENGINE)
    if engine not in SUPPORTED_ENGINES:
        raise FunctionalVerificationError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
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
    if trace_requested:
        # Also not designed (issue #1845/Epic #1585 Phase 3 only wires
        # `options.trace` into the base contract): each isolated per-mutant
        # variant (:func:`_run_isolated_variant`) builds in its own
        # throwaway `build_dir` this function never surfaces, so there is no
        # single trace artifact a `mutation_testing` response block could
        # point at -- a request error is safer than silently tracing only
        # the baseline (or dropping the flag) with no signal either way.
        raise FunctionalVerificationError(
            "options.trace cannot currently be combined with --mutations -- "
            "not yet designed (Epic #1585 Phase 3 wires options.trace into "
            "the base contract only)"
        )
    if coverage_requested:
        # Explicitly out of scope (issue #1592, per the spike's "Open
        # questions"): the N+1-multiplied Verilator cost of coverage-per-
        # mutant has not been measured, so this is a clean request error
        # rather than a silently-ignored or silently-wrong combination.
        raise FunctionalVerificationError(
            "options.coverage cannot currently be combined with --mutations "
            "-- not yet designed (docs/design/mutation-testing-spike.md, "
            "'Open questions')"
        )
    _reject_initial_state_with_mutations(
        request_doc.get("options"), engine, request_dir
    )
    if sdf is not None:
        # Also not designed (not explicitly listed in the issue's own "Out
        # of scope", but the same reasoning applies): options.sdf swaps in a
        # generated wrapper module as the elaboration root and adds extra
        # generated source files that this function's own isolated per-
        # mutant build does not replicate, so silently running it would
        # produce a build that diverges from the baseline's own in a way
        # nothing here would catch -- a request error is safer than a
        # silently-wrong mutant build.
        raise FunctionalVerificationError(
            "options.sdf cannot currently be combined with --mutations -- "
            "not yet designed (docs/design/mutation-testing-spike.md, "
            "'Open questions')"
        )

    sources = _resolve_sources(request_doc["sources"], request_dir)
    hdl_toplevel = request_doc["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise FunctionalVerificationError(
            "request.hdl_toplevel must be a non-empty string"
        )
    module, module_dir, testcase = _resolve_testbench(
        request_doc["testbench"], request_dir
    )
    parameters = _resolve_parameters(request_doc.get("parameters"))
    env = _resolve_env(request_doc.get("env"))
    _resolve_requires_tools(request_doc.get("requires_tools"), env)
    build_args = list(user_build_args)
    source_defines = _resolve_source_defines(
        request_doc.get("options"), sources, request_dir
    )

    output_dir = _resolve_output_dir(request_dir, output_dir)
    mutants_dir = os.path.join(output_dir, "mutants")
    try:
        os.makedirs(mutants_dir, exist_ok=True)
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not create output directory '{mutants_dir}': {exc}"
        ) from exc

    # The baseline-must-pass gate (spike §3d): the exact same request the
    # caller already used for an ordinary (non-mutation) run, reused
    # unchanged -- see `docs/design/mutation-testing-spike.md` §3a.
    baseline = run_functional_verification(request, output_dir)
    if baseline["status"] != "pass":
        failing_names = ", ".join(
            test["name"] for test in baseline["tests"] if test["status"] == "failed"
        )
        raise FunctionalVerificationError(
            "the baseline run itself failed ({}/{} tests failed{}) -- "
            "mutation testing aborted before running any proposal: "
            "comparing mutant behavior against an already-broken baseline "
            "cannot distinguish 'the mutation broke it' from 'it was "
            "already broken' (docs/design/mutation-testing-spike.md "
            "section 3d)".format(
                baseline["failed_count"],
                baseline["test_count"],
                f": {failing_names}" if failing_names else "",
            )
        )
    baseline_tests = baseline["tests"]

    plan, doc_rejections = _resolve_mutation_proposals(
        proposal_records, scope, request_dir
    )

    results: list[dict[str, Any]] = []
    rejected_count = 0
    killed_count = 0
    survived_count = 0

    for record in proposal_records:
        index = record.index
        base_result = {
            "index": index,
            "category": record.category,
            "file": record.file,
            "line": record.line,
        }

        if index in doc_rejections:
            results.append(
                {
                    **base_result,
                    "status": "rejected",
                    "reason": doc_rejections[index],
                    "first_killing_test": None,
                    "log_path": _path_envelope(None),
                }
            )
            rejected_count += 1
            continue

        mutant_dir = os.path.join(mutants_dir, f"mutant_{index}")
        try:
            os.makedirs(mutant_dir, exist_ok=True)
        except OSError as exc:
            raise FunctionalVerificationError(
                f"could not create mutant directory '{mutant_dir}': {exc}"
            ) from exc
        build_dir = os.path.join(mutant_dir, f"sim_build_{engine}")
        mutant_results_xml = os.path.join(mutant_dir, f"results_{engine}.xml")
        build_log = os.path.join(mutant_dir, f"build_{engine}.log")
        test_log = os.path.join(mutant_dir, f"test_{engine}.log")
        combined_log = os.path.join(mutants_dir, f"mutant_{index}.log")

        # Issue #3290: the wrappers `include the (mutated-in-place) source by
        # absolute path, so they are generated per mutant directory.
        variant_sources, scoped_include_dirs = _write_scoped_define_wrappers(
            sources, source_defines, mutant_dir
        )

        with plan.applied(index):
            timed_out, (status_kind, detail) = _run_isolated_variant(
                timeout_s=DEFAULT_MUTATION_TIMEOUT_S,
                engine=engine,
                sources=variant_sources,
                hdl_toplevel=hdl_toplevel,
                build_dir=build_dir,
                build_args=build_args,
                defines=defines,
                includes=[*includes, *scoped_include_dirs],
                timescale=timescale,
                parameters=parameters,
                env=env,
                build_log=build_log,
                module=module,
                module_dir=module_dir,
                testcase=testcase,
                random_seed=random_seed,
                test_dir=mutant_dir,
                results_xml=mutant_results_xml,
                test_log=test_log,
            )
        _write_mutant_combined_log(combined_log, build_log, test_log)

        if timed_out:
            # Booley's own rule, ported verbatim (spike §1a): "a timed-out
            # mutant counts as detected because the mutation can wedge the
            # design."
            results.append(
                {
                    **base_result,
                    "status": "killed",
                    "first_killing_test": None,
                    "log_path": _path_envelope(combined_log),
                }
            )
            killed_count += 1
            continue

        if status_kind == "error":
            results.append(
                {
                    **base_result,
                    "status": "rejected",
                    "reason": detail,
                    "first_killing_test": None,
                    "log_path": _path_envelope(combined_log),
                }
            )
            rejected_count += 1
            continue

        try:
            mutant_tests = parse_results_xml(mutant_results_xml)
        except FunctionalVerificationError as exc:
            results.append(
                {
                    **base_result,
                    "status": "rejected",
                    "reason": str(exc),
                    "first_killing_test": None,
                    "log_path": _path_envelope(combined_log),
                }
            )
            rejected_count += 1
            continue

        first_killing_test = _first_new_failure(baseline_tests, mutant_tests)
        if first_killing_test is not None:
            results.append(
                {
                    **base_result,
                    "status": "killed",
                    "first_killing_test": first_killing_test,
                    "log_path": _path_envelope(combined_log),
                }
            )
            killed_count += 1
        else:
            results.append(
                {
                    **base_result,
                    "status": "survived",
                    "first_killing_test": None,
                    "log_path": _path_envelope(combined_log),
                }
            )
            survived_count += 1

    proposal_count = len(proposal_records)
    valid_count = proposal_count - rejected_count
    if valid_count == 0 and proposal_count > 0:
        # Mirrors the base contract's own "a regression that registered
        # zero tests is exit 1, not a vacuous pass": a mutation run that
        # tested nothing must never look like a clean `mutation_score: null`
        # success (spike "Exit codes" table).
        reasons = "; ".join(
            f"#{entry['index']}: {entry['reason']}"
            for entry in results
            if entry["status"] == "rejected"
        )
        raise FunctionalVerificationError(
            f"every mutation proposal was rejected ({proposal_count} "
            f"proposal(s), 0 valid) -- {reasons}"
        )

    mutation_score = (killed_count / valid_count) if valid_count else None

    baseline["mutation_testing"] = {
        "schema_version": MUTATION_SCHEMA_VERSION,
        "proposal_count": proposal_count,
        "valid_count": valid_count,
        "rejected_count": rejected_count,
        "killed_count": killed_count,
        "survived_count": survived_count,
        "mutation_score": mutation_score,
        "results": results,
    }
    return baseline
