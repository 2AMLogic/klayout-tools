"""Counterexample replay and cross-check helpers of :mod:`klayout_tools.equiv`.

The replay backends (``iverilog``/``vvp``, ``verilator``) that re-simulate a
solver counterexample against both sides of a design, the cross-backend
agreement bookkeeping, and the replay-backend binary resolution. See
:mod:`klayout_tools.equiv`'s docstring, "Replay backend", for the contract.
Shared by the combinational path (``equiv.py``) and the ``"yosys-sequential"``
engine (``equiv_sequential.py``); split out of ``equiv.py`` (#4032) with
behaviour and JSON output unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from ._paths import resolve_tool_binary
from .equiv_errors import EquivError

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
SIM_ENGINE_LABEL = {"iverilog": "icarus", "verilator": "verilator"}

#: Human-readable tool label used in this module's own diagnostics.
SIM_BACKEND_LABEL = {"iverilog": "iverilog/vvp", "verilator": "verilator"}

#: Whether a backend models 4-state (``0``/``1``/``x``/``z``) values.
#: Verilator is 2-state: an undriven register reads ``0`` where Icarus
#: reads ``x``. Declared per-run in ``simulation.four_state`` so a
#: cross-backend bit difference can be *explained* rather than silently
#: tolerated -- see :func:`compare_replay_outputs`.
SIM_BACKEND_FOUR_STATE = {"iverilog": True, "verilator": False}

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

#: Replay-binary env overrides / PATH names (issue #2423), resolved like
#: ``KLT_YOSYS_BINARY`` in ``equiv.py``.
_IVERILOG_BINARY_ENV_VAR = "KLT_IVERILOG_BINARY"
_VVP_BINARY_ENV_VAR = "KLT_VVP_BINARY"

_IVERILOG_BINARY_NAMES: tuple[str, ...] = ("iverilog",)
_VVP_BINARY_NAMES: tuple[str, ...] = ("vvp",)

_SIM_DISPLAY_RE = re.compile(r"^EQUIV_SIM (gold|gate) (\S+) ([01xz]+)$")


def resolve_sim_backend(sim_backend: str | None, request_doc: dict[str, Any]) -> str:
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


def replay_backends(sim_backend: str) -> tuple[str, str | None]:
    """``(canonical, cross_check)`` replay backends for ``sim_backend``.

    ``"both"`` pairs the canonical ``iverilog`` replay with a ``verilator``
    cross-check; every other value runs that one backend alone (no
    cross-check, and -- importantly -- no silent fallback to the other).
    """
    if sim_backend == "both":
        return (CANONICAL_SIM_BACKEND, CROSS_CHECK_SIM_BACKEND)
    return (sim_backend, None)


def resolve_iverilog_binary(
    request_doc: dict[str, Any], request_dir: str
) -> str | None:
    """Resolve the ``iverilog`` binary for the counterexample-replay path
    (issue #2423), via ``request.iverilog_binary`` -> ``$KLT_IVERILOG_BINARY``
    -> ``iverilog`` on ``PATH``. ``required=False``: unlike
    :func:`_resolve_yosys_binary`, a bare ``iverilog`` genuinely absent from
    ``PATH`` (no override given) is not an application error here -- it is
    ``run_replay_backend``'s own long-standing "simulator not installed"
    degrade to a diagnostic, which this function must not disturb. An
    *explicitly-named* override that is not runnable still raises
    (never a silent fallback), exactly like every other tool this issue
    covers."""
    return resolve_tool_binary(
        "iverilog",
        request_doc,
        option_key="iverilog_binary",
        option_label="request.iverilog_binary",
        env_var=_IVERILOG_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=_IVERILOG_BINARY_NAMES,
        error_cls=EquivError,
        install_hint="Install Icarus Verilog (https://steveicarus.github.io/iverilog/).",
        required=False,
    )


def resolve_vvp_binary(request_doc: dict[str, Any], request_dir: str) -> str | None:
    """Resolve the ``vvp`` binary for the counterexample-replay path (issue
    #2423) -- the ``vvp`` counterpart of :func:`resolve_iverilog_binary`;
    see that function's docstring for the shared ``required=False``
    rationale."""
    return resolve_tool_binary(
        "vvp",
        request_doc,
        option_key="vvp_binary",
        option_label="request.vvp_binary",
        env_var=_VVP_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=_VVP_BINARY_NAMES,
        error_cls=EquivError,
        install_hint="Install Icarus Verilog (https://steveicarus.github.io/iverilog/).",
        required=False,
    )


def run_replay_backend(
    *,
    backend: str,
    netlist_path: str,
    tb_path: str,
    output_dir: str,
    stem: str,
    request_doc: dict[str, Any],
    request_dir: str,
) -> tuple[str | None, str | None, dict[str, str | None]]:
    """Compile and run the generated replay testbench ``tb_path`` against
    the flattened ``netlist_path`` under ``backend``.

    Returns ``(stdout, failure_message, binaries)``: ``(stdout, None, ...)``
    on a completed run, or ``(None, message, ...)`` naming exactly why the
    replay could not be completed (missing binary, compile timeout, compile
    error, run failure). ``binaries`` is ``{"iverilog_binary": ...,
    "vvp_binary": ...}`` for the ``iverilog`` backend (``None`` for whichever
    of the two never got resolved/attempted), or ``{}`` for ``verilator``
    (issue #2423 adds no override for that backend -- see this module's own
    docstring, "Replay backend").

    Never raises for a plain "not installed" absence -- an unavailable
    backend is reported as unavailable, never fabricated, per this repo's
    own convention -- **except** when ``request.iverilog_binary``/
    ``request.vvp_binary`` (or their ``$KLT_*_BINARY`` env var counterparts)
    explicitly names a binary that turns out not to be runnable: that one
    case *does* raise :class:`EquivError` (issue #2423, mirrors
    ``lvs_netgen._resolve_netgen_binary``'s "never a silent fallback" rule)
    rather than silently degrading to the same diagnostic a simple absence
    produces -- an override the caller believes is in force but is not
    would otherwise go unnoticed.
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
        binaries: dict[str, str | None] = {}
    else:
        iverilog_binary = resolve_iverilog_binary(request_doc, request_dir)
        vvp_binary = resolve_vvp_binary(request_doc, request_dir)
        binaries = {"iverilog_binary": iverilog_binary, "vvp_binary": vvp_binary}
        if iverilog_binary is None:
            return (
                None,
                "iverilog not found on $PATH -- counterexample reported by "
                "the solver only, not independently confirmed by simulation",
                binaries,
            )
        vvp_path = os.path.join(output_dir, f"{stem}.vvp")
        compile_cmd = [iverilog_binary, "-g2012", "-o", vvp_path, netlist_path, tb_path]
        compile_tool = "iverilog"
        compile_timeout_s = _IVERILOG_COMPILE_TIMEOUT_S
        if vvp_binary is None:
            return (
                None,
                "vvp not found on $PATH -- counterexample reported by the "
                "solver only, not independently confirmed by simulation",
                binaries,
            )
        run_cmd = [vvp_binary, vvp_path]
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
            binaries,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"{compile_tool} did not complete within {compile_timeout_s}s "
            "while compiling the counterexample-confirmation testbench",
            binaries,
        )

    if compiled.returncode != 0:
        tail = (compiled.stderr or compiled.stdout).strip()[-500:]
        return (
            None,
            f"{compile_tool} failed to compile the counterexample-"
            f"confirmation testbench: {tail}",
            binaries,
        )

    try:
        ran = subprocess.run(
            run_cmd, capture_output=True, text=True, timeout=_REPLAY_RUN_TIMEOUT_S
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return (
            None,
            f"could not run confirmation testbench with {run_tool}: {exc}",
            binaries,
        )

    return (ran.stdout or "", None, binaries)


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


def diverging_names(gold: dict[str, str], gate: dict[str, str]) -> list[str]:
    """Output names present on both sides whose replayed bits differ."""
    return sorted(name for name in gold if name in gate and gold[name] != gate[name])


def explains_two_state(canonical: str | None, cross: str | None) -> bool:
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


def compare_replay_outputs(
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
                and explains_two_state(canonical_bits, cross_bits)
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


def cross_check_block(backend: str, *, sequential: bool) -> dict[str, Any]:
    """A ``simulation_cross_check`` block pre-filled for a cross-check that
    has not run (yet, or at all): every result field ``None``,
    ``agreement: "unavailable"``.

    A cross-check backend that could not be run is reported as absent, never
    fabricated and never silently replaced by the canonical backend's own
    numbers -- the caller fills the result fields in only when the run
    actually completed.
    """
    block: dict[str, Any] = {
        "engine": SIM_ENGINE_LABEL[backend],
        "engine_version": sim_backend_version(backend),
        "four_state": SIM_BACKEND_FOUR_STATE[backend],
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


def record_cross_check_agreement(
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


def replay_backends_disagreed(counterexample: dict[str, Any]) -> bool:
    """Whether a ``sim_backend: "both"`` run's two replay backends
    disagreed -- the caller's signal to downgrade ``status``."""
    cross_check = counterexample.get("simulation_cross_check")
    return bool(cross_check is not None and cross_check.get("agreement") == "disagree")


def replay_evidence_is_untrustworthy(counterexample: dict[str, Any]) -> bool:
    """Whether the replay evidence for ``counterexample`` forbids reporting
    ``status: "counterexample"``: either the replay ran and did *not*
    reproduce the divergence, or the two replay backends disagreed.

    ``confirmed_by_simulation is None`` *because the replay could not be
    attempted at all* is deliberately not included -- that case has no
    evidence either way, so the solver's own verdict stands (a disagreement
    also sets the field to ``None``, and is caught by the second clause)."""
    return counterexample[
        "confirmed_by_simulation"
    ] is False or replay_backends_disagreed(counterexample)


def confirm_counterexample(
    *,
    counterexample: dict[str, Any],
    netlist_path: str,
    output_dir: str,
    diagnostics: list[dict[str, str]],
    sim_backend: str = DEFAULT_SIM_BACKEND,
    request_doc: dict[str, Any],
    request_dir: str,
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

    ``request_doc``/``request_dir`` (issue #2423) are threaded down to
    :func:`run_replay_backend` for the ``iverilog``/``vvp`` binary
    resolution -- see that function's docstring.
    """
    tb_path = os.path.join(output_dir, "equiv_tb.v")

    tb_source = build_testbench(counterexample)
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

    canonical_backend, cross_backend = replay_backends(sim_backend)

    stdout, failure, canonical_binaries = run_replay_backend(
        backend=canonical_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_tb",
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

    sim_gold, sim_gate = _parse_replay_outputs(stdout)
    sim_diverging = diverging_names(sim_gold, sim_gate)

    counterexample["simulation"] = {
        "engine": SIM_ENGINE_LABEL[canonical_backend],
        "engine_version": sim_backend_version(
            canonical_backend, canonical_binaries.get("iverilog_binary")
        ),
        "four_state": SIM_BACKEND_FOUR_STATE[canonical_backend],
        "gold_outputs": sim_gold,
        "gate_outputs": sim_gate,
        "diverging_outputs": sim_diverging,
        # Issue #2423: alongside `engine_version` above -- the resolved
        # absolute paths `run_replay_backend` actually invoked, so a
        # committed report distinguishes which `iverilog`/`vvp` build
        # produced this confirmation (or an explicitly named one). `{}`
        # (both keys absent) for the `verilator` canonical backend, which
        # this issue's scope adds no override for.
        **canonical_binaries,
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
                f"{SIM_BACKEND_LABEL[canonical_backend]} did not reproduce "
                "a diverging output -- treat this counterexample with "
                "suspicion",
            }
        )

    if cross_backend is None:
        return

    cross_stdout, cross_failure, _cross_binaries = run_replay_backend(
        backend=cross_backend,
        netlist_path=netlist_path,
        tb_path=tb_path,
        output_dir=output_dir,
        stem="equiv_tb",
        request_doc=request_doc,
        request_dir=request_dir,
    )
    if cross_stdout is None:
        counterexample["simulation_cross_check"] = cross_check_block(
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
    cross_check = cross_check_block(cross_backend, sequential=False)
    cross_check["gold_outputs"] = cross_gold
    cross_check["gate_outputs"] = cross_gate
    cross_check["diverging_outputs"] = diverging_names(cross_gold, cross_gate)

    record_cross_check_agreement(
        counterexample=counterexample,
        cross_check=cross_check,
        canonical_backend=canonical_backend,
        cross_backend=cross_backend,
        canonical_confirmed=confirmed,
        cross_confirmed=bool(cross_check["diverging_outputs"]),
        mismatches=compare_replay_outputs(
            canonical={"gold": sim_gold, "gate": sim_gate},
            cross={"gold": cross_gold, "gate": cross_gate},
            cross_four_state=SIM_BACKEND_FOUR_STATE[cross_backend],
        ),
        diagnostics=diagnostics,
    )


def build_testbench(counterexample: dict[str, Any]) -> str:
    """Generate a minimal Verilog testbench that drives ``gold``/``gate``
    (as written by ``write_verilog`` to ``netlist_path``) with the exact
    concrete counterexample input vector and ``$display``s both modules'
    output vectors, so :func:`confirm_counterexample` can compare them
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


def sim_backend_version(backend: str, binary: str | None = None) -> str | None:
    """The replay ``backend``'s own reported version string, or ``None``
    when it is not installed / does not report one -- the per-backend
    generalisation of the old ``iverilog -V``-only probe, mirroring
    ``functional_verification.py``'s ``_ENGINE_VERSION_COMMANDS`` table.

    ``binary`` (issue #2423), when given, replaces the version command's own
    bare tool name with the resolved absolute path the caller actually
    invoked for compile/run -- so a ``request.iverilog_binary``/
    ``$KLT_IVERILOG_BINARY`` override never reports the bare-PATH
    ``iverilog``'s version (or ``None``) for a replay that used a different
    build. Left ``None`` (the default) for the ``verilator`` backend, which
    this issue's scope does not add an override for.
    """
    command, pattern = _SIM_BACKEND_VERSION_COMMANDS[backend]
    if binary is not None:
        command = [binary, *command[1:]]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or "") + (completed.stderr or "")
    match = pattern.search(text)
    return match.group(1) if match else None
