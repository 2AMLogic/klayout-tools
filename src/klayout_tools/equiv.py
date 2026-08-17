"""Prove or refute combinational equivalence between two RTL/gate-level
netlists via Yosys, headless.

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
   (``functional_verification.py``) -- reused here, not SPICE.
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

#: ``"yosys"`` is the only engine implemented today -- see this module's
#: docstring, "Engine" section.
SUPPORTED_ENGINES = ("yosys",)

#: Default per-run wall-clock timeout, overridable via ``request.timeout_s``
#: or the CLI's ``--timeout-s``. Generous enough for a real corpus design's
#: proof, tight enough that an accidentally-hard SAT instance doesn't hang
#: a CI job indefinitely.
DEFAULT_TIMEOUT_S = 60.0

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
    """Run the ``klt equiv`` combinational-equivalence check declared by
    ``request`` (a path, ``-`` for stdin, or an inline JSON object string --
    see :func:`load_request_arg`).

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
        if side.get("liberty"):
            # No `-lib`: turns each cell's liberty `function` string into
            # real logic (a plain module `flatten` can inline), not an
            # opaque blackbox -- see this function's docstring and
            # `_resolve_side`'s `liberty` docs. `-ignore_miss_func` skips
            # any cell lacking a `function` spec (e.g. a pure sequential
            # cell) rather than erroring the whole liberty read; such a
            # cell surfaces instead as a missing-submodule `hierarchy`
            # error if the netlist actually instantiates it.
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
        if label == "gate" and port_map:
            lines.append(f"select -module {side['top']}")
            for gate_port, gold_port in port_map.items():
                lines.append(f"rename {gate_port} {gold_port}")
            lines.append("select -clear")
        lines.append(f"design -stash {label}_design")

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
