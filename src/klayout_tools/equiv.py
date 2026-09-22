"""Prove or refute combinational or sequential equivalence between two
RTL/gate-level netlists via Yosys, headless.

Pure library: :func:`run_equiv` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``lvs.py``/
``sim.py``/``synthesize.py``. Serialisation and human-readable formatting
live in the CLI command module (``cli/equiv_cmd.py``).

This is Phase 0 of the formal-equivalence epic
([#707](https://github.com/2AMLogic/klayout-tools/issues/707)) -- the
correctness loop-closer #704 (RTL synthesis) and #700 (place-and-route) both
name as their own verification step. Scope is deliberately narrow
(**combinational only**): #707's later phases extend this to sequential
designs (temporal induction / bounded model checking via SymbiYosys); a
design containing registers, latches, or memories is rejected up front with
a clear scope error (see :func:`_sequential_error_message`) rather than
silently running a per-cycle-only combinational check and reporting a
misleadingly confident verdict.

Two existing ``klt`` commands are this module's structural precedent, for
different parts of the shape:

- ``klt lvs`` (``lvs.py``) is the closest structural match: two
  representations in, a match/mismatch verdict out.
- ``klt sim``'s exit-code trichotomy (0 pass / 3 target-unmet / 4
  evaluator-errored) is the precedent for the **inconclusive-on-timeout**
  outcome this module's own scope requires: a solver timeout must never be
  reported as ``"equivalent"``, so it gets its own status (`"inconclusive"`)
  distinct from a proven counterexample (`"counterexample"`), following
  ``sim``'s pattern of adding a fourth outcome rather than overloading a
  3-way split.

## Engine: Yosys's built-in ``miter``/``sat`` equivalence flow

``request.engine`` exists from day one (only ``"yosys"`` is implemented) so
a later engine (e.g. a full SymbiYosys ``mode equiv`` invocation for
sequential designs) is an additive enum value, per every other ``klt``
digital-flow verb's own precedent (``synthesize.py``,
``functional_verification.py``).

This environment (and this repo's CI, ``.github/workflows/ci.yml``) has a
real ``yosys`` binary but no ``sby``/SymbiYosys install -- SymbiYosys adds
essentially nothing over plain Yosys for the **combinational** MVP this
issue scopes (its main value is orchestrating multi-step *sequential*
proofs -- BMC/k-induction -- which is explicitly out of scope here). Rather
than stub the orchestration behind an interface no build of this repo can
exercise, this module orchestrates Yosys's own `equiv`-family primitives
directly -- the same `miter -equiv` / `sat -prove-asserts` recipe the Yosys
manual's own equivalence-checking chapter documents, and the same one
SymbiYosys's `mode equiv` itself expands to for a single-cycle proof. A
`sby`/SymbiYosys-backed `"engine": "symbiyosys"` for sequential designs is
left for a later phase of #707, once it can be exercised.

Two subprocesses are actually run:

1. ``yosys -s <script>`` -- builds a miter circuit
   (``gold`` != ``gate`` for any legal input assignment) and asks Yosys's
   own SAT backend (built-in MiniSat, no external SAT-solver dependency) to
   prove or refute it. Never trusted blindly on its own (see next point).
2. On a refutation (a counterexample was found), ``iverilog``/``vvp`` runs
   the *actual* concrete counterexample vector through the two flattened
   netlists Yosys itself just proved diverge, independently of the SAT
   solver, and confirms the divergence really reproduces -- this is the
   "counterexample is executable" discipline the epic's own reality-
   grounding section requires. ``klt sim``'s simulation-invocation path
   (``sim.py``) is SPICE/analog-specific (``ngspice -b`` on a resistor/
   transistor-level netlist) and is not the right tool for a gate-level RTL
   vector; ``iverilog``/``vvp`` is the digital equivalent already wired up
   as a first-class dependency by ``klt functional-verification``
   (``functional_verification.py``) -- reused here, not SPICE. **When that
   independent replay does *not* reproduce a divergence
   (``confirmed_by_simulation: False``), the reported ``status`` is
   downgraded to ``"inconclusive"``, never left as ``"counterexample"``**
   (issue #1349) -- an unreproduced solver counterexample is not a
   demonstrated functional difference, it is evidence of an unsound
   miter/``$equiv`` artifact, and reporting it as a proven refutation would
   violate this module's own "a counterexample is executable" discipline
   rather than satisfy it. ``confirmed_by_simulation: None`` (simulation
   could not be attempted at all, e.g. no ``iverilog`` on ``$PATH``) is left
   alone -- there is no simulation evidence either way in that case, so the
   solver's own verdict stands.

## Replay backend: ``request.sim_backend`` (issue #2223)

Which simulator runs step 2's counterexample/vector *replay* -- an
orthogonal axis to ``request.engine``, which selects the *proof* engine:

- ``"iverilog"`` (default, and the **canonical** backend for evidence):
  exactly the ``iverilog``/``vvp`` path described above, unchanged.
- ``"verilator"``: replays the same generated testbench through
  ``verilator --binary`` instead. Icarus is an event-driven interpreter
  while Verilator compiles to C++, so on a long vector/trace the compiled
  path is dramatically faster (a downstream RTL bring-up measured 8:56 vs.
  1:22, ~6.4x, and the gap grows with vector length).
- ``"both"``: runs *both*, adopts neither silently. The canonical
  (``iverilog``) run populates ``counterexample.simulation`` exactly as
  today; the Verilator run populates the additive
  ``counterexample.simulation_cross_check``. **The two must agree**: if
  they reach different confirmation verdicts -- or differ on replayed
  output bits in a way the declared backend-specific fields do not explain
  -- the disagreement is reported as an error-severity
  ``sim_backend_disagreement`` diagnostic, ``confirmed_by_simulation`` is
  reset to ``None`` (neither backend's verdict is adopted), and the
  reported ``status`` is downgraded to ``"inconclusive"``. A backend
  disagreement is never a pass and never a silently-preferred fast path.

The one declared backend-specific difference is **value modelling**:
Icarus is 4-state (an undriven register reads ``x``), Verilator is 2-state
(it reads ``0``). Each replay records its own ``four_state`` flag, and a
bit difference that occurs *only* where the 4-state canonical run reported
``x``/``z`` is classified ``explained_by: "two_state_backend"`` rather than
counted as a disagreement.

When ``verilator`` is not installed, nothing is fabricated: the
Verilator-only backend degrades to the same ``simulation_unavailable``
diagnostic a missing ``iverilog`` already produces, and ``"both"`` records
``agreement: "unavailable"`` and leaves the canonical verdict standing --
byte-for-byte the behavior this module had before the backend existed.

## Engine: ``"yosys-sequential"`` -- register-correspondence sequential
equivalence (Phase 2, #1313)

A second engine, additive to ``"yosys"`` above (the combinational-only
engine keeps rejecting sequential designs outright, unchanged): proves
**register-correspondence** sequential equivalence -- the case where
``gold``/``gate`` share an identical set of state elements (flip-flops),
matched 1:1 by name, and only the *combinational* logic between them
differs. This is deliberately narrower than general sequential equivalence
(retiming, register cloning, FSM re-encoding): it is the evidence-matched
shape ``docs/design/sequential-equivalence-survey.md`` (SS2) found this
repo's own place-and-route pipeline actually produces (buffer insertion and
drive-strength resizing only -- zero register-count change, measured on a
real corpus run), and mirrors Yosys's own manual, which recommends exactly
this ``equiv_make``/``equiv_simple``/``equiv_induct``/``equiv_status``
family for this case.

**Not SymbiYosys (``sby``).** The survey's own §4.2 proposed driving this
via "``sby``'s ``mode equiv``" -- that mode does not exist. `sby` (as of
the pinned `v0.67`, verified live in this task) only implements
``bmc``/``prove``/``cover``/``live``/``prep`` modes (see
``sby_core.py``'s own ``self.opt_mode not in [...]`` check); the
``equiv_make``/``equiv_induct`` command family lives in Yosys itself and is
normally orchestrated by a *separate* YosysHQ project, ``eqy``, which is not
installed in this repo (a heavier dependency: a from-source C++ plugin
build, not just a pinned Python launcher) and was not needed -- these are
plain built-in Yosys passes, invocable directly via ``yosys -s <script>``,
the same "orchestrate Yosys's own primitives directly, no extra dependency
this repo can't exercise" choice ``equiv.py``'s combinational engine already
made for ``miter``/``sat``.

**Two stages, because ``equiv_status`` cannot itself report a
counterexample.** Read literally, Yosys's ``equiv_status`` pass has exactly
two states per ``$equiv`` cell -- "proven" (collapsed to a tautology) or
"unproven" (verified against Yosys's own ``passes/equiv/equiv_status.cc``
source, live in this task) -- never a definite "refuted". Register
correspondence via temporal induction (``equiv_induct``) is a *proof*
technique, not a counterexample-*finding* one: "unproven" honestly means
"this technique could not decide", not "these designs differ".

1. **Stage 1 (the named technique):** ``equiv_make`` pairs corresponding
   gold/gate wires by name; ``clk2fflogic`` lowers every clocked flip-flop
   to Yosys's formal-verification-friendly ``$ff`` model (handles both
   single- and multi-clock, sync- and async-reset designs uniformly, unlike
   hand-rolling one specific reset style); ``equiv_simple`` resolves what it
   can with plain per-cell SAT; ``equiv_induct`` resolves the rest by
   temporal induction over the design's own state elements; ``equiv_status``
   reports the final tally. All ``$equiv`` cells proven -> ``"equivalent"``.
   Stage 1 re-runs itself under **cut-point refinement** when it does not --
   see the next section.

### Stage 1 cut-point refinement (issue #1353)

``equiv_make`` pairs wires **by name**, and a physical-design transformation
is under no obligation to preserve internal wire names: OpenROAD's own
resizing, repair-buffer insertion and gate cloning routinely leave a
same-named internal wire carrying a *different* Boolean value on the two
sides, even though the two designs are output-equivalent. Each such pair
becomes an internal ``$equiv`` cell that can never be proven -- not because
the designs differ, but because the pairing itself was wrong. Because a
``$equiv`` cell is also a **cut point** (downstream cones on both sides read
the ``$equiv`` output rather than their own driver), a wrongly-paired wire
does not merely add noise: it injects a false assumption into every proof
downstream of it, which is exactly why Yosys refuses to call the run proven
while any ``$equiv`` cell is unproven.

Stage 1 therefore runs as a small **counterexample-guided refinement loop**
(bounded by :data:`_MAX_STAGE1_REFINEMENTS`, sharing one ``timeout_s``
budget across all of its passes):

1. Run the recipe above. If every ``$equiv`` cell is proven -> ``"equivalent"``.
2. Otherwise, collect the wire names Yosys reported unproven, **drop every
   name that is a top-level port**, and re-run ``equiv_make`` with the
   remainder passed via ``-blacklist`` so no ``$equiv`` cell (and so no cut
   point) is created for them at all.
3. Repeat until proven, until no new name is added, or until the iteration
   cap is hit -- then fall through to stage 2 exactly as before.

**This is sound, and strictly stronger than not doing it.** Blacklisting
only ever *removes* cut points, i.e. removes assumptions: every obligation
that survives is proven from more of the two designs' real logic, never
less. Top-level ports are never blacklisted, so the obligations that
actually define equivalence -- "gold and gate produce the same outputs" --
are always still proven, never dropped. A genuinely-broken gate netlist
cannot be laundered into ``"equivalent"`` by this loop: with the port
obligations retained, the loop simply fails to converge and stage 2's
bounded SAT search runs as before (verified live against a deliberately
mutated real post-route netlist -- see ``docs/cli/equiv.md``'s
"Re-running the real pre/post-route canary").

That soundness argument rests entirely on step 2's "**except any name that
is a top-level port**" filter, which in turn rests on
:func:`_parse_module_ports` seeing *every* port. Verilog **escaped
identifiers** (``\\q.x``, ``\\q[0]`` -- how a synthesis/P&R flow spells a
name containing ``.``/``[``/``]``/``/``) are terminated by whitespace, so
Yosys writes them as ``output \\q.x ;``; a parser that does not allow for
that space silently loses the port, the loop blacklists it as if it were an
internal wire, and a genuinely-different design is reported ``"equivalent"``
(issue #1999). :data:`_PORT_DECL_RE` therefore matches both spellings.

``artifacts.stage1_blacklist_path`` records exactly which wires were
dropped, so a reader can audit the weakened obligation set rather than
having to trust it; an ``equiv_cutpoint_refinement`` info diagnostic reports
the count.
2. **Stage 2 (only runs when stage 1 leaves cells unproven):** rebuilds the
   same gold/gate pairing with ``equiv_make -make_assert`` (turning each
   matched pair into an ``$assert`` instead of an ``$equiv`` cell), then
   runs a genuinely bounded (not inductive) ``sat -seq <N> -set-init-zero
   -prove-asserts`` search for an actual violating trace, up to the same
   depth ``equiv_induct`` used. ``-set-init-zero`` anchors both sides to an
   identical, defined starting register state -- without it, this task's
   own live experiments reproduced a well-known false-positive class
   (SS3.5 of the survey): an *unconstrained* free initial register state on
   each side diverges trivially, with no bearing on whether the two designs
   actually behave differently once reachable from reset. A genuine
   violation found within the bound is a **real, demonstrated**
   counterexample (this bounded search is a complete decision procedure
   *within its own depth*, unlike induction); finding none is reported
   ``"inconclusive"`` (a bounded, all-zero-start search finding nothing
   does not establish the general, unbounded claim register-correspondence
   induction itself could not prove) -- **never** silently upgraded to
   ``"equivalent"``, this module's own scope requirement restated for the
   sequential engine.

A genuine stage-2 counterexample is independently confirmed by simulation
exactly as the combinational engine's own counterexample is (see above),
generalised to multi-cycle: the confirmation testbench replays the entire
captured cycle-by-cycle input sequence (not a single vector) through the
same flattened gold/gate netlist via ``iverilog``/``vvp`` and checks the
solver-reported divergence reproduces somewhere in the trace -- see
:func:`_confirm_sequential_counterexample`'s own docstring for why "somewhere
in the trace" rather than "at the identical cycle index" (the same
X-vs-zero-init mismatch between a real Verilog simulation's own default
register reset value and ``-set-init-zero``'s SAT-side assumption). Exactly
as the combinational engine does, an unreproduced stage-2 counterexample
(``confirmed_by_simulation: False``) downgrades the reported ``status`` to
``"inconclusive"`` rather than the unsound ``"counterexample"`` (issue
#1349) -- verified live against a real ``equiv_make``-name-mismatch case
this engine's own bounded search can otherwise trip on (a real post-route
netlist's resizer/repair-buffer-renamed internal wires; see
``docs/cli/equiv.md``'s "Re-running the real pre/post-route canary").

## Resumable runs: ``--resume`` (issue #2280)

Long evidence runs die mid-way in autonomous-agent fleets (rate limits,
session reaping) -- issue #2280's two gf180-surge incidents. ``--resume``
gives ``klt equiv`` an idempotent re-entry contract modeled on ``klt sim``'s
own ``--resume`` checkpoint machinery (issue #473), adapted to what a Yosys
proof can actually checkpoint: **stages**, not corners.

- The ``"yosys-sequential"`` engine's stage 1 (the
  ``equiv_make``/``equiv_induct`` induction proof, including its bounded
  cut-point refinement loop) commits a stage record --
  ``.klt/equiv/stage1.commit.json`` -- atomically (temp file +
  ``os.replace``, the same never-partially-written discipline
  ``sim._Checkpoint`` uses) only after the stage reaches a classified
  outcome. A later ``klt equiv --resume`` run whose request fingerprint
  matches the record's skips stage 1 entirely: an ``all_proven`` record
  produces the final ``"equivalent"`` envelope without re-running Yosys at
  all; an ``unproven_cells`` record re-enters directly at stage 2.
- The combinational engine is a single Yosys subprocess -- it has no
  earlier stage artifact to re-enter from, so ``--resume`` is accepted and
  re-runs its one stage (documented, so an agent fleet can issue one
  uniform retry command regardless of engine).
- **A stage record is never verdict-bearing unless it is fully
  corroborated.** The loader (:func:`_load_committed_stage1`) rejects --
  and the resume re-runs the stage -- for: a ``partial: true`` record (the
  marker written when a stage attempt died or timed out before
  classifying), a fingerprint mismatch (the request changed since the
  commit), any missing/mistyped field, a missing committed log artifact,
  or a log whose own bytes do not corroborate the recorded classification
  (an ``all_proven`` record whose log lacks Yosys's success line can never
  satisfy the resume). A discarded record is never silent: the resumed
  envelope carries a ``resume_stage_record_discarded`` warning diagnostic
  naming the reason.
- **An envelope, by contrast, is never partial.** A run that dies mid-way
  emits nothing; the stage artifacts it left behind carry the ``partial``
  marking instead. A committed ``--format json`` envelope exists only for
  a run that reached a verdict. This is the negative-control property
  issue #2280's acceptance criteria require: partial artifacts can never
  satisfy a verdict check, structurally, because the only code path from
  a stage record to an envelope ``status`` runs the full loader
  validation above.

A resumed run's envelope is byte-identical to the uninterrupted run's
except in the fields the guide declares as run-scoped: ``elapsed_s``, the
additive ``resume`` block (present only when ``--resume`` was given,
mirroring ``klt sim``'s ``environment.resume`` convention), and --
when the resume happens on a different host -- the host-scoped
``provenance.klt_version``/``engine_version`` identity fields
(``docs/guides/remote-evidence-runs.md``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from typing import Any

from ._paths import _load_request_json, validate_request_shape
from ._paths import load_request_arg as _shared_load_request_arg
from ._provenance import (
    INPUT_ROLE_SOURCE,
    _combined_content_hash,
    _content_hash,
    _yosys_version,
    build_provenance,
    wasi_sandbox_hint_if_applicable,
)
from ._report_verify import (
    VOLATILE_FLOW_PATHS,
    build_check_result,
    build_rerun_result,
    get_path,
    hash_check,
    load_committed_report,
    strip_keys,
)

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"yosys"`` (combinational, Phase 0/1) and ``"yosys-sequential"``
#: (register-correspondence sequential, Phase 2) -- see this module's
#: docstring, "Engine" sections.
SUPPORTED_ENGINES = ("yosys", "yosys-sequential")

#: Counterexample/vector *replay* backends -- an axis orthogonal to
#: :data:`SUPPORTED_ENGINES` (which selects the *proof* engine). See this
#: module's docstring, "Replay backend" section (issue #2223).
SUPPORTED_SIM_BACKENDS = ("iverilog", "verilator", "both")

#: The default replay backend: unchanged from before the backend selector
#: existed, so omitting ``request.sim_backend`` reproduces the historical
#: behavior exactly.
DEFAULT_SIM_BACKEND = "iverilog"

#: The backend whose replay is **canonical for evidence** -- it is the one
#: that populates ``counterexample.simulation``/``confirmed_by_simulation``
#: under ``"both"``. Verilator is a cross-check/fast path, never a silent
#: replacement for it.
CANONICAL_SIM_BACKEND = "iverilog"

#: The cross-check backend ``"both"`` pairs :data:`CANONICAL_SIM_BACKEND`
#: with.
CROSS_CHECK_SIM_BACKEND = "verilator"

#: Per-backend ``(version command, version regex)`` -- mirrors
#: ``functional_verification.py``'s own ``_ENGINE_VERSION_COMMANDS`` table
#: rather than inventing a parallel convention.
_SIM_BACKEND_VERSION_COMMANDS = {
    "iverilog": (["iverilog", "-V"], re.compile(r"Icarus Verilog version (\S+)")),
    "verilator": (["verilator", "--version"], re.compile(r"Verilator (\S+)")),
}

#: On-the-wire ``simulation.engine`` label per replay backend. ``"icarus"``
#: is the value this command has always emitted for the ``iverilog``/``vvp``
#: path -- kept verbatim (it is the same name
#: ``functional_verification.py``'s own engine axis uses), so no existing
#: consumer sees a changed value.
_SIM_ENGINE_LABEL = {"iverilog": "icarus", "verilator": "verilator"}

#: Human-readable tool label used in this module's own diagnostics.
_SIM_BACKEND_LABEL = {"iverilog": "iverilog/vvp", "verilator": "verilator"}

#: Whether a backend models 4-state (``0``/``1``/``x``/``z``) values.
#: Verilator is 2-state: an undriven register reads ``0`` where Icarus
#: reads ``x``. Declared per-run in ``simulation.four_state`` so a
#: cross-backend bit difference can be *explained* rather than silently
#: tolerated -- see :func:`_compare_replay_outputs`.
_SIM_BACKEND_FOUR_STATE = {"iverilog": True, "verilator": False}

#: Compile-step budget for the ``iverilog`` replay backend. Unchanged
#: (30s); named only so the diagnostic text stays in sync with it.
_IVERILOG_COMPILE_TIMEOUT_S = 30

#: Compile-step budget for the ``verilator`` replay backend. Verilator
#: front-ends *and then C++-compiles* the design, so its one-off build cost
#: is much larger than Icarus's parse -- that upfront cost is exactly what
#: the per-vector speedup pays back on a long run. Generous enough for a
#: real netlist's C++ build, still bounded.
_VERILATOR_COMPILE_TIMEOUT_S = 300

#: Run-step budget, shared by both replay backends.
_REPLAY_RUN_TIMEOUT_S = 30

#: Default per-run wall-clock timeout, overridable via ``request.timeout_s``
#: or the CLI's ``--timeout-s``. Generous enough for a real corpus design's
#: proof, tight enough that an accidentally-hard SAT instance doesn't hang
#: a CI job indefinitely. Applied independently to *each* stage of the
#: ``"yosys-sequential"`` engine's two-stage run (see that engine's own
#: docstring) -- a worst-case run may take up to 2x this budget.
DEFAULT_TIMEOUT_S = 60.0

#: Default ``equiv_induct -seq``/stage-2 ``sat -seq`` depth for the
#: ``"yosys-sequential"`` engine, overridable via ``request.induction_depth``
#: -- matches ``equiv_induct``'s own Yosys-internal default, so omitting the
#: field behaves identically to not passing ``-seq`` at all.
DEFAULT_INDUCTION_DEPTH = 4

#: RTLIL cell-type prefixes that mean "this module has state" -- flip-flops
#: (``$dff``/``$adff``/``$sdff`` and their enable/async-reset/set-reset
#: variants, all sharing these prefixes), latches, memories, and Yosys's
#: internal set/reset cell. Detected *before* the SAT run via a
#: ``select -assert-none`` guard (see :func:`_write_script`) -- this MVP is
#: combinational-only (see module docstring).
_SEQUENTIAL_CELL_GLOBS = (
    "$dff*",
    "$adff*",
    "$sdff*",
    "$dlatch*",
    "$sr*",
    "$mem*",
    "$fsm*",
)

_SAT_SUCCESS_RE = re.compile(r"SAT proof finished - no model found: SUCCESS!")
_SAT_FAIL_RE = re.compile(r"SAT proof finished - model found: FAIL!")
_SAT_TIMEOUT_RE = re.compile(r"Interrupted SAT solver: TIMEOUT!")

_SIGNAL_TABLE_HEADER_RE = re.compile(r"Signal Name.*Bin\s*$")

_SIM_DISPLAY_RE = re.compile(r"^EQUIV_SIM (gold|gate) (\S+) ([01xz]+)$")

_REQUIRED_REQUEST_FIELDS = ("gold", "gate")

# --------------------------------------------------------------------------- #
# "yosys-sequential" engine (Phase 2, #1313) -- additional regexes/constants.
# --------------------------------------------------------------------------- #

#: ``equiv_status``'s own exact success line (verified live against the
#: pinned Yosys v0.67 this repo's own CI installs, `equiv_status.cc`'s
#: `unproven_equiv_cells.empty()` branch) -- every `$equiv` cell was proven.
_EQUIV_ALL_PROVEN_RE = re.compile(r"Equivalence successfully proven!")

#: `equiv_make`'s own degenerate-case line: zero `$equiv` cells were even
#: created (no matching gold/gate wire names found at all) -- a request-
#: shape problem (missing/wrong `port_map`, or gold/gate share no register
#: names), not a real proof outcome. Reported as an `EquivError`, not a
#: (misleading) `"inconclusive"` verdict.
_EQUIV_NONE_FOUND_RE = re.compile(r"No \$equiv cells found in")

#: Multi-cycle counterpart of `_SIGNAL_TABLE_HEADER_RE`/`_parse_signal_table`
#: -- `sat -seq N`'s own dump prepends a leading "Time" column and repeats
#: the "Signal Name / Dec / Hex / Bin" table once per timestep (see
#: `_parse_multicycle_signal_table`).
_SEQ_TIME_HEADER_RE = re.compile(r"Time Signal Name.*Bin\s*$")

_SEQ_SIM_DISPLAY_RE = re.compile(r"^EQUIV_SIM_CYCLE (\d+) (gold|gate) (\S+) ([01xz]+)$")

#: Parses one `write_verilog -noattr`-emitted port declaration line, e.g.
#: `  input clk;`, `  output [3:0] sum;` or -- for a Verilog *escaped*
#: identifier -- `  output \q.x ;` (issue #1999). An escaped identifier is
#: terminated by whitespace, not by the next non-identifier character, so
#: Yosys emits a space between the name and the `;`; the `\s*` before the
#: `;` is what lets such a port be recognised at all. Without it the port
#: is silently invisible to `_parse_module_ports`, and `_run_sequential`'s
#: cut-point refinement then mistakes a top-level port for an internal wire
#: and blacklists away the very obligation that defines equivalence.
_PORT_DECL_RE = re.compile(
    r"^\s*(input|output|inout)\s+(?:\[[^\]]+\]\s+)?(\S+?)\s*;\s*$"
)
_MODULE_START_RE = re.compile(r"^\s*module\s+(\S+)\s*\(")
_MODULE_END_RE = re.compile(r"^\s*endmodule\s*$")

#: A Verilog-2005 *simple* identifier: no escape needed when emitting it
#: into generated source. Anything else (a flattened hierarchical name
#: `q.x`, a bit-blasted `q[0]`, a path-shaped `top/u1/z`) must be written as
#: a whitespace-terminated escaped identifier -- see `_verilog_ident`.
_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

#: One `equiv_status` "unproven cell" report line, e.g.
#: `  Unproven $equiv $auto$equiv_make.cc:295:find_same_wires$10212:
#:  \_560_.B_gold \_560_.B_gate` -- the trailing group holds the gold/gate
#: signals the unproven `$equiv` cell pairs (see
#: `_parse_unproven_equiv_signals`).
_UNPROVEN_EQUIV_RE = re.compile(r"^\s*Unproven \$equiv \S+: (.+)$", re.MULTILINE)

#: How many times stage 1 may re-run `equiv_make` with a widened
#: `-blacklist` before giving up and falling through to stage 2 (issue
#: #1353's cut-point refinement loop -- see this module's docstring). The
#: real post-route netlists this engine targets converge in a single
#: refinement pass; the cap exists so a pathological design cannot spin, and
#: every pass shares the one `timeout_s` budget regardless.
_MAX_STAGE1_REFINEMENTS = 3


# --------------------------------------------------------------------------- #
# Resumable runs: stage commit records (issue #2280)
#
# Modeled on `sim.py`'s checkpoint/resume machinery (issue #473), adapted to
# what a Yosys proof can checkpoint: whole *stages*, not per-unit results.
# See this module's docstring, "Resumable runs", for the full contract --
# in particular why a stage record is structurally incapable of carrying a
# verdict unless every validation below passes.
# --------------------------------------------------------------------------- #

#: The two classified outcomes stage 1 can commit. Anything else (a process
#: timeout, a Yosys error, zero `$equiv` cells) is either an error the run
#: raises or a `partial: true` record -- never a resumable commit.
_STAGE1_ALL_PROVEN = "all_proven"
_STAGE1_UNPROVEN_CELLS = "unproven_cells"
_STAGE1_CLASSIFICATIONS = (_STAGE1_ALL_PROVEN, _STAGE1_UNPROVEN_CELLS)


def _stage_record_path(output_dir: str, stage: int) -> str:
    """Where stage ``stage``'s commit record lives: ``stage<N>.commit.json``
    directly under ``output_dir`` (the same ``.klt/equiv/`` directory the
    stage's own script/log/netlist artifacts already go to), so a resumable
    run's on-disk footprint stays in one place -- the same convention
    ``sim._checkpoint_path`` follows for ``.klt/sim/checkpoint.json``.
    """
    return os.path.join(output_dir, f"stage{stage}.commit.json")


def _stage_fingerprint(
    *,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    effective_timeout_s: float,
    engine: str,
    induction_depth: int,
    sim_backend: str,
) -> str:
    """A SHA-256 fingerprint of everything that determines what a
    sequential run's stages actually compute -- the basis for deciding
    whether an on-disk stage record still applies to *this* request.

    Mirrors ``sim._checkpoint_fingerprint``'s own rule: content-hashes the
    inputs (not just their paths -- an edited-in-place file at the same
    path must invalidate the record) alongside every run parameter that
    reaches the proof. Deliberately *path-independent* (the same property
    ``_combined_content_hash`` documents for itself): the same request
    checked out at a different path -- or pulled back from a remote host
    with the artifacts (``docs/guides/remote-evidence-runs.md``) -- is
    resumable, which a path-keyed fingerprint would silently forbid.
    ``timeout_s`` is normalized through ``float()`` so the JSON-encoded
    ``60`` and ``60.0`` (a request integer vs. a ``--timeout-s`` float)
    fingerprint identically -- they are the same budget, and a resume must
    not discard a valid record over encoding.

    A fingerprint mismatch means "the request changed since the stage
    committed" -- the record is discarded (with a
    ``resume_stage_record_discarded`` diagnostic, never silently), exactly
    like ``sim._load_checkpoint``'s mismatched checkpoint.
    """
    payload = {
        "engine": engine,
        "gold": {
            "top": gold["top"],
            "sources_sha256": _combined_content_hash(gold["sources"]),
            "liberty_sha256": (
                _content_hash(gold["liberty"]) if gold.get("liberty") else None
            ),
        },
        "gate": {
            "top": gate["top"],
            "sources_sha256": _combined_content_hash(gate["sources"]),
            "liberty_sha256": (
                _content_hash(gate["liberty"]) if gate.get("liberty") else None
            ),
        },
        "port_map": port_map,
        "timeout_s": float(effective_timeout_s),
        "induction_depth": induction_depth,
        "sim_backend": sim_backend,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _write_stage_record(path: str, record: dict[str, Any]) -> None:
    """Atomically write a stage record: temp file + ``os.replace`` -- the
    same never-partially-written discipline ``sim._Checkpoint._flush_locked``
    uses, so a reader (this one or a future one) can never observe a
    half-written record. ``partial`` defaults to an explicit JSON ``false``
    when the caller does not state it: the committing path in
    :func:`_run_sequential` writes classified records (always committed),
    while :func:`_write_partial_stage_record` is the only caller that
    passes ``partial: True`` explicitly.
    """
    payload = dict(record)
    payload.setdefault("partial", False)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError:
        # Best-effort, exactly like the stage log write above it: a record
        # write failure must never fail an otherwise-successful proof. The
        # cost of losing one is only that the next `--resume` re-runs the
        # stage -- never a wrong verdict.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _write_partial_stage_record(path: str, fingerprint: str, *, reason: str) -> None:
    """Best-effort write of a ``partial: true`` stage record documenting a
    stage attempt that died or failed *before* reaching a classified
    outcome (process timeout, Yosys error, zero `$equiv` cells).

    This is the producer behind issue #2280's `partial: true` marking: an
    interrupted run's on-disk trace of "stage 1 was attempted here and did
    not complete" is explicitly partial, and the loader below refuses any
    record whose ``partial`` is not exactly JSON ``false`` -- so a partial
    record can never satisfy the resume, no matter what else it contains.
    Never overwrites an already-committed (``partial: false``) record: a
    later, killed re-run's partial marker must not destroy the one valid
    commit an earlier run left. Never raises (see
    :func:`_write_stage_record` for the failure posture).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
        if isinstance(existing, dict) and existing.get("partial") is False:
            return
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    _write_stage_record(
        path,
        {
            "stage": 1,
            "fingerprint": fingerprint,
            # The marker this function exists for: an attempt that never
            # reached a classified outcome is explicitly partial, and the
            # loader refuses any record whose `partial` is not exactly
            # JSON false -- so this artifact can never satisfy the resume,
            # no matter what else it contains.
            "partial": True,
            "classification": None,
            "blacklist": [],
            "refinements": 0,
            "reason": reason,
        },
    )


def _stage_record_shape_error(data: Any, fingerprint: str) -> str | None:
    """The structural-validation half of :func:`_load_committed_stage1`:
    the reason a parsed record must be discarded, or ``None`` when every
    structural check passes. Factored out so the loader itself stays under
    the repo's complexity ratchet (`scripts/check_complexity_baseline.py`,
    issue #2034) -- each check here is one of the ways a partial or
    fabricated artifact is prevented from satisfying the resume's verdict
    check (issue #2280).
    """
    if not isinstance(data, dict):
        return "record is not a JSON object"
    if data.get("partial") is not False:
        return "record is marked partial (or carries no partial marker)"
    if data.get("stage") != 1:
        return "record is not for stage 1"
    if data.get("fingerprint") != fingerprint:
        return (
            "fingerprint mismatch -- the request changed since this stage "
            "record was committed"
        )
    if data.get("classification") not in _STAGE1_CLASSIFICATIONS:
        return "record carries no valid stage-1 classification"
    blacklist = data.get("blacklist")
    if not isinstance(blacklist, list) or not all(
        isinstance(name, str) and name for name in blacklist
    ):
        return "record's blacklist is not a list of wire names"
    refinements = data.get("refinements")
    if (
        not isinstance(refinements, int)
        or isinstance(refinements, bool)
        or (refinements < 0)
    ):
        return "record's refinement count is not a non-negative integer"
    return None


def _stage_log_corroborates(log_text: str, classification: str) -> bool:
    """Whether the committed stage log's own bytes corroborate the recorded
    classification: an ``all_proven`` record requires Yosys's own
    ``Equivalence successfully proven!`` line; an ``unproven_cells`` record
    requires unproven-``$equiv`` report lines and the success line's
    absence. The log, not the record, is the primary artifact -- a
    fabricated or truncated record whose log disagrees is discarded (issue
    #2280's negative-control property)."""
    if classification == _STAGE1_ALL_PROVEN:
        return bool(_EQUIV_ALL_PROVEN_RE.search(log_text))
    return not _EQUIV_ALL_PROVEN_RE.search(log_text) and bool(
        _UNPROVEN_EQUIV_RE.search(log_text)
    )


def _load_committed_stage1(
    record_path: str,
    fingerprint: str,
    *,
    log_path: str,
    netlist_path: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read and fully validate stage 1's commit record for a ``--resume``
    run. Returns ``(record, None)`` when the record is committed, intact,
    fingerprint-matched, and corroborated by its own committed log bytes;
    ``(None, None)`` when no record exists at all (the ordinary first-run
    shape -- nothing was discarded, so no diagnostic is owed); and
    ``(None, reason)`` when a record exists but is rejected -- the caller
    surfaces ``reason`` as a ``resume_stage_record_discarded`` warning and
    re-runs the stage.

    Every rejection below is issue #2280's negative-control property made
    structural: the only verdict-bearing field a record carries is its
    ``classification`` (an ``all_proven`` classification *is* the final
    ``"equivalent"`` verdict for the resumed run), so each check exists to
    make sure nothing but a fully-corroborated commit can reach it -- see
    :func:`_stage_record_shape_error` (the structural checks) and
    :func:`_stage_log_corroborates` (the log-bytes check). An
    ``unproven_cells`` record additionally requires the committed stage-1
    netlist (stage 2's own input) to still be on disk.
    """
    if not os.path.isfile(record_path):
        return None, None
    try:
        with open(record_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, "record is unreadable or not valid JSON"
    shape_error = _stage_record_shape_error(data, fingerprint)
    if shape_error is not None:
        return None, shape_error
    if not os.path.isfile(log_path):
        return None, "the committed stage log artifact is missing"
    try:
        with open(log_path, encoding="utf-8") as handle:
            log_text = handle.read()
    except OSError:
        return None, "the committed stage log artifact is unreadable"
    if not _stage_log_corroborates(log_text, data["classification"]):
        return None, (
            "the committed stage log does not corroborate the recorded "
            f"classification {data['classification']!r}"
        )
    if data["classification"] == _STAGE1_UNPROVEN_CELLS and not os.path.isfile(
        netlist_path
    ):
        return None, "the committed stage netlist artifact (stage 2's input) is missing"
    return data, None


class EquivError(Exception):
    """Raised when an equivalence check cannot even be attempted or
    completed to a verdict: a missing/malformed request, an unresolvable/
    unreadable RTL source, an unsupported engine, a design that contains
    sequential elements (out of this MVP's combinational-only scope), a
    Yosys elaboration/miter-construction error, or a missing ``yosys``
    binary.

    A run that *did* complete to a verdict -- ``"equivalent"``,
    ``"counterexample"``, or ``"inconclusive"`` (solver/process timeout) --
    is never an error; see :func:`run_equiv` and ``docs/cli/equiv.md``'s
    "Exit codes" section.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt equiv`` request JSON file.

    Raises :class:`EquivError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``gold``, ``gate``). Does
    not require a ``schema`` field, matching ``klt lvs``/``klt sim``'s
    ``load_request`` (user-authored input, never emitted by this tool).
    """
    request = _load_request_json(request_path, EquivError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=EquivError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve the ``klt equiv`` CLI ``request`` argument into a request
    dict plus the directory relative paths inside it should resolve
    against.

    ``value`` is one of three forms, mirroring ``klt lvs``/``klt
    functional-verification`` (see ``docs/cli/equiv.md``): a path to an
    existing request JSON file, ``"-"`` to read the request from stdin, or
    an inline JSON object string. Raises :class:`EquivError` for any read/
    parse/shape failure.
    """
    return _shared_load_request_arg(
        value,
        error_cls=EquivError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


def _build_resume_block(
    engine: str, output_dir: str, resume: bool
) -> dict[str, Any] | None:
    """The additive `resume` envelope block (issue #2280), built once in
    :func:`run_equiv` and attached to whichever report the run produces:
    present only when ``resume`` was requested (the same
    present-only-when-requested convention ``klt sim``'s
    ``environment.resume`` block uses), so a run without ``--resume`` is
    byte-identical to pre-#2280 output. ``resumed_stage`` starts at 0 and
    is upgraded to 1 by :func:`_resume_report_from_committed_stage1` iff a
    committed stage record is actually adopted; the combinational engine
    has no stage records, so its ``record_path`` is null (the
    resolved-fields-are-null convention)."""
    if not resume:
        return None
    return {
        "resumed_stage": 0,
        "record_path": (
            _stage_record_path(output_dir, 1) if engine == "yosys-sequential" else None
        ),
    }


def run_equiv(
    request: str,
    *,
    timeout_s: float | None = None,
    sim_backend: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Run the ``klt equiv`` equivalence check declared by ``request`` (a
    path, ``-`` for stdin, or an inline JSON object string -- see
    :func:`load_request_arg`), via ``request.engine`` (``"yosys"``,
    combinational, or ``"yosys-sequential"``, register-correspondence
    sequential -- see this module's own docstring "Engine" sections).

    ``timeout_s`` (the CLI's ``--timeout-s``) overrides the request's own
    ``timeout_s`` field when given; the request field is used when
    ``timeout_s`` is ``None``; :data:`DEFAULT_TIMEOUT_S` is used when
    neither is given.

    ``sim_backend`` (the CLI's ``--sim-backend``) overrides the request's
    own ``sim_backend`` field the same way, selecting which simulator
    replays a counterexample: ``"iverilog"`` (default/canonical),
    ``"verilator"`` (compiled fast path), or ``"both"`` (run both and
    require agreement) -- see this module's docstring, "Replay backend".

    ``resume`` (the CLI's ``--resume``, issue #2280) enables stage-scoped
    idempotent re-entry from committed stage artifacts under
    ``.klt/equiv/`` -- see this module's docstring, "Resumable runs". For
    the ``"yosys-sequential"`` engine, a fingerprint-matched, fully
    corroborated ``stage1.commit.json`` skips stage 1 entirely; every
    other case re-runs from stage 1. For the combinational engine the
    flag is accepted and without effect beyond the additive ``resume``
    envelope block: that engine is a single Yosys subprocess with no
    earlier stage artifact to re-enter from, so re-entry *is* the re-run
    -- accepted so an agent fleet can issue one uniform retry command
    regardless of engine. When given, the returned dict carries the
    additive ``resume`` block (``{"resumed_stage": 0 | 1, "record_path":
    ...}``); when ``False``, the returned dict is byte-identical to
    pre-#2280 output (the key is omitted entirely, the same
    present-only-when-requested convention ``klt sim``'s
    ``environment.resume`` block uses).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/equiv.md``). Raises :class:`EquivError` for anything that
    prevents a verdict from being reached *at all* -- see
    :class:`EquivError`'s own docstring. A solver/process timeout is
    **not** an error: it is reported as ``status: "inconclusive"`` in the
    returned dict (this module's own scope requires a timeout is never
    silently reported as ``"equivalent"``).

    Generated artifacts (the ``.ys`` script, the flattened combined
    netlist, the raw Yosys log, and -- on a counterexample -- the
    confirmation testbench) are written to ``.klt/equiv/`` next to the
    request file (or the current working directory for the stdin/inline
    forms) and kept as debuggable artifacts, never deleted -- the same
    convention ``klt synthesize``'s ``.klt/synthesize/`` and ``klt sim``'s
    ``.klt/sim/`` already use.
    """
    request_doc, request_dir = load_request_arg(request)

    engine = request_doc.get("engine", "yosys")
    if engine not in SUPPORTED_ENGINES:
        raise EquivError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    effective_sim_backend = _resolve_sim_backend(sim_backend, request_doc)

    effective_timeout_s = timeout_s
    if effective_timeout_s is None:
        effective_timeout_s = request_doc.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(effective_timeout_s, (int, float)) or isinstance(
        effective_timeout_s, bool
    ):
        raise EquivError("timeout_s must be a positive number")
    if effective_timeout_s <= 0:
        raise EquivError("timeout_s must be a positive number")

    gold = _resolve_side(request_doc.get("gold"), request_dir, "gold")
    gate = _resolve_side(request_doc.get("gate"), request_dir, "gate")

    port_map = request_doc.get("port_map")
    if port_map is not None:
        if not isinstance(port_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and v
            for k, v in port_map.items()
        ):
            raise EquivError(
                "request.port_map must be a JSON object of "
                "{gate_port_name: gold_port_name} string pairs"
            )

    output_dir = os.path.join(request_dir, ".klt", "equiv")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise EquivError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    # The additive `resume` envelope block (issue #2280) -- see
    # `_build_resume_block`.
    resume_block = _build_resume_block(engine, output_dir, resume)

    if engine == "yosys-sequential":
        induction_depth = request_doc.get("induction_depth", DEFAULT_INDUCTION_DEPTH)
        if (
            not isinstance(induction_depth, int)
            or isinstance(induction_depth, bool)
            or induction_depth < 1
        ):
            raise EquivError("request.induction_depth must be a positive integer")
        return _run_sequential(
            gold=gold,
            gate=gate,
            port_map=port_map or {},
            output_dir=output_dir,
            effective_timeout_s=effective_timeout_s,
            induction_depth=induction_depth,
            engine=engine,
            sim_backend=effective_sim_backend,
            resume_block=resume_block,
        )

    # engine == "yosys" (combinational, Phase 0/1) continues below, unchanged.
    # `resume` is deliberately unused on this path beyond the envelope block:
    # a single Yosys subprocess has no earlier stage artifact to re-enter
    # from (see `run_equiv`'s docstring), so re-entry *is* the re-run.
    script_path = os.path.join(output_dir, "equiv.ys")
    netlist_path = os.path.join(output_dir, "equiv_netlist.v")
    log_path = os.path.join(output_dir, "equiv.log")

    int_timeout = max(1, round(effective_timeout_s))
    _write_script(
        script_path=script_path,
        gold=gold,
        gate=gate,
        port_map=port_map or {},
        netlist_path=netlist_path,
        sat_timeout_s=int_timeout,
    )

    started = time.monotonic()
    timed_out = False
    stdout = ""
    stderr = ""
    returncode: int | None = None
    try:
        completed = subprocess.run(
            ["yosys", "-s", script_path],
            capture_output=True,
            text=True,
            timeout=effective_timeout_s,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        # Mirrors `sim.py`'s `_run_corner`: a killed process's partial
        # output is not trusted -- the run is classified purely on
        # `timed_out`, never on whatever text happened to be captured
        # before the kill.
        timed_out = True
    except OSError as exc:
        raise EquivError(f"could not launch yosys: {exc}") from exc
    elapsed_s = round(time.monotonic() - started, 3)

    try:
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(stdout)
            if stderr:
                handle.write("\n--- stderr ---\n")
                handle.write(stderr)
    except OSError:
        pass

    engine_version = _yosys_version()

    if timed_out:
        return _build_report(
            resume=resume_block,
            engine=engine,
            engine_version=engine_version,
            sim_backend=effective_sim_backend,
            status="inconclusive",
            gold=gold,
            gate=gate,
            port_map=port_map,
            timeout_s=effective_timeout_s,
            elapsed_s=elapsed_s,
            counterexample=None,
            diagnostics=[
                {
                    "severity": "error",
                    "code": "process_timeout",
                    "message": (
                        f"yosys did not complete within {effective_timeout_s}s "
                        "(process killed) -- proof is inconclusive, not "
                        "'equivalent'"
                    ),
                }
            ],
            script_path=script_path,
            netlist_path=netlist_path,
            log_path=log_path,
        )

    if returncode != 0:
        message = _sequential_error_message(stdout, stderr)
        if message is None:
            message = _yosys_error_message(stdout, stderr, returncode)
        raise EquivError(message)

    status, diagnostics = _classify_sat_result(stdout)

    counterexample = None
    if status == "counterexample":
        signals = _parse_signal_table(stdout)
        counterexample = _build_counterexample(signals)
        _confirm_counterexample(
            counterexample=counterexample,
            netlist_path=netlist_path,
            output_dir=output_dir,
            diagnostics=diagnostics,
            sim_backend=effective_sim_backend,
        )
        if _replay_evidence_is_untrustworthy(counterexample):
            # Either the solver reported a counterexample whose own replay
            # through the flattened netlists did not reproduce a diverging
            # output (`counterexample_not_reproduced`, appended above) -- an
            # unsound `$equiv`/miter artifact, not a demonstrated functional
            # difference -- or (`sim_backend: "both"`) the two replay
            # backends disagreed. Either way "inconclusive" is the honest
            # verdict, matching the downgrade the sequential engine's own
            # stage-2 path already applies for its analogous "unproven, no
            # counterexample either" case below. Never reported when
            # `confirmed_by_simulation` is `None` *because* simulation could
            # not be attempted at all (e.g. no `iverilog` on $PATH) -- that
            # case has no evidence either way, so the solver's own verdict
            # stands.
            status = "inconclusive"

    return _build_report(
        resume=resume_block,
        engine=engine,
        engine_version=engine_version,
        sim_backend=effective_sim_backend,
        status=status,
        gold=gold,
        gate=gate,
        port_map=port_map,
        timeout_s=effective_timeout_s,
        elapsed_s=elapsed_s,
        counterexample=counterexample,
        diagnostics=diagnostics,
        script_path=script_path,
        netlist_path=netlist_path,
        log_path=log_path,
    )


def _resolve_side(spec: Any, request_dir: str, label: str) -> dict[str, Any]:
    """Validate and resolve ``request.<label>`` (``gold``/``gate``) into
    ``{"top": str, "sources": [absolute paths], "liberty": absolute path or None}``.

    Raises :class:`EquivError` naming ``label`` for every failure mode:
    not an object, missing/empty ``sources``/``top``, or an unreadable
    source/liberty file.

    ``liberty`` (optional) is a standard-cell liberty file, read via
    Yosys's ``read_liberty`` *without* ``-lib`` -- i.e. with each cell's
    liberty ``function`` string turned into real combinational logic, not a
    blackbox -- before ``sources`` is read. This is what makes a genuine
    post-synthesis gate-level netlist (``klt synthesize``'s own
    ``netlist_path`` output, which references standard-cell instances like
    ``sky130_fd_sc_hd__and2_1`` with no logic definition of their own)
    usable as an ``equiv`` side at all: without it, ``hierarchy -check``
    fails outright on the undefined cell instances. Omit it for a side that
    is already self-contained RTL (the common case for two independent RTL
    implementations, or two synthesised netlists sharing the same
    already-resolved liberty on the *other* side).
    """
    if not isinstance(spec, dict):
        raise EquivError(f"request.{label} must be a JSON object")

    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EquivError(f"request.{label}.sources must be a non-empty array of paths")
    if not all(isinstance(entry, str) and entry for entry in sources):
        raise EquivError(f"request.{label}.sources entries must be non-empty strings")

    top = spec.get("top")
    if not isinstance(top, str) or not top:
        raise EquivError(
            f"request.{label}.top is required and must be a non-empty string"
        )

    liberty = spec.get("liberty")
    if liberty is not None and not (isinstance(liberty, str) and liberty):
        raise EquivError(
            f"request.{label}.liberty must be a non-empty string when given"
        )

    resolved: list[str] = []
    for entry in sources:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isfile(path):
            raise EquivError(f"{label} source not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise EquivError(f"could not read {label} source '{entry}': {exc}") from exc
        resolved.append(os.path.abspath(path))

    resolved_liberty = None
    if liberty is not None:
        liberty_path = (
            liberty if os.path.isabs(liberty) else os.path.join(request_dir, liberty)
        )
        if not os.path.isfile(liberty_path):
            raise EquivError(f"{label} liberty file not found: {liberty}")
        resolved_liberty = os.path.abspath(liberty_path)

    return {"top": top, "sources": resolved, "liberty": resolved_liberty}


def _side_prep_lines(
    label: str,
    side: dict[str, Any],
    port_map: dict[str, str],
    *,
    extra_opt: bool = False,
) -> list[str]:
    """Build the ``.ys`` script lines that read, elaborate, flatten, and
    stash one side (``"gold"``/``"gate"``) of an equivalence request --
    shared between the combinational engine's own miter/sat script
    (:func:`_write_script`) and the ``"yosys-sequential"`` engine's
    ``equiv_make``-based scripts (:func:`_write_sequential_stage1_script`/
    :func:`_write_sequential_stage2_script`).

    ``extra_opt`` (issue #1353): runs a real ``opt -noff`` pass (the full
    ``opt_expr``/``opt_muxtree``/``opt_reduce``/``opt_merge``/``opt_clean``
    convergence loop, ``-noff`` skipping only the ``opt_dff`` sub-pass)
    after ``flatten``, in addition to the plain dead-code-only ``opt_clean``
    both engines already ran. Only the ``"yosys-sequential"`` engine's own
    script writers pass ``extra_opt=True`` -- the combinational engine
    (:func:`_write_script`) keeps its original, narrower ``opt_clean``-only
    normalization unchanged.

    **What it buys, measured rather than assumed.** Every liberty cell here
    was already expanded into real primitive logic by ``read_liberty``'s own
    ``function``/``ff`` group parsing (no ``-lib``, see the ``liberty``
    branch above) *before* ``flatten`` inlines it, so a resizer-inserted
    identity buffer chain or a same-function drive-strength swap
    (``a21oi_1`` -> ``a21oi_2``, an inserted ``buf_4``) becomes ordinary
    foldable primitive logic that ``opt`` collapses, where ``opt_clean``
    alone would only have removed already-dead cells. On the real GCD
    pre/post-route pair this repo's own canary runs, that takes stage 1's
    residual unproven-``$equiv``-cell count from 98 to 93 (controlled A/B on
    one netlist pair, ``openroad`` 26Q3-1510-g6cb3f2b704 + sky130A, Yosys
    ``0.67+post``, 2026-08-24). It does **not** on its own make that design
    converge -- what closes the remaining gap is stage 1's cut-point
    refinement loop (see this module's docstring); the value of this pass is
    that fewer wrongly-paired wires survive to be blacklisted there, so the
    proof that does converge drops fewer obligations.

    ``-noff`` (skip the ``opt_dff`` sub-pass) is deliberate, not incidental:
    it keeps every flip-flop cell and name **completely untouched** by this
    normalization -- ``opt_dff`` can merge/retime/remove registers, which
    would undercut the register-name correspondence ``equiv_make``'s
    name-based matching (and so this whole engine) depends on.
    """
    lines: list[str] = []
    if side.get("liberty"):
        # No `-lib`: turns each cell's liberty `function` string into
        # real logic (a plain module `flatten` can inline), not an
        # opaque blackbox -- see `_resolve_side`'s `liberty` docs.
        # `-ignore_miss_func` skips any cell lacking a `function` spec
        # (e.g. a pure sequential cell) rather than erroring the whole
        # liberty read; such a cell surfaces instead as a missing-
        # submodule `hierarchy` error if the netlist actually instantiates
        # it.
        lines.append(f"read_liberty -ignore_miss_func {side['liberty']}")
    for path in side["sources"]:
        lines.append(f"read_verilog {path}")
    lines += [
        "proc",
        "opt_clean",
        f"hierarchy -check -top {side['top']}",
        "flatten",
        "opt_clean",
    ]
    if extra_opt:
        lines.append("opt -noff")
    if label == "gate" and port_map:
        lines.append(f"select -module {side['top']}")
        for gate_port, gold_port in port_map.items():
            lines.append(f"rename {gate_port} {gold_port}")
        lines.append("select -clear")
    lines.append(f"design -stash {label}_design")
    return lines


def _write_script(
    *,
    script_path: str,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    netlist_path: str,
    sat_timeout_s: int,
) -> None:
    """Generate the ``.ys`` equivalence-check script.

    The recipe (canonical, from the Yosys manual's own equivalence-checking
    worked example): read + ``proc`` + flatten each side into a single
    module under its own design *stash* namespace (so both sides may
    legally share a top-level module name, e.g. both called ``top``);
    reject either side if it contains sequential cells (combinational-only
    MVP -- see :data:`_SEQUENTIAL_CELL_GLOBS`); copy both stashed
    top-modules into the current design as ``gold``/``gate``; write them
    out as one combined, flattened Verilog file (reused for the
    counterexample-confirmation testbench, see :func:`_confirm_counterexample`);
    build a ``miter -equiv`` circuit with an assertion that the two never
    diverge; and ask ``sat -prove-asserts`` to prove or refute it.

    Every path embedded in the script is absolute, so the script runs
    correctly regardless of the invoking process's own working directory.
    """
    lines: list[str] = []

    for label, side in (("gold", gold), ("gate", gate)):
        lines += _side_prep_lines(label, side, port_map)

    lines.append(f"design -copy-from gold_design -as gold {gold['top']}")
    lines.append(f"design -copy-from gate_design -as gate {gate['top']}")
    lines.append(
        "select -assert-none "
        + " ".join(f"gold/t:{glob} gate/t:{glob}" for glob in _SEQUENTIAL_CELL_GLOBS)
    )
    lines.append(f"write_verilog -noattr {netlist_path}")
    lines.append("miter -equiv -make_assert -make_outputs -flatten gold gate miter")
    lines.append("flatten miter")
    lines.append("hierarchy -top miter")
    lines.append(f"sat -prove-asserts -show-ports -timeout {sat_timeout_s} miter")

    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise EquivError(
            f"could not write equiv script '{script_path}': {exc}"
        ) from exc


def _sequential_error_message(stdout: str, stderr: str) -> str | None:
    """Translate the ``select -assert-none`` sequential-cell guard's Yosys
    error into a clear, actionable :class:`EquivError` message -- ``None``
    if neither stream shows that specific failure (the caller falls back to
    :func:`_yosys_error_message`).
    """
    combined = f"{stdout}\n{stderr}"
    if "Assertion failed: selection is not empty:" not in combined:
        return None
    if not any(f"t:{glob}" in combined for glob in _SEQUENTIAL_CELL_GLOBS):
        return None
    return (
        "combinational-only MVP: gold and/or gate contains sequential "
        "elements (flip-flops, latches, or memories). klt equiv's Phase 0 "
        "scope (issue #707) is combinational equivalence only -- sequential "
        "designs need a future phase (temporal induction / BMC via "
        "SymbiYosys)."
    )


def _yosys_error_message(stdout: str, stderr: str, returncode: int | None) -> str:
    """Build an actionable error message from a failed ``yosys -s`` run --
    mirrors ``synthesize.py``'s ``_synthesis_error_message``: prefers the
    last ``ERROR:`` line Yosys itself printed, falling back to a short tail
    of captured output.

    When the error line is the ``Can't open script file `<path>' for
    reading: No such file or directory`` shape *and* ``<path>`` verifiably
    exists on the host filesystem, appends a hint that the ``yosys`` on
    ``$PATH`` is likely a WASI-sandboxed build (e.g. ``yowasp-yosys``) whose
    sandbox does not preopen that path -- see issue #1368/#1755. A script
    path that genuinely does not exist is a different failure and is left
    unchanged.
    """
    for stream in (stderr, stdout):
        error_lines = [line.strip() for line in stream.splitlines() if "ERROR:" in line]
        if error_lines:
            message = f"yosys equivalence check failed: {error_lines[-1]}"
            message += wasi_sandbox_hint_if_applicable(error_lines[-1])
            return message

    tail_source = (stderr or stdout).strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    return f"yosys exited with code {returncode}: {snippet}"


def _classify_sat_result(stdout: str) -> tuple[str, list[dict[str, str]]]:
    """Classify a completed ``sat -prove-asserts`` run's stdout into
    ``(status, diagnostics)``.

    Precedence: an internal solver timeout always wins (never
    ``"equivalent"``, per this module's own scope), then a proven
    counterexample, then a proven equivalence. Anything else (unexpected
    output shape -- should not happen with the fixed recipe this module
    generates, but defensively handled rather than crashing) is reported
    ``"inconclusive"`` with a diagnostic explaining why.
    """
    if _SAT_TIMEOUT_RE.search(stdout):
        return "inconclusive", [
            {
                "severity": "error",
                "code": "solver_timeout",
                "message": "yosys's internal SAT solver hit its own --timeout "
                "before reaching a verdict -- proof is inconclusive, not "
                "'equivalent'",
            }
        ]
    if _SAT_FAIL_RE.search(stdout):
        return "counterexample", []
    if _SAT_SUCCESS_RE.search(stdout):
        return "equivalent", []
    return "inconclusive", [
        {
            "severity": "error",
            "code": "unrecognized_solver_output",
            "message": "could not determine a SAT proof verdict from yosys's "
            "output -- proof is inconclusive, not 'equivalent'",
        }
    ]


def _parse_signal_table(stdout: str) -> dict[str, str]:
    """Parse the ``Signal Name / Dec / Hex / Bin`` table ``sat -show-ports``
    prints for a found model into ``{signal_name: bin_string}``.

    Only the ``Bin`` column is used -- ``sat``'s ``Dec``/``Hex`` columns
    render as ``--`` for wide buses (observed live on a 24x24-bit
    multiplier miter: Yosys 0.33), while ``Bin`` always carries the full,
    exact-width bit string. Empty dict if the table is not found (should
    not happen when :func:`_classify_sat_result` reported
    ``"counterexample"``).
    """
    lines = stdout.splitlines()
    signals: dict[str, str] = {}
    seen_header = False
    in_table = False
    for line in lines:
        if not seen_header:
            if _SIGNAL_TABLE_HEADER_RE.search(line):
                seen_header = True
            continue
        if not in_table:
            if line.strip().startswith("---"):
                in_table = True
            continue
        stripped = line.strip()
        if not stripped:
            break
        parts = stripped.split()
        if len(parts) < 2:
            break
        name = parts[0].lstrip("\\")
        signals[name] = parts[-1]
    return signals


def _decode_bin(bin_str: str) -> int | None:
    """Decode a solver ``Bin`` value to an ``int``, or ``None`` when it
    contains an undef/don't-care bit (``x``/``-``/``z``)."""
    if all(ch in "01" for ch in bin_str):
        return int(bin_str, 2)
    return None


def _build_counterexample(signals: dict[str, str]) -> dict[str, Any]:
    """Turn the parsed miter signal table into the documented
    ``counterexample`` shape: ``inputs``/``gold_outputs``/``gate_outputs``
    (each ``{name: {bin, width, value}}``) plus ``diverging_outputs``.
    """
    inputs: dict[str, Any] = {}
    gold_outputs: dict[str, Any] = {}
    gate_outputs: dict[str, Any] = {}

    for name, bin_str in signals.items():
        entry = {"bin": bin_str, "width": len(bin_str), "value": _decode_bin(bin_str)}
        if name.startswith("in_"):
            inputs[name[len("in_") :]] = entry
        elif name.startswith("gold_"):
            gold_outputs[name[len("gold_") :]] = entry
        elif name.startswith("gate_"):
            gate_outputs[name[len("gate_") :]] = entry
        # "trigger" (and any other miter-internal signal) is intentionally
        # ignored -- it is not part of either module's own interface.

    diverging = sorted(
        name
        for name in gold_outputs
        if name in gate_outputs
        and gold_outputs[name]["bin"] != gate_outputs[name]["bin"]
    )

    return {
        "inputs": inputs,
        "gold_outputs": gold_outputs,
        "gate_outputs": gate_outputs,
        "diverging_outputs": diverging,
        "confirmed_by_simulation": None,
        "simulation": None,
        "simulation_cross_check": None,
    }


def _resolve_sim_backend(sim_backend: str | None, request_doc: dict[str, Any]) -> str:
    """The effective replay backend: the explicit ``sim_backend`` argument
    (the CLI's ``--sim-backend``) when given, else ``request.sim_backend``,
    else :data:`DEFAULT_SIM_BACKEND` -- the same override-the-request-field
    precedence ``timeout_s`` already has.

    Raises :class:`EquivError` for an unsupported value, exactly as an
    unsupported ``engine`` does.
    """
    effective = sim_backend
    if effective is None:
        effective = request_doc.get("sim_backend", DEFAULT_SIM_BACKEND)
    if effective not in SUPPORTED_SIM_BACKENDS:
        raise EquivError(
            f"unsupported sim_backend '{effective}' "
            f"(supported: {', '.join(SUPPORTED_SIM_BACKENDS)})"
        )
    return effective


def _replay_backends(sim_backend: str) -> tuple[str, str | None]:
    """``(canonical, cross_check)`` replay backends for ``sim_backend``.

    ``"both"`` pairs the canonical ``iverilog`` replay with a ``verilator``
    cross-check; every other value runs that one backend alone (no
    cross-check, and -- importantly -- no silent fallback to the other).
    """
    if sim_backend == "both":
        return (CANONICAL_SIM_BACKEND, CROSS_CHECK_SIM_BACKEND)
    return (sim_backend, None)


def _run_replay_backend(
    *,
    backend: str,
    netlist_path: str,
    tb_path: str,
    output_dir: str,
    stem: str,
) -> tuple[str | None, str | None]:
    """Compile and run the generated replay testbench ``tb_path`` against
    the flattened ``netlist_path`` under ``backend``.

    Returns ``(stdout, None)`` on a completed run, or ``(None, message)``
    naming exactly why the replay could not be completed (missing binary,
    compile timeout, compile error, run failure). Never raises and never
    falls back to the other backend -- an unavailable backend is reported
    as unavailable, never fabricated, per this repo's own convention.
    """
    if backend == "verilator":
        mdir = os.path.join(output_dir, f"{stem}_verilator")
        exe_name = f"{stem}_verilator_bin"
        compile_cmd = [
            "verilator",
            "--binary",
            # Yosys-written netlists routinely trip Verilator *lint*
            # warnings (module name vs. file name, unused signals) that say
            # nothing about the replayed values; the replay is a value
            # check, not a lint gate. Real errors still fail the compile.
            "-Wno-fatal",
            "--top-module",
            "equiv_tb",
            "--Mdir",
            mdir,
            "-o",
            exe_name,
            netlist_path,
            tb_path,
        ]
        compile_tool = "verilator"
        compile_timeout_s = _VERILATOR_COMPILE_TIMEOUT_S
        run_cmd = [os.path.join(mdir, exe_name)]
        run_tool = "the verilator --binary executable"
    else:
        vvp_path = os.path.join(output_dir, f"{stem}.vvp")
        compile_cmd = ["iverilog", "-g2012", "-o", vvp_path, netlist_path, tb_path]
        compile_tool = "iverilog"
        compile_timeout_s = _IVERILOG_COMPILE_TIMEOUT_S
        run_cmd = ["vvp", vvp_path]
        run_tool = "vvp"

    try:
        compiled = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=compile_timeout_s,
        )
    except FileNotFoundError:
        return (
            None,
            f"{compile_tool} not found on $PATH -- counterexample "
            "reported by the solver only, not independently confirmed "
            "by simulation",
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"{compile_tool} did not complete within {compile_timeout_s}s "
            "while compiling the counterexample-confirmation testbench",
        )

    if compiled.returncode != 0:
        tail = (compiled.stderr or compiled.stdout).strip()[-500:]
        return (
            None,
            f"{compile_tool} failed to compile the counterexample-"
            f"confirmation testbench: {tail}",
        )

    try:
        ran = subprocess.run(
            run_cmd, capture_output=True, text=True, timeout=_REPLAY_RUN_TIMEOUT_S
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return (
            None,
            f"could not run confirmation testbench with {run_tool}: {exc}",
        )

    return (ran.stdout or "", None)


def _parse_replay_outputs(stdout: str) -> tuple[dict[str, str], dict[str, str]]:
    """Split a single-vector replay run's ``stdout`` into
    ``({gold outputs}, {gate outputs})`` raw ``{name: bit string}`` maps."""
    sim_gold: dict[str, str] = {}
    sim_gate: dict[str, str] = {}
    for line in stdout.splitlines():
        match = _SIM_DISPLAY_RE.match(line.strip())
        if not match:
            continue
        side, name, bits = match.groups()
        (sim_gold if side == "gold" else sim_gate)[name] = bits
    return sim_gold, sim_gate


def _diverging_names(gold: dict[str, str], gate: dict[str, str]) -> list[str]:
    """Output names present on both sides whose replayed bits differ."""
    return sorted(name for name in gold if name in gate and gold[name] != gate[name])


def _explains_two_state(canonical: str | None, cross: str | None) -> bool:
    """Whether a 2-state backend's ``cross`` value differs from the 4-state
    canonical value ``canonical`` *only* where the canonical value is
    undefined (``x``/``z``).

    This is the single declared backend-specific difference between the two
    replay backends (see this module's docstring): Verilator reads ``0``
    where Icarus reads ``x``. Any other difference -- a differing *defined*
    bit, a differing width, a signal one backend did not report at all --
    is a real disagreement, never explained away.
    """
    if canonical is None or cross is None:
        return False
    if len(canonical) != len(cross):
        return False
    return all(
        canonical_bit == cross_bit or canonical_bit in "xzXZ"
        for canonical_bit, cross_bit in zip(canonical, cross, strict=True)
    )


def _compare_replay_outputs(
    *,
    canonical: dict[str, dict[str, str]],
    cross: dict[str, dict[str, str]],
    cross_four_state: bool,
    cycle: int | None = None,
) -> list[dict[str, Any]]:
    """Per-signal differences between two backends' replayed outputs.

    ``canonical``/``cross`` are ``{"gold": {name: bits}, "gate": {...}}``.
    Each returned entry is ``{side, name, canonical, cross_check,
    explained_by}`` (plus ``cycle`` for a multi-cycle replay), where
    ``explained_by`` is ``"two_state_backend"`` for a difference the
    declared 2-state/4-state modelling gap accounts for and ``None`` for a
    genuine disagreement.
    """
    mismatches: list[dict[str, Any]] = []
    for side in ("gold", "gate"):
        canonical_side = canonical.get(side, {})
        cross_side = cross.get(side, {})
        for name in sorted(set(canonical_side) | set(cross_side)):
            canonical_bits = canonical_side.get(name)
            cross_bits = cross_side.get(name)
            if canonical_bits == cross_bits:
                continue
            explained = (
                "two_state_backend"
                if not cross_four_state
                and _explains_two_state(canonical_bits, cross_bits)
                else None
            )
            entry: dict[str, Any] = {
                "side": side,
                "name": name,
                "canonical": canonical_bits,
                "cross_check": cross_bits,
                "explained_by": explained,
            }
            if cycle is not None:
                entry["cycle"] = cycle
            mismatches.append(entry)
    return mismatches


def _cross_check_block(backend: str, *, sequential: bool) -> dict[str, Any]:
    """A ``simulation_cross_check`` block pre-filled for a cross-check that
    has not run (yet, or at all): every result field ``None``,
    ``agreement: "unavailable"``.

    A cross-check backend that could not be run is reported as absent, never
    fabricated and never silently replaced by the canonical backend's own
    numbers -- the caller fills the result fields in only when the run
    actually completed.
    """
    block: dict[str, Any] = {
        "engine": _SIM_ENGINE_LABEL[backend],
        "engine_version": _sim_backend_version(backend),
        "four_state": _SIM_BACKEND_FOUR_STATE[backend],
        "confirmed_by_simulation": None,
        "agreement": "unavailable",
        "output_mismatches": [],
    }
    if sequential:
        block["cycles"] = None
        block["diverging_outputs"] = None
    else:
        block["gold_outputs"] = None
        block["gate_outputs"] = None
        block["diverging_outputs"] = None
    return block


def _record_cross_check_agreement(
    *,
    counterexample: dict[str, Any],
    cross_check: dict[str, Any],
    canonical_backend: str,
    cross_backend: str,
    canonical_confirmed: bool,
    cross_confirmed: bool,
    mismatches: list[dict[str, Any]],
    diagnostics: list[dict[str, str]],
) -> None:
    """Apply this module's backend-agreement policy to a completed
    cross-check run, mutating ``counterexample`` and ``diagnostics``.

    Agreement requires **both** the same confirmation verdict and no
    unexplained replayed-output difference. On a disagreement the canonical
    verdict is *not* silently preferred: ``confirmed_by_simulation`` is
    reset to ``None`` and an error-severity ``sim_backend_disagreement``
    diagnostic is appended (the caller downgrades ``status`` to
    ``"inconclusive"``).
    """
    unexplained = [entry for entry in mismatches if entry["explained_by"] is None]
    verdicts_agree = cross_confirmed == canonical_confirmed
    agree = verdicts_agree and not unexplained

    cross_check["confirmed_by_simulation"] = cross_confirmed
    cross_check["agreement"] = "agree" if agree else "disagree"
    cross_check["output_mismatches"] = mismatches
    counterexample["simulation_cross_check"] = cross_check

    if not agree:
        reasons = []
        if not verdicts_agree:
            reasons.append(
                f"{canonical_backend} reported "
                f"confirmed_by_simulation={canonical_confirmed} but "
                f"{cross_backend} reported {cross_confirmed}"
            )
        if unexplained:
            differing = ", ".join(
                f"{entry['side']}.{entry['name']} "
                f"({canonical_backend}={entry['canonical']!r}, "
                f"{cross_backend}={entry['cross_check']!r})"
                for entry in unexplained[:5]
            )
            reasons.append(f"replayed outputs differ on {differing}")
        counterexample["confirmed_by_simulation"] = None
        diagnostics.append(
            {
                "severity": "error",
                "code": "sim_backend_disagreement",
                "message": "the two counterexample-replay backends "
                f"disagreed: {'; '.join(reasons)} -- neither verdict is "
                "adopted (a backend disagreement is never silently "
                "resolved in favour of one backend), so the replay "
                "evidence is not trustworthy and the proof is reported "
                "inconclusive",
            }
        )
    elif mismatches:
        differing = ", ".join(
            f"{entry['side']}.{entry['name']}" for entry in mismatches[:5]
        )
        diagnostics.append(
            {
                "severity": "info",
                "code": "sim_backend_output_difference",
                "message": f"{cross_backend}'s replay agreed with "
                f"{canonical_backend}'s verdict; the replayed bits differ "
                f"on {differing}, entirely where the 4-state "
                f"{canonical_backend} run reported x/z and the 2-state "
                f"{cross_backend} run reported a defined value "
                "(explained_by: two_state_backend)",
            }
        )


def _replay_backends_disagreed(counterexample: dict[str, Any]) -> bool:
    """Whether a ``sim_backend: "both"`` run's two replay backends
    disagreed -- the caller's signal to downgrade ``status``."""
    cross_check = counterexample.get("simulation_cross_check")
    return bool(cross_check) and cross_check.get("agreement") == "disagree"


def _replay_evidence_is_untrustworthy(counterexample: dict[str, Any]) -> bool:
    """Whether the replay evidence for ``counterexample`` forbids reporting
    ``status: "counterexample"``: either the replay ran and did *not*
    reproduce the divergence, or the two replay backends disagreed.

    ``confirmed_by_simulation is None`` *because the replay could not be
    attempted at all* is deliberately not included -- that case has no
    evidence either way, so the solver's own verdict stands (a disagreement
    also sets the field to ``None``, and is caught by the second clause)."""
    return counterexample[
        "confirmed_by_simulation"
    ] is False or _replay_backends_disagreed(counterexample)


def _confirm_counterexample(
    *,
    counterexample: dict[str, Any],
    netlist_path: str,
    output_dir: str,
    diagnostics: list[dict[str, str]],
    sim_backend: str = DEFAULT_SIM_BACKEND,
) -> None:
    """Independently confirm ``counterexample`` by actually running it
    through the flattened ``gold``/``gate`` netlists Yosys wrote to
    ``netlist_path``, via the selected replay backend (``iverilog``/``vvp``
    by default) -- never trusting the SAT solver's own reported values
    uncritically (this module's own "the counterexample is executable"
    discipline).

    Mutates ``counterexample`` in place (``confirmed_by_simulation``,
    ``simulation``, and -- under ``sim_backend: "both"`` --
    ``simulation_cross_check``); appends to ``diagnostics`` on any
    degradation (a missing simulator binary, a compile error, a
    re-simulation that does *not* reproduce the divergence the solver
    reported, or a disagreement between the two replay backends).
    """
    tb_path = os.path.join(output_dir, "equiv_tb.v")

    tb_source = _build_testbench(counterexample)
    try:
        with open(tb_path, "w", encoding="utf-8") as handle:
            handle.write(tb_source)
    except OSError as exc:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": f"could not write confirmation testbench: {exc}",
            }
        )
        return

    canonical_backend, cross_backend = _replay_backends(sim_backend)

    stdout, failure = _run_replay_backend(
        backend=canonical_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_tb",
    )
    if stdout is None:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": failure or "counterexample replay did not run",
            }
        )
        return

    sim_gold, sim_gate = _parse_replay_outputs(stdout)
    sim_diverging = _diverging_names(sim_gold, sim_gate)

    counterexample["simulation"] = {
        "engine": _SIM_ENGINE_LABEL[canonical_backend],
        "engine_version": _sim_backend_version(canonical_backend),
        "four_state": _SIM_BACKEND_FOUR_STATE[canonical_backend],
        "gold_outputs": sim_gold,
        "gate_outputs": sim_gate,
        "diverging_outputs": sim_diverging,
    }
    confirmed = bool(sim_diverging)
    counterexample["confirmed_by_simulation"] = confirmed
    if not confirmed:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "counterexample_not_reproduced",
                "message": "re-running the solver's counterexample through "
                "the flattened netlists via "
                f"{_SIM_BACKEND_LABEL[canonical_backend]} did not reproduce "
                "a diverging output -- treat this counterexample with "
                "suspicion",
            }
        )

    if cross_backend is None:
        return

    cross_stdout, cross_failure = _run_replay_backend(
        backend=cross_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_tb",
    )
    if cross_stdout is None:
        counterexample["simulation_cross_check"] = _cross_check_block(
            cross_backend, sequential=False
        )
        diagnostics.append(
            {
                "severity": "warning",
                "code": "sim_backend_cross_check_unavailable",
                "message": f"{cross_failure} -- the canonical "
                f"{canonical_backend} replay above stands unchanged",
            }
        )
        return

    cross_gold, cross_gate = _parse_replay_outputs(cross_stdout)
    cross_check = _cross_check_block(cross_backend, sequential=False)
    cross_check["gold_outputs"] = cross_gold
    cross_check["gate_outputs"] = cross_gate
    cross_check["diverging_outputs"] = _diverging_names(cross_gold, cross_gate)

    _record_cross_check_agreement(
        counterexample=counterexample,
        cross_check=cross_check,
        canonical_backend=canonical_backend,
        cross_backend=cross_backend,
        canonical_confirmed=confirmed,
        cross_confirmed=bool(cross_check["diverging_outputs"]),
        mismatches=_compare_replay_outputs(
            canonical={"gold": sim_gold, "gate": sim_gate},
            cross={"gold": cross_gold, "gate": cross_gate},
            cross_four_state=_SIM_BACKEND_FOUR_STATE[cross_backend],
        ),
        diagnostics=diagnostics,
    )


def _build_testbench(counterexample: dict[str, Any]) -> str:
    """Generate a minimal Verilog testbench that drives ``gold``/``gate``
    (as written by ``write_verilog`` to ``netlist_path``) with the exact
    concrete counterexample input vector and ``$display``s both modules'
    output vectors, so :func:`_confirm_counterexample` can compare them
    independently of the SAT solver's own report.
    """
    inputs = counterexample["inputs"]
    outputs = sorted(
        set(counterexample["gold_outputs"]) | set(counterexample["gate_outputs"])
    )

    lines = ["module equiv_tb;"]
    for name, entry in inputs.items():
        width = entry["width"]
        if width == 1:
            lines.append(f"  wire {name} = 1'b{entry['bin']};")
        else:
            lines.append(f"  wire [{width - 1}:0] {name} = {width}'b{entry['bin']};")

    for name in outputs:
        width = counterexample["gold_outputs"].get(
            name, counterexample["gate_outputs"].get(name)
        )["width"]
        if width == 1:
            lines.append(f"  wire gold_{name}, gate_{name};")
        else:
            lines.append(f"  wire [{width - 1}:0] gold_{name}, gate_{name};")

    def _ports(prefix: str) -> str:
        conns = [f".{name}({name})" for name in inputs]
        conns += [f".{name}({prefix}_{name})" for name in outputs]
        return ", ".join(conns)

    lines.append(f"  gold u_gold ({_ports('gold')});")
    lines.append(f"  gate u_gate ({_ports('gate')});")
    lines.append("  initial begin")
    lines.append("    #1;")
    for name in outputs:
        lines.append(f'    $display("EQUIV_SIM gold {name} %b", gold_{name});')
        lines.append(f'    $display("EQUIV_SIM gate {name} %b", gate_{name});')
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _sim_backend_version(backend: str) -> str | None:
    """The replay ``backend``'s own reported version string, or ``None``
    when it is not installed / does not report one -- the per-backend
    generalisation of the old ``iverilog -V``-only probe, mirroring
    ``functional_verification.py``'s ``_ENGINE_VERSION_COMMANDS`` table."""
    command, pattern = _SIM_BACKEND_VERSION_COMMANDS[backend]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or "") + (completed.stderr or "")
    match = pattern.search(text)
    return match.group(1) if match else None


def _build_report(
    *,
    engine: str,
    engine_version: str | None,
    sim_backend: str,
    status: str,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str] | None,
    timeout_s: float,
    elapsed_s: float,
    counterexample: dict[str, Any] | None,
    diagnostics: list[dict[str, str]],
    script_path: str,
    netlist_path: str,
    log_path: str,
    resume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_sources = gold["sources"] + gate["sources"]
    if len(all_sources) == 1:
        # Issue #2027: `klt equiv`'s inputs are HDL sources (the golden and
        # gate-level designs), never a layout stream -- see
        # `docs/json-contract.md`'s `provenance.input.role`.
        provenance = build_provenance(
            input_path=all_sources[0], input_role=INPUT_ROLE_SOURCE
        )
    else:
        provenance = build_provenance()
        provenance["input"] = {
            "content_hash": _combined_content_hash(all_sources),
            "role": INPUT_ROLE_SOURCE,
        }

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "sim_backend": sim_backend,
        "status": status,
        "gold": gold,
        "gate": gate,
        "port_map": port_map,
        "timeout_s": timeout_s,
        "elapsed_s": elapsed_s,
        "counterexample": counterexample,
        "diagnostics": diagnostics,
        "artifacts": {
            "script_path": script_path,
            "netlist_path": netlist_path if os.path.isfile(netlist_path) else None,
            "log_path": log_path if os.path.isfile(log_path) else None,
        },
        "provenance": provenance,
    }
    if resume is not None:
        report["resume"] = resume
    return report


# --------------------------------------------------------------------------- #
# "yosys-sequential" engine (Phase 2, #1313): register-correspondence
# sequential equivalence via equiv_make/equiv_simple/equiv_induct/
# equiv_status, with a bounded-SAT fallback (stage 2) to extract a genuine,
# confirmable counterexample when stage 1 leaves cells unproven -- see this
# module's own docstring, "Engine: yosys-sequential" section, for the full
# rationale.
# --------------------------------------------------------------------------- #


class _YosysRunResult:
    """Small return-value bundle for :func:`_run_yosys_subprocess` -- mirrors
    the local variables ``run_equiv``'s own combinational path already
    tracks inline (``timed_out``/``returncode``/``stdout``/``stderr``), just
    packaged so the sequential engine's two near-identical stages can share
    one subprocess-running helper instead of duplicating it."""

    def __init__(
        self,
        *,
        timed_out: bool,
        returncode: int | None,
        stdout: str,
        stderr: str,
        elapsed_s: float,
    ) -> None:
        self.timed_out = timed_out
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed_s = elapsed_s


def _run_yosys_subprocess(script_path: str, timeout_s: float) -> _YosysRunResult:
    """Run ``yosys -s <script_path>``, bounded by ``timeout_s`` -- the same
    subprocess-invocation shape ``run_equiv``'s own combinational path uses
    inline, factored out so the sequential engine's two stages
    (:func:`_run_sequential`) can share it."""
    started = time.monotonic()
    timed_out = False
    stdout = ""
    stderr = ""
    returncode: int | None = None
    try:
        completed = subprocess.run(
            ["yosys", "-s", script_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        raise EquivError(f"could not launch yosys: {exc}") from exc
    elapsed_s = round(time.monotonic() - started, 3)
    return _YosysRunResult(
        timed_out=timed_out,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_s=elapsed_s,
    )


def _write_sequential_stage1_script(
    *,
    script_path: str,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    netlist_path: str,
    induction_depth: int,
    blacklist_path: str | None = None,
) -> None:
    """Generate stage 1's ``.ys`` script: the named register-correspondence
    technique (``equiv_make``/``equiv_simple``/``equiv_induct``/
    ``equiv_status``) -- see this module's docstring "Engine:
    yosys-sequential" section for the full recipe rationale.

    ``blacklist_path``, when given, is passed to ``equiv_make -blacklist``:
    the wire names listed in that file are not paired, so no ``$equiv``
    cell -- and so no cut point -- is created for them. It is written by
    stage 1's own cut-point refinement loop (:func:`_run_sequential`, issue
    #1353) and never contains a top-level port; see this module's docstring,
    "Stage 1 cut-point refinement", for why removing cut points is sound.

    Also writes ``netlist_path`` (the plain, flattened, still-really-clocked
    gold/gate netlist, *before* ``equiv_make``/``clk2fflogic`` run) --
    reused both by :func:`_parse_module_ports` (to discover the shared
    gold/gate port list) and by the stage-2 counterexample-confirmation
    testbench (:func:`_confirm_sequential_counterexample`), the same
    "write the netlist once, reuse it for confirmation" convention
    :func:`_write_script` already uses for the combinational engine.
    """
    lines: list[str] = []
    for label, side in (("gold", gold), ("gate", gate)):
        lines += _side_prep_lines(label, side, port_map, extra_opt=True)

    lines.append(f"design -copy-from gold_design -as gold {gold['top']}")
    lines.append(f"design -copy-from gate_design -as gate {gate['top']}")
    lines.append(f"write_verilog -noattr {netlist_path}")
    if blacklist_path:
        lines.append(f"equiv_make -blacklist {blacklist_path} gold gate equiv")
    else:
        lines.append("equiv_make gold gate equiv")
    lines.append("hierarchy -top equiv")
    # `clk2fflogic` (not `async2sync`): handles single- and multi-clock,
    # sync- and async-reset designs uniformly via Yosys's own formal-
    # verification `$ff` model -- see module docstring.
    lines.append("clk2fflogic")
    lines.append("equiv_simple")
    lines.append(f"equiv_induct -seq {induction_depth}")
    lines.append("equiv_status")

    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise EquivError(
            f"could not write equiv script '{script_path}': {exc}"
        ) from exc


def _write_sequential_stage2_script(
    *,
    script_path: str,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    ports: dict[str, str],
    bmc_depth: int,
    sat_timeout_s: int,
) -> None:
    """Generate stage 2's ``.ys`` script: a genuinely bounded (not
    inductive) ``sat -seq <bmc_depth> -set-init-zero -prove-asserts`` search
    for an actual violating trace, over the same gold/gate pairing rebuilt
    with ``equiv_make -make_assert`` -- see this module's docstring "Engine:
    yosys-sequential" section for why this stage exists and why
    ``-set-init-zero`` is required.

    ``-show <name>_gold -show <name>_gate`` is passed explicitly for every
    port in ``ports`` (rather than the broader ``-show-public``) so a
    genuine counterexample's dump is limited to the design's own actual
    interface -- :func:`_parse_module_ports` discovers ``ports`` from the
    plain netlist stage 1 already wrote.
    """
    lines: list[str] = []
    for label, side in (("gold", gold), ("gate", gate)):
        lines += _side_prep_lines(label, side, port_map, extra_opt=True)

    lines.append(f"design -copy-from gold_design -as gold {gold['top']}")
    lines.append(f"design -copy-from gate_design -as gate {gate['top']}")
    lines.append("equiv_make -make_assert gold gate equiv2")
    lines.append("hierarchy -top equiv2")
    lines.append("clk2fflogic")
    show_args = " ".join(
        f"-show {name}_gold -show {name}_gate" for name in sorted(ports)
    )
    lines.append(
        f"sat -seq {bmc_depth} -set-init-zero -prove-asserts -show-ports "
        f"{show_args} -timeout {sat_timeout_s} equiv2"
    )

    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise EquivError(
            f"could not write equiv script '{script_path}': {exc}"
        ) from exc


def _parse_module_ports(netlist_path: str, module_name: str) -> dict[str, str]:
    """Parse ``module_name``'s ``input``/``output``/``inout`` port
    declarations out of the flattened Verilog ``write_verilog -noattr``
    already wrote to ``netlist_path`` (see
    :func:`_write_sequential_stage1_script`) -- ``{port_name: direction}``.

    Gold and gate share identical port names by construction (``equiv_make``
    can only match wires with identical names -- a request whose gold/gate
    ports differ needs ``port_map``, exactly as the combinational engine
    already requires), so parsing the ``gold`` module block alone gives the
    complete, correct port list for both sides. ``inout`` ports are recorded
    but treated the same as ``output`` everywhere else in this module (rare
    at the gate-level netlists this engine targets; not specially modelled).

    Port names are returned with Yosys's leading ``\\`` escape stripped --
    the same spelling :func:`_parse_unproven_equiv_signals` produces, which
    is what makes ``_run_sequential``'s "is this unproven signal a top-level
    port?" test comparable at all (issue #1999), and the same spelling
    ``sat -show``'s own signal table is parsed back into by
    :func:`_parse_multicycle_signal_table`. Use :func:`_verilog_ident` when
    emitting one of these names back into generated Verilog.
    """
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise EquivError(
            f"could not read generated netlist '{netlist_path}': {exc}"
        ) from exc

    ports: dict[str, str] = {}
    in_module = False
    for line in text.splitlines():
        if not in_module:
            match = _MODULE_START_RE.match(line)
            if match and match.group(1) == module_name:
                in_module = True
            continue
        if _MODULE_END_RE.match(line):
            break
        match = _PORT_DECL_RE.match(line)
        if match:
            direction, name = match.groups()
            ports.setdefault(name.lstrip("\\"), direction)
    return ports


def _verilog_ident(name: str) -> str:
    """Render ``name`` -- a port name as
    :func:`_parse_module_ports`/:func:`_parse_multicycle_signal_table`
    return it, i.e. already stripped of Yosys's leading ``\\`` -- as a
    legal Verilog identifier for emission into generated source.

    Simple identifiers pass through verbatim, so every testbench this
    module already generated is byte-for-byte unchanged. A name that is not
    a simple identifier (a flattened hierarchical output ``q.x``, a
    bit-blasted ``q[0]``) becomes a Verilog *escaped* identifier: a leading
    ``\\`` and a **trailing space**, which is the escape's terminator and
    therefore part of the token, not decoration (issue #1999).
    """
    if _SIMPLE_IDENT_RE.match(name):
        return name
    return f"\\{name} "


def _parse_unproven_equiv_signals(stdout: str) -> set[str]:
    """Extract the *original* wire names behind ``equiv_status``'s own
    ``Unproven $equiv ...: \\foo_gold \\foo_gate`` report lines -- the input
    to stage 1's cut-point refinement loop (:func:`_run_sequential`, issue
    #1353).

    ``equiv_make`` renames each side's copy of a paired wire by appending
    ``_gold``/``_gate``, so the name to feed back to ``equiv_make
    -blacklist`` is the reported signal with that suffix (and Yosys's
    leading ``\\`` escape) stripped. Bit-select tokens (``[3]``) and the
    synthetic ``$auto$...`` names Yosys generates for its own internal cells
    carry no such suffix and are ignored: only real, named wires can be
    blacklisted by name, and it is exactly the real, named wires a P&R tool
    renames or repurposes.
    """
    names: set[str] = set()
    for match in _UNPROVEN_EQUIV_RE.finditer(stdout):
        for token in match.group(1).split():
            token = token.lstrip("\\")
            for suffix in ("_gold", "_gate"):
                if token.endswith(suffix):
                    base = token[: -len(suffix)]
                    if base:
                        names.add(base)
                    break
    return names


def _write_equiv_blacklist(path: str, names: set[str]) -> None:
    """Write ``names``, one per line, as an ``equiv_make -blacklist`` file."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            for name in sorted(names):
                handle.write(f"{name}\n")
    except OSError as exc:
        raise EquivError(f"could not write equiv blacklist '{path}': {exc}") from exc


def _parse_multicycle_signal_table(stdout: str) -> dict[str, dict[str, str]]:
    """Parse ``sat -seq N -show-...``'s multi-cycle ``Time Signal Name ...
    Bin`` table into ``{time_label: {signal_name: bin_string}}``.

    ``time_label`` is ``"init"`` for the pre-first-cycle sample-tracking
    block ``clk2fflogic``'s own internal helper signals populate (discarded
    by :func:`_build_sequential_counterexample`, which only wants the
    numbered cycles) or a stringified cycle number (``"1"``, ``"2"``, ...)
    for every other block. Mirrors :func:`_parse_signal_table`'s own
    whitespace-split parsing, generalised for the extra leading "Time"
    column and the repeated per-timestep table blocks."""
    lines = stdout.splitlines()
    cycles: dict[str, dict[str, str]] = {}
    seen_header = False
    in_table = False
    for line in lines:
        if not seen_header:
            if _SEQ_TIME_HEADER_RE.search(line):
                seen_header = True
            continue
        stripped = line.strip()
        if stripped.startswith("----"):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped:
            break
        parts = stripped.split()
        if len(parts) < 4:
            break
        time_label = parts[0]
        name = parts[1].lstrip("\\")
        bin_str = parts[-1]
        cycles.setdefault(time_label, {})[name] = bin_str
    return cycles


def _build_sequential_counterexample(
    cycles: dict[str, dict[str, str]], ports: dict[str, str]
) -> dict[str, Any]:
    """Turn :func:`_parse_multicycle_signal_table`'s parsed per-cycle signal
    dump into the documented multi-cycle ``counterexample`` shape: a
    ``cycles`` list (each entry the same ``inputs``/``gold_outputs``/
    ``gate_outputs``/``diverging_outputs`` shape the combinational engine's
    own :func:`_build_counterexample` produces, plus ``time``), a top-level
    ``diverging_outputs`` (the union across every cycle), and
    ``first_diverging_cycle`` (the earliest cycle with a nonempty
    ``diverging_outputs``, or ``None``).

    Every port's gold/gate values are read from the ``<name>_gold``/
    ``<name>_gate`` signal pair ``-show`` dumped (see
    :func:`_write_sequential_stage2_script`) -- for an ``input`` port, both
    sides are ``equiv_make``-matched and asserted equal by construction, so
    only the gold-side value is recorded once (matching the combinational
    engine's own single-vector ``inputs`` shape); for an ``output``/
    ``inout`` port, both sides are recorded separately so a genuine
    divergence is visible.
    """
    time_labels = sorted((label for label in cycles if label != "init"), key=int)
    cycle_entries: list[dict[str, Any]] = []
    diverging_union: set[str] = set()

    for label in time_labels:
        signals = cycles[label]
        inputs: dict[str, Any] = {}
        gold_outputs: dict[str, Any] = {}
        gate_outputs: dict[str, Any] = {}

        for name, direction in ports.items():
            gold_bin = signals.get(f"{name}_gold")
            gate_bin = signals.get(f"{name}_gate")
            if direction == "input":
                bin_str = gold_bin if gold_bin is not None else gate_bin
                if bin_str is not None:
                    inputs[name] = {
                        "bin": bin_str,
                        "width": len(bin_str),
                        "value": _decode_bin(bin_str),
                    }
            else:
                if gold_bin is not None:
                    gold_outputs[name] = {
                        "bin": gold_bin,
                        "width": len(gold_bin),
                        "value": _decode_bin(gold_bin),
                    }
                if gate_bin is not None:
                    gate_outputs[name] = {
                        "bin": gate_bin,
                        "width": len(gate_bin),
                        "value": _decode_bin(gate_bin),
                    }

        diverging = sorted(
            name
            for name in gold_outputs
            if name in gate_outputs
            and gold_outputs[name]["bin"] != gate_outputs[name]["bin"]
        )
        diverging_union.update(diverging)
        cycle_entries.append(
            {
                "time": int(label),
                "inputs": inputs,
                "gold_outputs": gold_outputs,
                "gate_outputs": gate_outputs,
                "diverging_outputs": diverging,
            }
        )

    first_diverging_cycle = next(
        (entry["time"] for entry in cycle_entries if entry["diverging_outputs"]),
        None,
    )

    return {
        "cycles": cycle_entries,
        "diverging_outputs": sorted(diverging_union),
        "first_diverging_cycle": first_diverging_cycle,
        "confirmed_by_simulation": None,
        "simulation": None,
        "simulation_cross_check": None,
    }


def _build_sequential_testbench(
    counterexample: dict[str, Any], ports: dict[str, str]
) -> str:
    """Generate a multi-cycle Verilog testbench that replays
    ``counterexample``'s entire captured cycle-by-cycle input sequence (not
    a single vector) through ``gold``/``gate`` (as written by
    ``write_verilog`` to the stage-1 ``netlist_path``) and ``$display``s
    both modules' output values after each cycle -- the sequential
    generalisation of :func:`_build_testbench`.

    Every recorded input value is driven directly, cycle by cycle
    (including any clock signal -- the real, still-``posedge``-clocked
    gold/gate modules fire on the replayed transitions exactly as a real
    testbench's clock generator would, since the solver's own ``-seq``
    trace already encodes a self-consistent clock waveform); real Verilog
    simulation semantics (undriven registers start at ``x``, not sat's own
    ``-set-init-zero`` assumption) are why
    :func:`_confirm_sequential_counterexample` checks for the reported
    divergence reproducing *somewhere* in the trace rather than at the
    identical cycle index -- see that function's own docstring.

    Port names that are not simple Verilog identifiers (a flattened
    hierarchical ``q.x``, a bit-blasted ``q[0]``) are emitted through
    :func:`_verilog_ident` so the generated testbench still compiles; the
    ``$display`` *text* keeps the plain, unescaped spelling, since that is
    what :data:`_SEQ_SIM_DISPLAY_RE` parses back out (issue #1999).
    """
    input_names = sorted(
        name for name, direction in ports.items() if direction == "input"
    )
    output_names = sorted(
        name for name, direction in ports.items() if direction != "input"
    )

    def _tb_net(prefix: str, name: str) -> str:
        """The testbench-local net carrying ``prefix``'s copy of ``name``."""
        return _verilog_ident(f"{prefix}_{name}")

    lines = ["module equiv_tb;"]
    for name in input_names:
        lines.append(f"  reg {_verilog_ident(name)};")
    for name in output_names:
        lines.append(f"  wire {_tb_net('gold', name)}, {_tb_net('gate', name)};")

    def _conns(prefix: str) -> str:
        conns = [
            f".{_verilog_ident(name)}({_verilog_ident(name)})" for name in input_names
        ]
        conns += [
            f".{_verilog_ident(name)}({_tb_net(prefix, name)})" for name in output_names
        ]
        return ", ".join(conns)

    lines.append(f"  gold u_gold ({_conns('gold')});")
    lines.append(f"  gate u_gate ({_conns('gate')});")
    lines.append("  initial begin")
    for cycle in counterexample["cycles"]:
        for name in input_names:
            entry = cycle["inputs"].get(name)
            if entry is None:
                continue
            width = entry["width"]
            if width == 1:
                lines.append(f"    {_verilog_ident(name)} = 1'b{entry['bin']};")
            else:
                lines.append(f"    {_verilog_ident(name)} = {width}'b{entry['bin']};")
        lines.append("    #1;")
        time_value = cycle["time"]
        for name in output_names:
            lines.append(
                f'    $display("EQUIV_SIM_CYCLE {time_value} gold {name} %b", '
                f"{_tb_net('gold', name)});"
            )
            lines.append(
                f'    $display("EQUIV_SIM_CYCLE {time_value} gate {name} %b", '
                f"{_tb_net('gate', name)});"
            )
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _confirm_sequential_counterexample(
    *,
    counterexample: dict[str, Any],
    ports: dict[str, str],
    netlist_path: str,
    output_dir: str,
    diagnostics: list[dict[str, str]],
    sim_backend: str = DEFAULT_SIM_BACKEND,
) -> None:
    """Multi-cycle counterpart of :func:`_confirm_counterexample`:
    independently re-runs ``counterexample``'s entire captured trace through
    the flattened ``gold``/``gate`` netlists via the selected replay backend
    (``iverilog``/``vvp`` by default -- see this module's docstring,
    "Replay backend").

    Mutates ``counterexample`` in place (``confirmed_by_simulation``,
    ``simulation``); appends to ``diagnostics`` on any degradation, exactly
    as :func:`_confirm_counterexample` does.

    **Confirmed if the reported divergence reproduces anywhere in the
    trace, not necessarily at the identical cycle index.** A real Verilog
    simulation's registers start at ``x`` (undefined) per standard Verilog
    semantics, while the solver's own ``-set-init-zero`` search assumed an
    all-zero start -- so the first one or two cycles can legitimately
    disagree on timing/settling details even for a fully accurate replay.
    Requiring only that the *same output signal* diverges *at some* replayed
    cycle -- not literally the solver's own cycle index -- is the same
    "confirm the reported divergence is real, not the solver's exact
    bookkeeping" bar :func:`_confirm_counterexample` already applies to the
    combinational engine's single-vector case, generalised for the
    additional timing degree of freedom a multi-cycle trace introduces.
    """
    if not ports:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "could not determine gold/gate module port list "
                "for the confirmation testbench",
            }
        )
        return

    tb_path = os.path.join(output_dir, "equiv_seq_tb.v")

    tb_source = _build_sequential_testbench(counterexample, ports)
    try:
        with open(tb_path, "w", encoding="utf-8") as handle:
            handle.write(tb_source)
    except OSError as exc:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": f"could not write confirmation testbench: {exc}",
            }
        )
        return

    canonical_backend, cross_backend = _replay_backends(sim_backend)

    stdout, failure = _run_replay_backend(
        backend=canonical_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_seq_tb",
    )
    if stdout is None:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": failure or "counterexample replay did not run",
            }
        )
        return

    sim_cycle_entries, sim_diverging_union = _parse_sequential_replay_outputs(stdout)

    counterexample["simulation"] = {
        "engine": _SIM_ENGINE_LABEL[canonical_backend],
        "engine_version": _sim_backend_version(canonical_backend),
        "four_state": _SIM_BACKEND_FOUR_STATE[canonical_backend],
        "cycles": sim_cycle_entries,
        "diverging_outputs": sorted(sim_diverging_union),
    }

    reported_diverging = set(counterexample["diverging_outputs"])
    confirmed = bool(sim_diverging_union & reported_diverging)
    counterexample["confirmed_by_simulation"] = confirmed
    if not confirmed:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "counterexample_not_reproduced",
                "message": "re-running the solver's counterexample trace "
                "through the flattened netlists via "
                f"{_SIM_BACKEND_LABEL[canonical_backend]} did not "
                "reproduce a diverging output on any replayed cycle -- "
                "treat this counterexample with suspicion",
            }
        )

    if cross_backend is None:
        return

    cross_stdout, cross_failure = _run_replay_backend(
        backend=cross_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_seq_tb",
    )
    if cross_stdout is None:
        counterexample["simulation_cross_check"] = _cross_check_block(
            cross_backend, sequential=True
        )
        diagnostics.append(
            {
                "severity": "warning",
                "code": "sim_backend_cross_check_unavailable",
                "message": f"{cross_failure} -- the canonical "
                f"{canonical_backend} replay above stands unchanged",
            }
        )
        return

    cross_cycle_entries, cross_diverging_union = _parse_sequential_replay_outputs(
        cross_stdout
    )
    cross_check = _cross_check_block(cross_backend, sequential=True)
    cross_check["cycles"] = cross_cycle_entries
    cross_check["diverging_outputs"] = sorted(cross_diverging_union)

    canonical_by_time = {entry["time"]: entry for entry in sim_cycle_entries}
    cross_by_time = {entry["time"]: entry for entry in cross_cycle_entries}
    mismatches: list[dict[str, Any]] = []
    for time_value in sorted(set(canonical_by_time) | set(cross_by_time)):
        canonical_cycle = canonical_by_time.get(time_value, {})
        cross_cycle = cross_by_time.get(time_value, {})
        mismatches.extend(
            _compare_replay_outputs(
                canonical={
                    "gold": canonical_cycle.get("gold_outputs", {}),
                    "gate": canonical_cycle.get("gate_outputs", {}),
                },
                cross={
                    "gold": cross_cycle.get("gold_outputs", {}),
                    "gate": cross_cycle.get("gate_outputs", {}),
                },
                cross_four_state=_SIM_BACKEND_FOUR_STATE[cross_backend],
                cycle=time_value,
            )
        )

    _record_cross_check_agreement(
        counterexample=counterexample,
        cross_check=cross_check,
        canonical_backend=canonical_backend,
        cross_backend=cross_backend,
        canonical_confirmed=confirmed,
        cross_confirmed=bool(cross_diverging_union & reported_diverging),
        mismatches=mismatches,
        diagnostics=diagnostics,
    )


def _parse_sequential_replay_outputs(
    stdout: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Split a multi-cycle replay run's ``stdout`` into its per-cycle
    ``{time, gold_outputs, gate_outputs, diverging_outputs}`` entries plus
    the union of every cycle's own diverging output names."""
    sim_cycles: dict[int, dict[str, dict[str, str]]] = {}
    for line in stdout.splitlines():
        match = _SEQ_SIM_DISPLAY_RE.match(line.strip())
        if not match:
            continue
        time_str, side, name, bits = match.groups()
        entry = sim_cycles.setdefault(int(time_str), {"gold": {}, "gate": {}})
        entry[side][name] = bits

    sim_cycle_entries: list[dict[str, Any]] = []
    sim_diverging_union: set[str] = set()
    for time_value in sorted(sim_cycles):
        gold_outputs = sim_cycles[time_value]["gold"]
        gate_outputs = sim_cycles[time_value]["gate"]
        diverging = _diverging_names(gold_outputs, gate_outputs)
        sim_diverging_union.update(diverging)
        sim_cycle_entries.append(
            {
                "time": time_value,
                "gold_outputs": gold_outputs,
                "gate_outputs": gate_outputs,
                "diverging_outputs": diverging,
            }
        )
    return sim_cycle_entries, sim_diverging_union


def _build_sequential_report(
    *,
    engine: str,
    engine_version: str | None,
    sim_backend: str,
    status: str,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str] | None,
    timeout_s: float,
    elapsed_s: float,
    induction_depth: int,
    counterexample: dict[str, Any] | None,
    diagnostics: list[dict[str, str]],
    stage1_script_path: str,
    stage1_log_path: str,
    stage2_script_path: str | None,
    stage2_log_path: str | None,
    netlist_path: str,
    stage1_blacklist_path: str | None = None,
    resume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``"yosys-sequential"`` engine's own report builder -- mirrors
    :func:`_build_report`'s shape (``schema_version``/``engine``/``status``/
    ``gold``/``gate``/``port_map``/``timeout_s``/``elapsed_s``/
    ``counterexample``/``diagnostics``/``artifacts``/``provenance``) plus
    two additive fields this engine alone needs: ``induction_depth`` (the
    effective ``equiv_induct -seq``/stage-2 ``sat -seq`` depth actually
    used) and ``artifacts.stage2_script_path``/``artifacts.stage2_log_path``
    (``None`` when stage 2 never ran, i.e. stage 1 alone reached
    ``"equivalent"``) -- ``artifacts.script_path``/``artifacts.log_path``
    keep referring to stage 1 (always run), matching the combinational
    engine's own singular ``script_path``/``log_path`` field names so a
    caller that only reads those two fields still gets a meaningful path.

    ``artifacts.stage1_blacklist_path`` (issue #1353) is the
    ``equiv_make -blacklist`` file stage 1's cut-point refinement loop wrote,
    or ``None`` when no refinement was needed -- the auditable record of
    exactly which internal wire pairings the proof dropped.

    ``resume`` (issue #2280) is the additive ``resume`` envelope block,
    attached only when ``--resume`` was requested (``None`` otherwise, so a
    run without the flag emits byte-identical output to pre-#2280 builds --
    the same present-only-when-requested convention ``klt sim``'s
    ``environment.resume`` block uses).
    """
    all_sources = gold["sources"] + gate["sources"]
    if len(all_sources) == 1:
        # Issue #2027: `klt equiv`'s inputs are HDL sources (the golden and
        # gate-level designs), never a layout stream -- see
        # `docs/json-contract.md`'s `provenance.input.role`.
        provenance = build_provenance(
            input_path=all_sources[0], input_role=INPUT_ROLE_SOURCE
        )
    else:
        provenance = build_provenance()
        provenance["input"] = {
            "content_hash": _combined_content_hash(all_sources),
            "role": INPUT_ROLE_SOURCE,
        }

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "sim_backend": sim_backend,
        "status": status,
        "gold": gold,
        "gate": gate,
        "port_map": port_map,
        "timeout_s": timeout_s,
        "elapsed_s": elapsed_s,
        "induction_depth": induction_depth,
        "counterexample": counterexample,
        "diagnostics": diagnostics,
        "artifacts": {
            "script_path": stage1_script_path,
            "netlist_path": netlist_path if os.path.isfile(netlist_path) else None,
            "log_path": stage1_log_path if os.path.isfile(stage1_log_path) else None,
            "stage2_script_path": stage2_script_path,
            "stage2_log_path": (
                stage2_log_path
                if stage2_log_path is not None and os.path.isfile(stage2_log_path)
                else None
            ),
            "stage1_blacklist_path": (
                stage1_blacklist_path
                if stage1_blacklist_path is not None
                and os.path.isfile(stage1_blacklist_path)
                else None
            ),
        },
        "provenance": provenance,
    }
    if resume is not None:
        report["resume"] = resume
    return report


def _new_blacklist_candidates(
    stdout: str, netlist_path: str, blacklisted: set[str]
) -> set[str] | None:
    """The next ``equiv_make -blacklist`` widening for stage 1's cut-point
    refinement loop (issue #1353): the names ``equiv_status`` reported
    unproven that are *not* top-level ports and not already blacklisted --
    or ``None`` when the loop must stop: an unparsable netlist (no new
    candidates can be derived) or nothing new to drop (either every
    unproven obligation is a top-level port -- a real output difference,
    stage 2's job -- or refinement has reached its fixpoint)."""
    try:
        ports = _parse_module_ports(netlist_path, "gold")
    except EquivError:
        return None
    candidates = {
        name for name in _parse_unproven_equiv_signals(stdout) if name not in ports
    }
    if candidates <= blacklisted:
        return None
    return candidates - blacklisted


def _stage1_refinement_diagnostics(
    refinements: int, blacklisted: set[str]
) -> list[dict[str, str]]:
    """The ``equiv_cutpoint_refinement`` info diagnostic, factored out of
    :func:`_run_sequential` so a resumed run can reconstruct it
    byte-identically from the committed stage record's ``refinements``/
    ``blacklist`` fields instead of re-deriving them from a Yosys run that
    (by definition of the resume) did not happen again."""
    if not blacklisted:
        return []
    return [
        {
            "severity": "info",
            "code": "equiv_cutpoint_refinement",
            "message": (
                f"stage 1 re-ran equiv_make {refinements}x with "
                f"{len(blacklisted)} internal wire(s) blacklisted -- "
                "same-named gold/gate wires that could not be proven "
                "equivalent and so were dropped as cut points (no "
                "top-level port is ever dropped; see "
                "artifacts.stage1_blacklist_path for the exact list)"
            ),
        }
    ]


def _load_stage1_record_for_resume(
    resume_block: dict[str, Any] | None,
    record_path: str,
    fingerprint: str,
    *,
    log_path: str,
    netlist_path: str,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """The resume-only record load for :func:`_run_sequential`:
    ``(record, resume_diagnostics)`` -- ``record`` is what
    :func:`_load_committed_stage1` adopted (``None`` unless ``--resume``
    was requested), and ``resume_diagnostics`` carries the
    ``resume_stage_record_discarded`` warning when a record existed but
    was rejected. A rejected record is never silently ignored: the
    envelope must describe truthfully that stage 1 re-ran because the
    commit on disk did not satisfy the resume contract (issue #2280)."""
    if resume_block is None:
        return None, []
    record, discard_reason = _load_committed_stage1(
        record_path, fingerprint, log_path=log_path, netlist_path=netlist_path
    )
    if record is None and discard_reason is not None:
        return None, [
            {
                "severity": "warning",
                "code": "resume_stage_record_discarded",
                "message": (
                    f"committed stage record '{record_path}' was "
                    f"discarded ({discard_reason}) -- stage 1 re-runs "
                    "from scratch"
                ),
            }
        ]
    return record, []


def _resume_report_from_committed_stage1(
    stage1_record: dict[str, Any],
    *,
    engine: str,
    engine_version: str | None,
    sim_backend: str,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str] | None,
    effective_timeout_s: float,
    induction_depth: int,
    script1_path: str,
    log1_path: str,
    netlist_path: str,
    blacklist_path: str,
    resume_block: dict[str, Any],
    resume_diagnostics: list[dict[str, str]],
) -> tuple[dict[str, Any] | None, set[str], int, str | None]:
    """Adopt a loader-validated stage-1 commit record (issue #2280): the
    committed record IS stage 1's outcome, so reconstruct the exact
    post-stage-1 state the interrupted run left -- blacklisted set,
    refinement count, refinement diagnostic, blacklist artifact -- without
    re-running Yosys. The loader has already corroborated the recorded
    classification against the committed log's own bytes, which is what
    makes this reconstruction safe to trust (see
    :func:`_load_committed_stage1`).

    Returns ``(report, blacklisted, refinements, active_blacklist_path)``
    where ``report`` is the final ``"equivalent"`` envelope when the
    record's classification is ``all_proven`` (the run is done -- stage 2
    must not run), or ``None`` when it is ``unproven_cells`` and the
    caller falls through to stage 2 with the reconstructed state.
    """
    blacklisted = set(stage1_record["blacklist"])
    refinements = stage1_record["refinements"]
    if blacklisted and not os.path.isfile(blacklist_path):
        # Regenerate the blacklist artifact deterministically (the same
        # sorted names `_write_equiv_blacklist` always writes), so the
        # resumed envelope's `artifacts.stage1_blacklist_path` matches the
        # uninterrupted run's even when the resume happens on a host that
        # received the record but not that one artifact file.
        _write_equiv_blacklist(blacklist_path, blacklisted)
    active_blacklist_path = blacklist_path if blacklisted else None

    resume_block["resumed_stage"] = 1  # a record is only loaded under resume
    refinement_diagnostics = resume_diagnostics + _stage1_refinement_diagnostics(
        refinements, blacklisted
    )

    if stage1_record["classification"] != _STAGE1_ALL_PROVEN:
        # classification == unproven_cells: fall through to stage 2,
        # exactly as a live stage-1 run that left cells unproven would.
        return None, blacklisted, refinements, active_blacklist_path

    return (
        _build_sequential_report(
            engine=engine,
            engine_version=engine_version,
            sim_backend=sim_backend,
            status="equivalent",
            gold=gold,
            gate=gate,
            port_map=port_map or None,
            timeout_s=effective_timeout_s,
            elapsed_s=0.0,
            induction_depth=induction_depth,
            counterexample=None,
            diagnostics=refinement_diagnostics,
            stage1_script_path=script1_path,
            stage1_log_path=log1_path,
            stage2_script_path=None,
            stage2_log_path=None,
            netlist_path=netlist_path,
            stage1_blacklist_path=active_blacklist_path,
            resume=resume_block,
        ),
        blacklisted,
        refinements,
        active_blacklist_path,
    )


def _run_sequential(
    *,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    output_dir: str,
    effective_timeout_s: float,
    induction_depth: int,
    engine: str,
    sim_backend: str = DEFAULT_SIM_BACKEND,
    resume_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``"yosys-sequential"`` engine's own top-level driver, called from
    ``run_equiv`` once ``gold``/``gate``/``port_map``/``output_dir`` are
    already resolved -- see this module's docstring, "Engine:
    yosys-sequential" section, for the two-stage rationale, and
    "Resumable runs" (issue #2280) for the ``resume`` contract: a
    fingerprint-matched, log-corroborated ``stage1.commit.json`` skips
    stage 1 (an ``all_proven`` record yields the final ``"equivalent"``
    envelope directly; an ``unproven_cells`` record re-enters at stage 2),
    and every other case re-runs stage 1, committing its own record for
    the next resume."""
    script1_path = os.path.join(output_dir, "equiv_seq_stage1.ys")
    netlist_path = os.path.join(output_dir, "equiv_seq_netlist.v")
    log1_path = os.path.join(output_dir, "equiv_seq_stage1.log")
    blacklist_path = os.path.join(output_dir, "equiv_seq_blacklist.txt")
    record_path = _stage_record_path(output_dir, 1)

    int_timeout = max(1, round(effective_timeout_s))
    engine_version = _yosys_version()
    fingerprint = _stage_fingerprint(
        gold=gold,
        gate=gate,
        port_map=port_map,
        effective_timeout_s=effective_timeout_s,
        engine=engine,
        induction_depth=induction_depth,
        sim_backend=sim_backend,
    )

    # `resumed_stage` is upgraded to 1 inside
    # `_resume_report_from_committed_stage1` iff a committed record was
    # actually adopted; a discarded record keeps it at 0, with the
    # `resume_stage_record_discarded` warning explaining why. The loader
    # only runs when `resume_block` is not None, so a default run neither
    # reads nor writes stage records.
    stage1_record, resume_diagnostics = _load_stage1_record_for_resume(
        resume_block,
        record_path,
        fingerprint,
        log_path=log1_path,
        netlist_path=netlist_path,
    )

    # Stage 1, run as a bounded cut-point refinement loop: each pass drops
    # the wrongly-paired *internal* wires the previous pass could not prove
    # (never a top-level port) from `equiv_make`'s matching, so their false
    # cut points stop poisoning every downstream proof. See this module's
    # docstring, "Stage 1 cut-point refinement" (issue #1353).
    blacklisted: set[str] = set()
    refinements = 0
    total_elapsed_s = 0.0
    active_blacklist_path: str | None = None

    if stage1_record is not None:
        # Resume reuse path (issue #2280): reconstruct stage 1's outcome
        # from the committed record. An `all_proven` record yields the
        # final envelope directly; an `unproven_cells` record falls
        # through to stage 2 with the reconstructed state.
        (
            resumed_report,
            blacklisted,
            refinements,
            active_blacklist_path,
        ) = _resume_report_from_committed_stage1(
            stage1_record,
            engine=engine,
            engine_version=engine_version,
            sim_backend=sim_backend,
            gold=gold,
            gate=gate,
            port_map=port_map or None,
            effective_timeout_s=effective_timeout_s,
            induction_depth=induction_depth,
            script1_path=script1_path,
            log1_path=log1_path,
            netlist_path=netlist_path,
            blacklist_path=blacklist_path,
            resume_block=resume_block,
            resume_diagnostics=resume_diagnostics,
        )
        if resumed_report is not None:
            return resumed_report
    else:
        while True:
            _write_sequential_stage1_script(
                script_path=script1_path,
                gold=gold,
                gate=gate,
                port_map=port_map,
                netlist_path=netlist_path,
                induction_depth=induction_depth,
                blacklist_path=active_blacklist_path,
            )

            # Every refinement pass shares the single `timeout_s` budget (the
            # first pass gets all of it, since `total_elapsed_s` is still 0), so
            # the loop can never push stage 1 past the one-stage budget the JSON
            # contract documents.
            stage1 = _run_yosys_subprocess(
                script1_path, effective_timeout_s - total_elapsed_s
            )
            total_elapsed_s = round(total_elapsed_s + stage1.elapsed_s, 3)
            try:
                with open(log1_path, "w", encoding="utf-8") as handle:
                    handle.write(stage1.stdout)
                    if stage1.stderr:
                        handle.write("\n--- stderr ---\n")
                        handle.write(stage1.stderr)
            except OSError:
                pass

            if (
                stage1.timed_out
                or stage1.returncode != 0
                or _EQUIV_NONE_FOUND_RE.search(stage1.stdout)
                or _EQUIV_ALL_PROVEN_RE.search(stage1.stdout)
                or refinements >= _MAX_STAGE1_REFINEMENTS
                or effective_timeout_s - total_elapsed_s <= 0
            ):
                break

            candidates = _new_blacklist_candidates(
                stage1.stdout, netlist_path, blacklisted
            )
            if candidates is None:
                break
            blacklisted |= candidates
            _write_equiv_blacklist(blacklist_path, blacklisted)
            active_blacklist_path = blacklist_path
            refinements += 1

        refinement_diagnostics = resume_diagnostics + _stage1_refinement_diagnostics(
            refinements, blacklisted
        )

        if stage1.timed_out:
            # Stage 1 never reached a classified outcome, so nothing is
            # committed for a later resume (issue #2280): the on-disk record is
            # (re)written as a `partial: true` marker documenting the failed
            # attempt -- which `_load_committed_stage1` can never adopt.
            _write_partial_stage_record(
                record_path, fingerprint, reason="process_timeout"
            )
            return _build_sequential_report(
                engine=engine,
                engine_version=engine_version,
                sim_backend=sim_backend,
                status="inconclusive",
                gold=gold,
                gate=gate,
                port_map=port_map or None,
                timeout_s=effective_timeout_s,
                elapsed_s=total_elapsed_s,
                induction_depth=induction_depth,
                counterexample=None,
                diagnostics=refinement_diagnostics
                + [
                    {
                        "severity": "error",
                        "code": "process_timeout",
                        "message": (
                            f"yosys (stage 1: equiv_make/equiv_induct) did not "
                            f"complete within {effective_timeout_s}s (process "
                            "killed) -- proof is inconclusive, not 'equivalent'"
                        ),
                    }
                ],
                stage1_script_path=script1_path,
                stage1_log_path=log1_path,
                stage2_script_path=None,
                stage2_log_path=None,
                netlist_path=netlist_path,
                stage1_blacklist_path=active_blacklist_path,
                resume=resume_block,
            )

        if stage1.returncode != 0:
            # No `select -assert-none` sequential-cell guard exists in this
            # engine's own script (unlike the combinational engine's -- this
            # engine exists specifically *for* sequential designs), so a
            # nonzero return here is always a genuine elaboration/build error,
            # never the combinational engine's own scope-rejection shape.
            _write_partial_stage_record(record_path, fingerprint, reason="yosys_error")
            message = _yosys_error_message(
                stage1.stdout, stage1.stderr, stage1.returncode
            )
            raise EquivError(message)

        if _EQUIV_NONE_FOUND_RE.search(stage1.stdout):
            _write_partial_stage_record(
                record_path, fingerprint, reason="no_equiv_cells"
            )
            raise EquivError(
                "yosys-sequential engine found no matching gold/gate signals to "
                "compare (equiv_make only matches identically-named wires) -- "
                "check that gold/gate share port and register names, or supply "
                "request.port_map"
            )

        # Stage 1 reached a classified outcome -- commit it (issue #2280) so a
        # later `--resume` run of this same request can re-enter from here
        # instead of re-running the (potentially multi-pass, minutes-long)
        # induction proof. Atomic write; `partial` forced false; see the
        # "Resumable runs" module-docstring section.
        stage1_all_proven = bool(_EQUIV_ALL_PROVEN_RE.search(stage1.stdout))
        _write_stage_record(
            record_path,
            {
                "stage": 1,
                "fingerprint": fingerprint,
                "classification": (
                    _STAGE1_ALL_PROVEN if stage1_all_proven else _STAGE1_UNPROVEN_CELLS
                ),
                "blacklist": sorted(blacklisted),
                "refinements": refinements,
            },
        )

        if stage1_all_proven:
            return _build_sequential_report(
                engine=engine,
                engine_version=engine_version,
                sim_backend=sim_backend,
                status="equivalent",
                gold=gold,
                gate=gate,
                port_map=port_map or None,
                timeout_s=effective_timeout_s,
                elapsed_s=total_elapsed_s,
                induction_depth=induction_depth,
                counterexample=None,
                diagnostics=refinement_diagnostics,
                stage1_script_path=script1_path,
                stage1_log_path=log1_path,
                stage2_script_path=None,
                stage2_log_path=None,
                netlist_path=netlist_path,
                stage1_blacklist_path=active_blacklist_path,
                resume=resume_block,
            )

    # Stage 1 left one or more $equiv cells unproven -- register-
    # correspondence induction alone could not decide. Stage 2 attempts a
    # genuinely bounded (complete-within-its-own-depth) SAT search for an
    # actual demonstrated counterexample; see module docstring.
    #
    # `refinement_diagnostics` is (re)built here for whichever fall-through
    # path reached this point: a live stage-1 run that left cells unproven,
    # or a resumed run whose committed record said `unproven_cells` (in
    # which case the reconstruction comes from the record's committed
    # blacklist/refinement count, not a Yosys run).
    refinement_diagnostics = resume_diagnostics + _stage1_refinement_diagnostics(
        refinements, blacklisted
    )
    ports = _parse_module_ports(netlist_path, "gold")

    script2_path = os.path.join(output_dir, "equiv_seq_stage2.ys")
    log2_path = os.path.join(output_dir, "equiv_seq_stage2.log")

    _write_sequential_stage2_script(
        script_path=script2_path,
        gold=gold,
        gate=gate,
        port_map=port_map,
        ports=ports,
        bmc_depth=induction_depth,
        sat_timeout_s=int_timeout,
    )

    stage2 = _run_yosys_subprocess(script2_path, effective_timeout_s)
    try:
        with open(log2_path, "w", encoding="utf-8") as handle:
            handle.write(stage2.stdout)
            if stage2.stderr:
                handle.write("\n--- stderr ---\n")
                handle.write(stage2.stderr)
    except OSError:
        pass

    total_elapsed_s = round(total_elapsed_s + stage2.elapsed_s, 3)

    if stage2.timed_out:
        return _build_sequential_report(
            engine=engine,
            engine_version=engine_version,
            sim_backend=sim_backend,
            status="inconclusive",
            gold=gold,
            gate=gate,
            port_map=port_map or None,
            timeout_s=effective_timeout_s,
            elapsed_s=total_elapsed_s,
            induction_depth=induction_depth,
            counterexample=None,
            diagnostics=refinement_diagnostics
            + [
                {
                    "severity": "error",
                    "code": "process_timeout",
                    "message": (
                        "yosys (stage 2: bounded counterexample search) did "
                        f"not complete within {effective_timeout_s}s "
                        "(process killed) -- proof is inconclusive, not "
                        "'equivalent'"
                    ),
                }
            ],
            stage1_script_path=script1_path,
            stage1_log_path=log1_path,
            stage2_script_path=script2_path,
            stage2_log_path=log2_path,
            netlist_path=netlist_path,
            stage1_blacklist_path=active_blacklist_path,
            resume=resume_block,
        )

    if stage2.returncode != 0:
        message = _yosys_error_message(stage2.stdout, stage2.stderr, stage2.returncode)
        raise EquivError(message)

    status2, diagnostics2 = _classify_sat_result(stage2.stdout)

    if status2 != "counterexample":
        if status2 == "equivalent":
            # A bounded, all-zero-start search found no violation within
            # `induction_depth` cycles -- weaker than the general,
            # unbounded claim register-correspondence induction itself
            # could not prove (see module docstring). Never silently
            # upgraded to "equivalent".
            diagnostics2 = diagnostics2 + [
                {
                    "severity": "warning",
                    "code": "unproven_by_induction",
                    "message": (
                        "register-correspondence induction (equiv_induct) "
                        f"could not prove full equivalence within "
                        f"{induction_depth} induction cycles, and a bounded "
                        "confirmation search (from an all-registers-zero "
                        "start state) found no counterexample either -- "
                        "treating as inconclusive rather than silently "
                        "upgrading to 'equivalent'"
                    ),
                }
            ]
        return _build_sequential_report(
            engine=engine,
            engine_version=engine_version,
            sim_backend=sim_backend,
            status="inconclusive",
            gold=gold,
            gate=gate,
            port_map=port_map or None,
            timeout_s=effective_timeout_s,
            elapsed_s=total_elapsed_s,
            induction_depth=induction_depth,
            counterexample=None,
            diagnostics=refinement_diagnostics + diagnostics2,
            stage1_script_path=script1_path,
            stage1_log_path=log1_path,
            stage2_script_path=script2_path,
            stage2_log_path=log2_path,
            netlist_path=netlist_path,
            stage1_blacklist_path=active_blacklist_path,
            resume=resume_block,
        )

    cycles = _parse_multicycle_signal_table(stage2.stdout)
    counterexample = _build_sequential_counterexample(cycles, ports)
    _confirm_sequential_counterexample(
        counterexample=counterexample,
        ports=ports,
        netlist_path=netlist_path,
        output_dir=output_dir,
        diagnostics=diagnostics2,
        sim_backend=sim_backend,
    )

    # Mirrors the combinational engine's own downgrade (see `run_equiv`
    # above): a stage-2 counterexample whose own iverilog/vvp replay does
    # not reproduce a diverging output on any cycle is not a demonstrated
    # functional difference -- it is an unproven-`$equiv`-cell artifact of
    # `equiv_make`'s name-based wire matching (see this module's own
    # docstring and issue #1349). Reporting it as "counterexample" would be
    # unsound: "inconclusive" is the honest verdict. `confirmed_by_simulation
    # is None` (simulation could not be attempted at all) is left alone --
    # that case has no evidence either way, so the solver's own verdict
    # stands, exactly as the combinational path does.
    #
    # A `sim_backend: "both"` run whose two replay backends *disagreed*
    # (issue #2223) is downgraded the same way, for the same reason: the
    # replay evidence is not trustworthy, and neither backend's verdict is
    # silently preferred over the other's.
    status3 = (
        "inconclusive"
        if _replay_evidence_is_untrustworthy(counterexample)
        else "counterexample"
    )

    return _build_sequential_report(
        engine=engine,
        engine_version=engine_version,
        sim_backend=sim_backend,
        status=status3,
        gold=gold,
        gate=gate,
        port_map=port_map or None,
        timeout_s=effective_timeout_s,
        elapsed_s=total_elapsed_s,
        induction_depth=induction_depth,
        counterexample=counterexample,
        diagnostics=refinement_diagnostics + diagnostics2,
        stage1_script_path=script1_path,
        stage1_log_path=log1_path,
        stage2_script_path=script2_path,
        stage2_log_path=log2_path,
        netlist_path=netlist_path,
        stage1_blacklist_path=active_blacklist_path,
        resume=resume_block,
    )


# --------------------------------------------------------------------------- #
# --check / --rerun: verify a previously committed report (issue #2224's
# shared machinery, wired for `klt equiv` by issue #2280 so a retrieved
# remote-run envelope can be verified against local inputs -- see
# docs/guides/remote-evidence-runs.md)
# --------------------------------------------------------------------------- #

#: Run-scoped bookkeeping keys dropped from *both* sides of a ``--rerun``
#: diff, via :func:`klayout_tools._report_verify.strip_keys` -- the same
#: mechanism ``synthesize.RERUN_BOOKKEEPING_KEYS`` uses (issue #2224).
#:
#: ``elapsed_s`` differs between *every* pair of runs, including two runs of
#: the identical request on the identical host -- wall-clock time is not a
#: property of the evidence. ``resume`` is issue #2280's own run-scoped
#: block: it records what *this* invocation reused from on-disk stage
#: artifacts, which a fresh re-run (that did not pass ``--resume``) will
#: legitimately not carry, or carry with a different ``resumed_stage``.
#: Neither key is verdict-bearing: ``status``/``counterexample``/
#: ``diagnostics`` all stay in the diff.
RERUN_BOOKKEEPING_KEYS: frozenset[str] = frozenset({"elapsed_s", "resume"})


def _canonicalize_equiv_report_for_rerun_diff(
    report: dict[str, Any],
) -> dict[str, Any]:
    """``report`` with :data:`RERUN_BOOKKEEPING_KEYS` removed at every depth
    -- the comparison view :func:`rerun_equiv_report` diffs, never what it
    embeds as ``fresh``. See
    :func:`klayout_tools._report_verify.strip_keys` for why the exclusion is
    keyed by name rather than by path."""
    return strip_keys(report, RERUN_BOOKKEEPING_KEYS)


def _equiv_input_hash(gold: dict[str, Any], gate: dict[str, Any]) -> str | None:
    """The ``provenance.input.content_hash`` a fresh run of this request
    would record -- re-derived **without invoking Yosys**, mirroring
    :func:`_build_report`/``_build_sequential_report``'s own hashing step
    for step (the same rule ``synthesize._synthesize_provenance_hashes``
    documents: any divergence here would show up as permanent, unexplained
    drift on a report that never moved, so there is exactly one recipe and
    both call sites follow it). A single source across both sides hashes
    through ``_content_hash`` (what ``build_provenance``'s ``input_path``
    records); multiple sources through the order-independent
    ``_combined_content_hash``."""
    all_sources = gold["sources"] + gate["sources"]
    if len(all_sources) == 1:
        return _content_hash(all_sources[0])
    return _combined_content_hash(all_sources)


def check_equiv_report(report_path: str, request: str) -> dict[str, Any]:
    """``klt equiv <request> --check <report>`` (cheap mode, issues #2224 +
    #2280): verify a previously committed ``klt equiv --format json``
    report at ``report_path`` still reproduces from the request at
    ``request``, without invoking Yosys at all.

    Re-resolves the request's ``gold``/``gate`` sources (the same
    :func:`_resolve_side` a real run uses, so an unreadable source is the
    same clean :class:`EquivError` a run would raise) and re-hashes them
    (:func:`_equiv_input_hash`), comparing against the committed report's
    ``provenance.input.content_hash``. Returns the shared ``--check``
    payload built by
    :func:`klayout_tools._report_verify.build_check_result` -- ``status:
    "match"`` when the hash agrees, ``"drifted"`` otherwise -- the same
    contract every other ``--check`` verb shares. A recorded hash that is
    itself ``None`` never counts as a match (see
    :func:`klayout_tools._report_verify.hash_check`'s "nothing recorded is
    never a pass" rule).

    Because the hash is content-derived and path-independent, this is the
    verification step for a **retrieved remote-run envelope** (issue
    #2280's remote round-trip): the committed report may carry the remote
    host's absolute source paths, and ``--check`` still verifies it against
    the local copies of the same sources.

    Raises :class:`EquivError` for a missing/unparseable committed report
    or an unresolvable request -- never a traceback.
    """
    committed = load_committed_report(report_path, EquivError)
    request_doc, request_dir = load_request_arg(request)
    gold = _resolve_side(request_doc.get("gold"), request_dir, "gold")
    gate = _resolve_side(request_doc.get("gate"), request_dir, "gate")
    checks = [
        hash_check(
            "provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            _equiv_input_hash(gold, gate),
        )
    ]
    return build_check_result(report_path=report_path, checks=checks)


def rerun_equiv_report(
    report_path: str,
    request: str,
    *,
    timeout_s: float | None = None,
    sim_backend: str | None = None,
) -> dict[str, Any]:
    """``klt equiv <request> --check <report> --rerun`` (full mode, issues
    #2224 + #2280): verify a committed report by actually re-running the
    proof the request declares and diffing the fresh report against the
    committed one.

    Diffs via :func:`klayout_tools._report_verify.diff_verdict_fields`,
    excluding :data:`klayout_tools._report_verify.VOLATILE_FLOW_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version`` plus
    the Yosys ``engine_version`` build string) and canonicalizing
    :data:`RERUN_BOOKKEEPING_KEYS` (``elapsed_s``, the ``resume`` block)
    out of both sides first. ``status: "drifted"`` names every other field
    that changed -- a moved ``status``, a different ``counterexample``,
    new ``diagnostics``, a changed ``provenance.input.content_hash``.

    **Known limitation, the mirror image of ``--check``'s cross-host
    strength:** the echoed ``gold``/``gate`` source paths are absolute, so
    a full-mode re-run on a *different* host (or from a moved checkout)
    legitimately drifts on those paths even when the proof result is
    identical -- cheap mode (:func:`check_equiv_report`) is the
    cross-host verification path; full mode answers "does this reproduce
    *here*". As with every ``--rerun`` verb, pass the same
    ``--timeout-s``/``--sim-backend`` the original run used (the CLI does),
    since the response does not echo them.

    Raises :class:`EquivError` for a missing/unparseable committed report
    or any error the re-run itself raises -- never a traceback.
    """
    committed = load_committed_report(report_path, EquivError)
    fresh = run_equiv(request, timeout_s=timeout_s, sim_backend=sim_backend)
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_FLOW_PATHS,
        committed_for_diff=_canonicalize_equiv_report_for_rerun_diff(committed),
        fresh_for_diff=_canonicalize_equiv_report_for_rerun_diff(fresh),
    )
