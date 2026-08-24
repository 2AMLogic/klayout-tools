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
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from ._paths import _load_request_json, validate_request_shape
from ._paths import load_request_arg as _shared_load_request_arg
from ._provenance import _combined_content_hash, _yosys_version, build_provenance

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"yosys"`` (combinational, Phase 0/1) and ``"yosys-sequential"``
#: (register-correspondence sequential, Phase 2) -- see this module's
#: docstring, "Engine" sections.
SUPPORTED_ENGINES = ("yosys", "yosys-sequential")

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

_ICARUS_VERSION_RE = re.compile(r"Icarus Verilog version (\S+)")

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
#: `  input clk;` or `  output [3:0] sum;` -- see `_parse_module_ports`.
_PORT_DECL_RE = re.compile(r"^\s*(input|output|inout)\s+(?:\[[^\]]+\]\s+)?(\S+?);\s*$")
_MODULE_START_RE = re.compile(r"^\s*module\s+(\S+)\s*\(")
_MODULE_END_RE = re.compile(r"^\s*endmodule\s*$")

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


def run_equiv(request: str, *, timeout_s: float | None = None) -> dict[str, Any]:
    """Run the ``klt equiv`` equivalence check declared by ``request`` (a
    path, ``-`` for stdin, or an inline JSON object string -- see
    :func:`load_request_arg`), via ``request.engine`` (``"yosys"``,
    combinational, or ``"yosys-sequential"``, register-correspondence
    sequential -- see this module's own docstring "Engine" sections).

    ``timeout_s`` (the CLI's ``--timeout-s``) overrides the request's own
    ``timeout_s`` field when given; the request field is used when
    ``timeout_s`` is ``None``; :data:`DEFAULT_TIMEOUT_S` is used when
    neither is given.

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
        )

    # engine == "yosys" (combinational, Phase 0/1) continues below, unchanged.
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
            engine=engine,
            engine_version=engine_version,
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
        )
        if counterexample["confirmed_by_simulation"] is False:
            # The solver reported a counterexample, but re-running its own
            # trace through the flattened netlists via iverilog/vvp did not
            # reproduce a diverging output (`counterexample_not_reproduced`,
            # appended above) -- an unsound `$equiv`/miter artifact, not a
            # demonstrated functional difference. "inconclusive" is the
            # honest verdict here, matching the downgrade the sequential
            # engine's own stage-2 path already applies for its analogous
            # "unproven, no counterexample either" case below. Never
            # reported when `confirmed_by_simulation` is `None` (simulation
            # could not be attempted at all, e.g. no `iverilog` on $PATH) --
            # that case has no evidence either way, so the solver's own
            # verdict stands.
            status = "inconclusive"

    return _build_report(
        engine=engine,
        engine_version=engine_version,
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
    """
    for stream in (stderr, stdout):
        error_lines = [line.strip() for line in stream.splitlines() if "ERROR:" in line]
        if error_lines:
            return f"yosys equivalence check failed: {error_lines[-1]}"

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
    }


def _confirm_counterexample(
    *,
    counterexample: dict[str, Any],
    netlist_path: str,
    output_dir: str,
    diagnostics: list[dict[str, str]],
) -> None:
    """Independently confirm ``counterexample`` by actually running it
    through the flattened ``gold``/``gate`` netlists Yosys wrote to
    ``netlist_path``, via ``iverilog``/``vvp`` -- never trusting the SAT
    solver's own reported values uncritically (this module's own "the
    counterexample is executable" discipline).

    Mutates ``counterexample`` in place (``confirmed_by_simulation``,
    ``simulation``); appends to ``diagnostics`` on any degradation (a
    missing ``iverilog`` binary, a compile error, or -- most importantly --
    a re-simulation that does *not* reproduce the divergence the solver
    reported, which would mean the solver's own counterexample was not
    trustworthy).
    """
    tb_path = os.path.join(output_dir, "equiv_tb.v")
    vvp_path = os.path.join(output_dir, "equiv_tb.vvp")

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

    try:
        compiled = subprocess.run(
            ["iverilog", "-g2012", "-o", vvp_path, netlist_path, tb_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog not found on $PATH -- counterexample "
                "reported by the solver only, not independently confirmed "
                "by simulation",
            }
        )
        return
    except subprocess.TimeoutExpired:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog did not complete within 30s while "
                "compiling the counterexample-confirmation testbench",
            }
        )
        return

    if compiled.returncode != 0:
        tail = (compiled.stderr or compiled.stdout).strip()[-500:]
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog failed to compile the counterexample-"
                f"confirmation testbench: {tail}",
            }
        )
        return

    try:
        ran = subprocess.run(
            ["vvp", vvp_path], capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": f"could not run confirmation testbench with vvp: {exc}",
            }
        )
        return

    sim_gold: dict[str, str] = {}
    sim_gate: dict[str, str] = {}
    for line in (ran.stdout or "").splitlines():
        match = _SIM_DISPLAY_RE.match(line.strip())
        if not match:
            continue
        side, name, bits = match.groups()
        (sim_gold if side == "gold" else sim_gate)[name] = bits

    sim_diverging = sorted(
        name
        for name in sim_gold
        if name in sim_gate and sim_gold[name] != sim_gate[name]
    )

    counterexample["simulation"] = {
        "engine": "icarus",
        "engine_version": _icarus_version(),
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
                "the flattened netlists via iverilog/vvp did not reproduce "
                "a diverging output -- treat this counterexample with "
                "suspicion",
            }
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


def _icarus_version() -> str | None:
    """``iverilog -V``'s reported version string, or ``None`` -- mirrors
    ``functional_verification.py``'s own engine-version probe."""
    try:
        completed = subprocess.run(
            ["iverilog", "-V"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or "") + (completed.stderr or "")
    match = _ICARUS_VERSION_RE.search(text)
    return match.group(1) if match else None


def _build_report(
    *,
    engine: str,
    engine_version: str | None,
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
) -> dict[str, Any]:
    all_sources = gold["sources"] + gate["sources"]
    if len(all_sources) == 1:
        provenance = build_provenance(input_path=all_sources[0])
    else:
        provenance = build_provenance()
        provenance["input"] = {"content_hash": _combined_content_hash(all_sources)}

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
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
            ports.setdefault(name, direction)
    return ports


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
    """
    input_names = sorted(
        name for name, direction in ports.items() if direction == "input"
    )
    output_names = sorted(
        name for name, direction in ports.items() if direction != "input"
    )

    lines = ["module equiv_tb;"]
    for name in input_names:
        lines.append(f"  reg {name};")
    for name in output_names:
        lines.append(f"  wire gold_{name}, gate_{name};")

    def _conns(prefix: str) -> str:
        conns = [f".{name}({name})" for name in input_names]
        conns += [f".{name}({prefix}_{name})" for name in output_names]
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
                lines.append(f"    {name} = 1'b{entry['bin']};")
            else:
                lines.append(f"    {name} = {width}'b{entry['bin']};")
        lines.append("    #1;")
        time_value = cycle["time"]
        for name in output_names:
            lines.append(
                f'    $display("EQUIV_SIM_CYCLE {time_value} gold {name} %b", '
                f"gold_{name});"
            )
            lines.append(
                f'    $display("EQUIV_SIM_CYCLE {time_value} gate {name} %b", '
                f"gate_{name});"
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
) -> None:
    """Multi-cycle counterpart of :func:`_confirm_counterexample`:
    independently re-runs ``counterexample``'s entire captured trace through
    the flattened ``gold``/``gate`` netlists via ``iverilog``/``vvp``.

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
    vvp_path = os.path.join(output_dir, "equiv_seq_tb.vvp")

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

    try:
        compiled = subprocess.run(
            ["iverilog", "-g2012", "-o", vvp_path, netlist_path, tb_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog not found on $PATH -- counterexample "
                "reported by the solver only, not independently confirmed "
                "by simulation",
            }
        )
        return
    except subprocess.TimeoutExpired:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog did not complete within 30s while "
                "compiling the counterexample-confirmation testbench",
            }
        )
        return

    if compiled.returncode != 0:
        tail = (compiled.stderr or compiled.stdout).strip()[-500:]
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog failed to compile the counterexample-"
                f"confirmation testbench: {tail}",
            }
        )
        return

    try:
        ran = subprocess.run(
            ["vvp", vvp_path], capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": f"could not run confirmation testbench with vvp: {exc}",
            }
        )
        return

    sim_cycles: dict[int, dict[str, dict[str, str]]] = {}
    for line in (ran.stdout or "").splitlines():
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
        diverging = sorted(
            name
            for name in gold_outputs
            if name in gate_outputs and gold_outputs[name] != gate_outputs[name]
        )
        sim_diverging_union.update(diverging)
        sim_cycle_entries.append(
            {
                "time": time_value,
                "gold_outputs": gold_outputs,
                "gate_outputs": gate_outputs,
                "diverging_outputs": diverging,
            }
        )

    counterexample["simulation"] = {
        "engine": "icarus",
        "engine_version": _icarus_version(),
        "cycles": sim_cycle_entries,
        "diverging_outputs": sorted(sim_diverging_union),
    }

    confirmed = bool(sim_diverging_union & set(counterexample["diverging_outputs"]))
    counterexample["confirmed_by_simulation"] = confirmed
    if not confirmed:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "counterexample_not_reproduced",
                "message": "re-running the solver's counterexample trace "
                "through the flattened netlists via iverilog/vvp did not "
                "reproduce a diverging output on any replayed cycle -- "
                "treat this counterexample with suspicion",
            }
        )


def _build_sequential_report(
    *,
    engine: str,
    engine_version: str | None,
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
    """
    all_sources = gold["sources"] + gate["sources"]
    if len(all_sources) == 1:
        provenance = build_provenance(input_path=all_sources[0])
    else:
        provenance = build_provenance()
        provenance["input"] = {"content_hash": _combined_content_hash(all_sources)}

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
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


def _run_sequential(
    *,
    gold: dict[str, Any],
    gate: dict[str, Any],
    port_map: dict[str, str],
    output_dir: str,
    effective_timeout_s: float,
    induction_depth: int,
    engine: str,
) -> dict[str, Any]:
    """The ``"yosys-sequential"`` engine's own top-level driver, called from
    ``run_equiv`` once ``gold``/``gate``/``port_map``/``output_dir`` are
    already resolved -- see this module's docstring, "Engine:
    yosys-sequential" section, for the two-stage rationale."""
    script1_path = os.path.join(output_dir, "equiv_seq_stage1.ys")
    netlist_path = os.path.join(output_dir, "equiv_seq_netlist.v")
    log1_path = os.path.join(output_dir, "equiv_seq_stage1.log")
    blacklist_path = os.path.join(output_dir, "equiv_seq_blacklist.txt")

    int_timeout = max(1, round(effective_timeout_s))
    engine_version = _yosys_version()

    # Stage 1, run as a bounded cut-point refinement loop: each pass drops
    # the wrongly-paired *internal* wires the previous pass could not prove
    # (never a top-level port) from `equiv_make`'s matching, so their false
    # cut points stop poisoning every downstream proof. See this module's
    # docstring, "Stage 1 cut-point refinement" (issue #1353).
    blacklisted: set[str] = set()
    refinements = 0
    total_elapsed_s = 0.0
    active_blacklist_path: str | None = None

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

        try:
            ports = _parse_module_ports(netlist_path, "gold")
        except EquivError:
            break
        candidates = {
            name
            for name in _parse_unproven_equiv_signals(stage1.stdout)
            if name not in ports
        }
        if candidates <= blacklisted:
            # Nothing new to drop -- either every unproven obligation is a
            # top-level port (a real output difference, stage 2's job) or
            # refinement has reached its fixpoint.
            break
        blacklisted |= candidates
        _write_equiv_blacklist(blacklist_path, blacklisted)
        active_blacklist_path = blacklist_path
        refinements += 1

    refinement_diagnostics: list[dict[str, str]] = []
    if blacklisted:
        refinement_diagnostics.append(
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
        )

    if stage1.timed_out:
        return _build_sequential_report(
            engine=engine,
            engine_version=engine_version,
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
        )

    if stage1.returncode != 0:
        # No `select -assert-none` sequential-cell guard exists in this
        # engine's own script (unlike the combinational engine's -- this
        # engine exists specifically *for* sequential designs), so a
        # nonzero return here is always a genuine elaboration/build error,
        # never the combinational engine's own scope-rejection shape.
        message = _yosys_error_message(stage1.stdout, stage1.stderr, stage1.returncode)
        raise EquivError(message)

    if _EQUIV_NONE_FOUND_RE.search(stage1.stdout):
        raise EquivError(
            "yosys-sequential engine found no matching gold/gate signals to "
            "compare (equiv_make only matches identically-named wires) -- "
            "check that gold/gate share port and register names, or supply "
            "request.port_map"
        )

    if _EQUIV_ALL_PROVEN_RE.search(stage1.stdout):
        return _build_sequential_report(
            engine=engine,
            engine_version=engine_version,
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
        )

    # Stage 1 left one or more $equiv cells unproven -- register-
    # correspondence induction alone could not decide. Stage 2 attempts a
    # genuinely bounded (complete-within-its-own-depth) SAT search for an
    # actual demonstrated counterexample; see module docstring.
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
        )

    cycles = _parse_multicycle_signal_table(stage2.stdout)
    counterexample = _build_sequential_counterexample(cycles, ports)
    _confirm_sequential_counterexample(
        counterexample=counterexample,
        ports=ports,
        netlist_path=netlist_path,
        output_dir=output_dir,
        diagnostics=diagnostics2,
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
    status3 = (
        "inconclusive"
        if counterexample["confirmed_by_simulation"] is False
        else "counterexample"
    )

    return _build_sequential_report(
        engine=engine,
        engine_version=engine_version,
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
    )
