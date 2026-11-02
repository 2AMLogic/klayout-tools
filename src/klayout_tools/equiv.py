"""Prove or refute equivalence between two RTL/gate-level netlists via
Yosys, headless.

Pure library: :func:`run_equiv` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``lvs.py``/
``sim.py``/``synthesize.py``. Serialisation and human-readable formatting
live in the CLI command module (``cli/equiv_cmd.py``). The full behaviour
spec (request/response schema, engines, exit codes) is
``docs/cli/equiv.md``.

Public entry points: :func:`run_equiv`, :func:`load_request` /
:func:`load_request_arg`, :func:`check_equiv_report` (``--check``),
:func:`rerun_equiv_report` (``--rerun``) and
:func:`~klayout_tools.equiv_stage.load_committed_stage1` (``--resume``).

Engines (``request.engine``):

- ``"yosys"`` (default): combinational only, via Yosys's ``miter -equiv`` /
  ``sat -prove-asserts`` flow. A design with registers, latches or memories
  is rejected up front (see :func:`_sequential_error_message`).
- ``"yosys-sequential"``: register-correspondence sequential equivalence
  via Yosys's ``equiv_make``/``equiv_induct`` family, with a bounded
  cut-point refinement loop (``equiv_sequential._MAX_STAGE1_REFINEMENTS``) and a bounded
  SAT search as stage 2. Not SymbiYosys: ``sby`` has no equivalence mode.

Invariants that live only in code, and must be preserved:

- A solver timeout is never ``"equivalent"``: it reports ``"inconclusive"``,
  distinct from a proven ``"counterexample"``.
- A solver counterexample is replayed independently through ``iverilog``/
  ``vvp`` (or Verilator, per ``request.sim_backend``). If the replay does
  not reproduce the divergence (``confirmed_by_simulation: False``) the
  status is downgraded to ``"inconclusive"``; ``None`` (no simulator
  available) leaves the solver's verdict standing. With
  ``sim_backend="both"`` a disagreement between the two backends also
  downgrades to ``"inconclusive"``.
- Cut-point refinement only removes assumptions and never blacklists a
  top-level port, so a genuinely different design cannot be laundered into
  ``"equivalent"``. That depends on ``equiv_sequential._parse_module_ports``
  seeing every port, including Verilog escaped identifiers (``\\q.x ;``);
  ``equiv_sequential._PORT_DECL_RE`` matches both spellings.
- Stage 2 uses ``-set-init-zero`` to avoid false positives from an
  unconstrained initial register state, and a bounded search that finds
  nothing is ``"inconclusive"``, never ``"equivalent"``.
- ``--resume`` skips stage 1 only for a fully corroborated stage record
  (see ``equiv_stage.load_committed_stage1``); a discarded record emits a
  ``resume_stage_record_discarded`` warning. An envelope is never partial.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from ._paths import _load_request_json, resolve_tool_binary, validate_request_shape
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
from ._verilog_params import (
    chparam_lines,
    read_verilog_line,
    validate_defines,
    validate_parameters,
)
from .equiv_errors import EquivError
from .equiv_replay import (  # noqa: F401 -- sim-backend constants stay public here
    CANONICAL_SIM_BACKEND,
    CROSS_CHECK_SIM_BACKEND,
    DEFAULT_SIM_BACKEND,
    SUPPORTED_SIM_BACKENDS,
    confirm_counterexample,
    replay_evidence_is_untrustworthy,
    resolve_sim_backend,
)
from .equiv_stage import stage_record_path

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

#: Environment-variable overrides for this module's three wrapped engine
#: binaries (issue #2423, the ``equiv.py``/``sim.py`` port of
#: ``lvs_netgen.py``'s ``$KLT_NETGEN_BINARY``, issue #2373) -- checked after
#: the matching ``request.<name>_binary`` field and before the bare name on
#: ``PATH``. ``request.<name>_binary`` rather than an ``options.<name>_binary``
#: sub-object: unlike ``klt lvs``'s ``options`` block, every other
#: ``klt equiv`` request field (``engine``, ``timeout_s``, ``sim_backend``,
#: ``induction_depth``) already sits directly on the request document, so
#: these three match that existing convention instead of introducing a new
#: nesting shape this module has never had.
_YOSYS_BINARY_ENV_VAR = "KLT_YOSYS_BINARY"

#: Unlike ``netgen``/``netgen-lvs`` (issue #2373), there is no known
#: distro-name collision for any of these three -- a single bare name each.
_YOSYS_BINARY_NAMES: tuple[str, ...] = ("yosys",)


def _resolve_yosys_binary(request_doc: dict[str, Any], request_dir: str) -> str:
    """Resolve the ``yosys`` binary to invoke for this run (issue #2423),
    via ``request.yosys_binary`` -> ``$KLT_YOSYS_BINARY`` -> ``yosys`` on
    ``PATH`` (:func:`klayout_tools._paths.resolve_tool_binary`).

    Concretely fixes the friction issue #2423 names: on a host whose
    ``yosys`` on ``PATH`` is a WASI-sandboxed build (e.g. ``yowasp-yosys``,
    issue #1755/#1368) that cannot read this module's own generated
    ``.ys`` script off disk, a caller can now point at a native build
    elsewhere instead of having to reshape ``$PATH``.
    """
    return resolve_tool_binary(
        "yosys",
        request_doc,
        option_key="yosys_binary",
        option_label="request.yosys_binary",
        env_var=_YOSYS_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=_YOSYS_BINARY_NAMES,
        error_cls=EquivError,
        install_hint="Install yosys (https://github.com/YosysHQ/yosys).",
    )


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

_REQUIRED_REQUEST_FIELDS = ("gold", "gate")


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
            stage_record_path(output_dir, 1) if engine == "yosys-sequential" else None
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

    # Issue #2423: resolve *which* yosys binary to run before launching it
    # (`request.yosys_binary` > `$KLT_YOSYS_BINARY` > `yosys` on PATH),
    # rather than hardcoding the name `yosys` -- both engines (combinational
    # and "yosys-sequential") always invoke Yosys, so this is resolved once
    # up front. The resolved path is what gets spawned, what `_yosys_version`
    # below re-derives its `engine_version` from, and what the returned
    # report's own `yosys_binary` field (alongside `engine_version`) records.
    yosys_binary = _resolve_yosys_binary(request_doc, request_dir)

    effective_sim_backend = resolve_sim_backend(sim_backend, request_doc)

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
        # Deferred: equiv_sequential imports shared helpers from this module (#3694).
        from .equiv_sequential import _run_sequential

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
            yosys_binary=yosys_binary,
            request_doc=request_doc,
            request_dir=request_dir,
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
            [yosys_binary, "-s", script_path],
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

    # Issue #2423: re-derive `engine_version` from the *same* binary that
    # was actually invoked above, so a `request.yosys_binary`/
    # `$KLT_YOSYS_BINARY` override never reports the bare-PATH `yosys`'s
    # version (or `None`, if no bare `yosys` is even on PATH) for a run
    # that used a different build entirely.
    engine_version = _yosys_version(yosys_binary)

    if timed_out:
        return _build_report(
            resume=resume_block,
            engine=engine,
            engine_version=engine_version,
            yosys_binary=yosys_binary,
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
        confirm_counterexample(
            counterexample=counterexample,
            netlist_path=netlist_path,
            output_dir=output_dir,
            diagnostics=diagnostics,
            sim_backend=effective_sim_backend,
            request_doc=request_doc,
            request_dir=request_dir,
        )
        if replay_evidence_is_untrustworthy(counterexample):
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
        yosys_binary=yosys_binary,
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
    not an object, missing/empty ``sources``/``top``, a malformed optional
    ``parameters`` override (Verilog top-level ``chparam``, issue #3466), or an
    unreadable source/liberty file.

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

    parameters = validate_parameters(
        spec.get("parameters"), EquivError, f"request.{label}.parameters"
    )
    defines = validate_defines(
        spec.get("defines"), EquivError, f"request.{label}.defines"
    )
    return {
        "top": top,
        "sources": resolved,
        "liberty": resolved_liberty,
        "parameters": parameters,
        "defines": defines,
    }


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
        lines.append(
            read_verilog_line(path, side.get("defines") or {}, side.get("parameters"))
        )
    # Issue #3466: optional top-level parameter overrides for this side.
    lines += chparam_lines(side.get("parameters") or {}, side["top"])
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
    counterexample-confirmation testbench, see :func:`confirm_counterexample`);
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
    sandbox does not preopen that path -- see issue #1368/#1755; for a path under
    the host temp dir the hint instead names the sandbox's private ``/tmp``
    mount and leads with relocating the request (issue #2453). A script
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


def _build_report(
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
        # Issue #2423: the absolute path `_resolve_yosys_binary` settled on
        # for this run -- alongside `engine_version`, so a committed report
        # distinguishes a native yosys build from a WASI-sandboxed one (or
        # from an explicitly named build) the same way `klt lvs`'s
        # `environment.netgen_binary` (issue #2373) does for netgen. Always
        # present (both engines always invoke Yosys), never null.
        "yosys_binary": yosys_binary,
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

#: `klt equiv`'s own ``--rerun`` exclusion set: everything
#: :data:`klayout_tools._report_verify.VOLATILE_FLOW_PATHS` already covers,
#: plus the three host-local absolute binary paths issue #2423 added
#: (``yosys_binary`` and the counterexample-replay
#: ``counterexample.simulation.iverilog_binary``/``vvp_binary``) -- the same
#: "resolved against *this* host's PATH, not a property of the evidence"
#: reasoning ``lvs.py``'s own ``_LVS_RERUN_EXCLUDE_PATHS`` documents for
#: ``environment.netgen_binary`` (issue #2373): a second host resolving the
#: identical tool at a different path (or under an explicitly named build)
#: is not a change in what was proved/replayed. Deliberately module-local
#: rather than folded into the shared ``VOLATILE_FLOW_PATHS`` constant --
#: `klt synthesize`/`klt place-and-route` (that constant's other two
#: callers) have no analogous binary-path field to exclude, so widening it
#: would be correct here but dead configuration there (mirrors
#: `_LVS_RERUN_EXCLUDE_PATHS`'s own module-local-vs-shared reasoning).
#: A report committed before #2423 carries none of these three keys at all;
#: excluding them (rather than requiring presence) means such a report
#: re-runs clean instead of spuriously drifting on a field it never had --
#: the same "a field a report never carried cannot itself have drifted"
#: rule `_LVS_RERUN_EXCLUDE_PATHS`'s own docstring states.
_EQUIV_RERUN_EXCLUDE_PATHS: frozenset[tuple[str, ...]] = VOLATILE_FLOW_PATHS | {
    ("yosys_binary",),
    ("counterexample", "simulation", "iverilog_binary"),
    ("counterexample", "simulation", "vvp_binary"),
}


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
    excluding :data:`_EQUIV_RERUN_EXCLUDE_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version``, the
    Yosys ``engine_version`` build string, and -- issue #2423 -- the
    host-local ``yosys_binary``/``counterexample.simulation.iverilog_binary``/
    ``vvp_binary`` absolute paths) and canonicalizing
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
        exclude=_EQUIV_RERUN_EXCLUDE_PATHS,
        committed_for_diff=_canonicalize_equiv_report_for_rerun_diff(committed),
        fresh_for_diff=_canonicalize_equiv_report_for_rerun_diff(fresh),
    )
