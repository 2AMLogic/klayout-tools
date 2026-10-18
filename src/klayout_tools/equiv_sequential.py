"""The ``"yosys-sequential"`` engine of :mod:`klayout_tools.equiv` (Phase 2, #1313).

Register-correspondence sequential equivalence via ``equiv_make`` /
``equiv_simple`` / ``equiv_induct`` / ``equiv_status``, with a bounded-SAT
fallback (stage 2) to extract a confirmable counterexample when stage 1 leaves
cells unproven. See :mod:`klayout_tools.equiv`'s own docstring, "Engine:
yosys-sequential", for the full rationale. Split out of ``equiv.py`` (#3694);
behaviour and JSON output are unchanged. Helpers shared with the combinational
path stay in ``equiv.py`` and are imported lazily to avoid an import cycle.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from ._provenance import (
    INPUT_ROLE_SOURCE,
    _combined_content_hash,
    _yosys_version,
    build_provenance,
)
from .equiv import (
    _EQUIV_ALL_PROVEN_RE,
    _SIM_BACKEND_FOUR_STATE,
    _SIM_BACKEND_LABEL,
    _SIM_ENGINE_LABEL,
    _STAGE1_ALL_PROVEN,
    _STAGE1_UNPROVEN_CELLS,
    _UNPROVEN_EQUIV_RE,
    DEFAULT_SIM_BACKEND,
    SCHEMA_VERSION,
    EquivError,
    _classify_sat_result,
    _compare_replay_outputs,
    _cross_check_block,
    _decode_bin,
    _diverging_names,
    _load_committed_stage1,
    _record_cross_check_agreement,
    _replay_backends,
    _replay_evidence_is_untrustworthy,
    _run_replay_backend,
    _side_prep_lines,
    _sim_backend_version,
    _stage_fingerprint,
    _stage_record_path,
    _write_partial_stage_record,
    _write_stage_record,
    _yosys_error_message,
)

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

#: How many times stage 1 may re-run `equiv_make` with a widened
#: `-blacklist` before giving up and falling through to stage 2 (issue
#: #1353's cut-point refinement loop -- see this module's docstring). The
#: real post-route netlists this engine targets converge in a single
#: refinement pass; the cap exists so a pathological design cannot spin, and
#: every pass shares the one `timeout_s` budget regardless.
_MAX_STAGE1_REFINEMENTS = 3


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


def _run_yosys_subprocess(
    script_path: str, timeout_s: float, binary: str = "yosys"
) -> _YosysRunResult:
    """Run ``<binary> -s <script_path>``, bounded by ``timeout_s`` -- the
    same subprocess-invocation shape ``run_equiv``'s own combinational path
    uses inline, factored out so the sequential engine's two stages
    (:func:`_run_sequential`) can share it.

    ``binary`` (issue #2423) is the resolved absolute path
    ``_resolve_yosys_binary`` settled on for this run; both of
    :func:`_run_sequential`'s call sites always pass it explicitly -- the
    ``"yosys"`` default exists only so a direct test of this function need
    not also stub binary resolution.
    """
    started = time.monotonic()
    timed_out = False
    stdout = ""
    stderr = ""
    returncode: int | None = None
    try:
        completed = subprocess.run(
            [binary, "-s", script_path],
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
    request_doc: dict[str, Any],
    request_dir: str,
) -> None:
    """Multi-cycle counterpart of :func:`_confirm_counterexample`:
    independently re-runs ``counterexample``'s entire captured trace through
    the flattened ``gold``/``gate`` netlists via the selected replay backend
    (``iverilog``/``vvp`` by default -- see this module's docstring,
    "Replay backend").

    Mutates ``counterexample`` in place (``confirmed_by_simulation``,
    ``simulation``); appends to ``diagnostics`` on any degradation, exactly
    as :func:`_confirm_counterexample` does. ``request_doc``/``request_dir``
    (issue #2423) are threaded down to :func:`_run_replay_backend` the same
    way.

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

    stdout, failure, canonical_binaries = _run_replay_backend(
        backend=canonical_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_seq_tb",
        request_doc=request_doc,
        request_dir=request_dir,
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
        "engine_version": _sim_backend_version(
            canonical_backend, canonical_binaries.get("iverilog_binary")
        ),
        "four_state": _SIM_BACKEND_FOUR_STATE[canonical_backend],
        "cycles": sim_cycle_entries,
        "diverging_outputs": sorted(sim_diverging_union),
        # Issue #2423: see `_confirm_counterexample`'s identical comment.
        **canonical_binaries,
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

    cross_stdout, cross_failure, _cross_binaries = _run_replay_backend(
        backend=cross_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_seq_tb",
        request_doc=request_doc,
        request_dir=request_dir,
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
    yosys_binary: str | None,
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
        # Issue #2423: same convention as `_build_report`'s own
        # `yosys_binary` -- see that field's comment.
        "yosys_binary": yosys_binary,
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
    yosys_binary: str | None,
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
            yosys_binary=yosys_binary,
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
    yosys_binary: str,
    request_doc: dict[str, Any],
    request_dir: str,
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
    engine_version = _yosys_version(yosys_binary)
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
            yosys_binary=yosys_binary,
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
                script1_path, effective_timeout_s - total_elapsed_s, yosys_binary
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
                yosys_binary=yosys_binary,
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
                yosys_binary=yosys_binary,
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

    stage2 = _run_yosys_subprocess(script2_path, effective_timeout_s, yosys_binary)
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
            yosys_binary=yosys_binary,
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
            yosys_binary=yosys_binary,
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
        request_doc=request_doc,
        request_dir=request_dir,
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
        yosys_binary=yosys_binary,
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
